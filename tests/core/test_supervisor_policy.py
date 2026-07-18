import pytest

from crazy_harness.core.a2a.coordinator import AgentStatus
from crazy_harness.core.a2a.messages import AgentCard
from crazy_harness.core.a2a.orchestration import (
    CapabilitySupervisorPolicy,
    SupervisorContext,
    TeamContract,
    TeamStageSpec,
)


def card(agent_id: str, *capabilities: str, role: str = "worker") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        role=role,
        capabilities=list(capabilities),
        max_concurrency=1,
    )


def context(
    *,
    cards: tuple[AgentCard, ...],
    statuses: dict[str, AgentStatus] | None = None,
    completed: frozenset[str] = frozenset(),
    active: frozenset[str] = frozenset(),
    attempts: dict[str, int] | None = None,
) -> SupervisorContext:
    return SupervisorContext(
        run_id="run-1",
        task_id="task-1",
        brief="Collect evidence and produce a checked artifact.",
        revision=0,
        cards=cards,
        statuses=statuses or {item.agent_id: AgentStatus.IDLE for item in cards},
        completed_stage_ids=completed,
        active_stage_ids=active,
        attempts=attempts or {},
        active_loads={},
    )


def test_supervisor_selects_by_capability_not_role_name():
    contract = TeamContract(
        contract_id="demo",
        stages=(
            TeamStageSpec(
                stage_id="evidence",
                result_kind="evidence",
                goal="collect facts",
                required_capabilities=frozenset({"evidence.collect"}),
                exit_criteria=("facts persisted",),
            ),
        ),
    )
    cards = (
        card("named-scout", "artifact.compose", role="Scout"),
        card("oddly-named-worker", "evidence.collect", role="Tea maker"),
    )

    patch = CapabilitySupervisorPolicy().propose(contract, context(cards=cards))

    assert [item.agent_id for item in patch.assignments] == ["oddly-named-worker"]
    assert patch.assignments[0].stage_id == "evidence"


def test_supervisor_can_activate_two_independent_ready_stages():
    contract = TeamContract(
        contract_id="parallel-demo",
        max_parallel_assignments=2,
        stages=(
            TeamStageSpec(
                stage_id="evidence",
                result_kind="evidence",
                goal="collect facts",
                required_capabilities=frozenset({"evidence.collect"}),
            ),
            TeamStageSpec(
                stage_id="risk",
                result_kind="risk",
                goal="inspect risk",
                required_capabilities=frozenset({"risk.inspect"}),
            ),
        ),
    )
    cards = (card("collector", "evidence.collect"), card("risk-worker", "risk.inspect"))

    patch = CapabilitySupervisorPolicy().propose(contract, context(cards=cards))

    assert {(item.stage_id, item.agent_id) for item in patch.assignments} == {
        ("evidence", "collector"),
        ("risk", "risk-worker"),
    }


def test_unserviceable_ready_stage_does_not_consume_assignment_capacity():
    contract = TeamContract(
        contract_id="capacity-demo",
        max_parallel_assignments=1,
        stages=(
            TeamStageSpec(
                stage_id="a-unserviceable",
                result_kind="missing",
                goal="requires a missing capability",
                required_capabilities=frozenset({"missing.capability"}),
            ),
            TeamStageSpec(
                stage_id="z-serviceable",
                result_kind="evidence",
                goal="collect available evidence",
                required_capabilities=frozenset({"evidence.collect"}),
            ),
        ),
    )
    cards = (card("collector", "evidence.collect"),)

    patch = CapabilitySupervisorPolicy().propose(contract, context(cards=cards))

    assert [(item.stage_id, item.agent_id) for item in patch.assignments] == [
        ("z-serviceable", "collector")
    ]


def test_supervisor_uses_backup_after_primary_is_degraded():
    contract = TeamContract(
        contract_id="failover-demo",
        stages=(
            TeamStageSpec(
                stage_id="evidence",
                result_kind="evidence",
                goal="collect facts",
                required_capabilities=frozenset({"evidence.collect"}),
            ),
        ),
    )
    cards = (card("primary", "evidence.collect"), card("secondary", "evidence.collect"))
    statuses = {"primary": AgentStatus.DEGRADED, "secondary": AgentStatus.IDLE}

    patch = CapabilitySupervisorPolicy().propose(
        contract,
        context(cards=cards, statuses=statuses, attempts={"evidence": 1}),
    )

    assert patch.assignments[0].agent_id == "secondary"
    assert patch.assignments[0].attempt == 2


def test_supervisor_requests_completion_only_after_every_stage_is_complete():
    contract = TeamContract(
        contract_id="complete-demo",
        stages=(TeamStageSpec(stage_id="review", result_kind="review", goal="review"),),
    )

    patch = CapabilitySupervisorPolicy().propose(
        contract,
        context(cards=(), completed=frozenset({"review"})),
    )

    assert patch.completion_ready is True
    assert patch.assignments == ()


def test_team_contract_rejects_dependency_cycles():
    with pytest.raises(ValueError, match="cycle"):
        TeamContract(
            contract_id="cyclic",
            stages=(
                TeamStageSpec(stage_id="a", result_kind="a", goal="a", depends_on=("b",)),
                TeamStageSpec(stage_id="b", result_kind="b", goal="b", depends_on=("a",)),
            ),
        )
