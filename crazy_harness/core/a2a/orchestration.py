from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from crazy_harness.core.a2a.coordinator import AgentStatus
from crazy_harness.core.a2a.messages import AgentCard


class TeamStageSpec(BaseModel):
    """One business-replaceable stage in a declarative team task graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: str = Field(min_length=1)
    result_kind: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    required_capabilities: frozenset[str] = Field(default_factory=frozenset)
    exit_criteria: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    completion_event_type: str | None = None


class TeamContract(BaseModel):
    """Versioned DAG and limits supplied by a replaceable Team TaskPack."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    stages: tuple[TeamStageSpec, ...]
    max_parallel_assignments: int = Field(default=1, ge=1, le=32)
    lease_seconds: int = Field(default=30, ge=1, le=3600)

    @model_validator(mode="after")
    def validate_graph(self) -> TeamContract:
        stage_ids = [stage.stage_id for stage in self.stages]
        if not stage_ids:
            raise ValueError("team contract requires at least one stage")
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("team contract contains duplicate stage ids")
        known = set(stage_ids)
        for stage in self.stages:
            unknown = set(stage.depends_on) - known
            if unknown:
                raise ValueError(
                    f"stage {stage.stage_id} has unknown dependencies: {sorted(unknown)}"
                )

        dependencies = {stage.stage_id: set(stage.depends_on) for stage in self.stages}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(stage_id: str) -> None:
            if stage_id in visiting:
                raise ValueError("stage dependency cycle detected")
            if stage_id in visited:
                return
            visiting.add(stage_id)
            for dependency in dependencies[stage_id]:
                visit(dependency)
            visiting.remove(stage_id)
            visited.add(stage_id)

        for stage_id in stage_ids:
            visit(stage_id)
        return self

    @property
    def completion_event_types(self) -> frozenset[str]:
        return frozenset(
            stage.completion_event_type
            for stage in self.stages
            if stage.completion_event_type is not None
        )


class AssignmentProposal(BaseModel):
    """Untrusted assignment proposal inside a PlanPatch candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: str = Field(min_length=1)
    stage_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    agent_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    required_capabilities: frozenset[str] = Field(default_factory=frozenset)
    exit_criteria: tuple[str, ...] = ()
    result_kind: str = Field(min_length=1)
    contract_version: int = Field(default=1, ge=1)
    lease_seconds: int = Field(ge=1, le=3600)


class StagePlanView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: str
    state: Literal["pending", "ready", "active", "completed", "blocked"]
    depends_on: tuple[str, ...] = ()
    agent_id: str | None = None


class PlanPatch(BaseModel):
    """A Supervisor proposal; only ControlKernel may turn it into facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1)
    contract_id: str = Field(min_length=1)
    contract_version: int = Field(ge=1)
    reason: str = Field(min_length=1)
    stages: tuple[StagePlanView, ...]
    assignments: tuple[AssignmentProposal, ...] = ()
    completion_ready: bool = False
    blocked_reason: str | None = None

    def command_payload(self) -> dict:
        return self.model_dump(mode="json")


class SupervisorContext(BaseModel):
    """TeamView facts available to a policy; no worker private context is shared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    task_id: str
    brief: str
    revision: int = Field(default=0, ge=0)
    cards: tuple[AgentCard, ...]
    statuses: dict[str, AgentStatus]
    completed_stage_ids: frozenset[str] = Field(default_factory=frozenset)
    active_stage_ids: frozenset[str] = Field(default_factory=frozenset)
    active_stage_agents: dict[str, str] = Field(default_factory=dict)
    attempts: dict[str, int] = Field(default_factory=dict)
    active_loads: dict[str, int] = Field(default_factory=dict)


class SupervisorPolicy(Protocol):
    def propose(self, contract: TeamContract, context: SupervisorContext) -> PlanPatch: ...


class CapabilitySupervisorPolicy:
    """Deterministic baseline policy driven by DAG readiness, capability, and load."""

    _AVAILABLE = {AgentStatus.IDLE, AgentStatus.BUSY}

    def propose(self, contract: TeamContract, context: SupervisorContext) -> PlanPatch:
        all_stage_ids = frozenset(stage.stage_id for stage in contract.stages)
        completed = context.completed_stage_ids & all_stage_ids
        active = context.active_stage_ids & all_stage_ids
        revision = context.revision + 1

        if completed == all_stage_ids:
            return PlanPatch(
                revision=revision,
                contract_id=contract.contract_id,
                contract_version=contract.version,
                reason="all_contract_stages_completed",
                stages=self._stage_views(contract, completed, active, frozenset(), {}),
                completion_ready=True,
            )

        ready = tuple(
            stage
            for stage in sorted(contract.stages, key=lambda item: item.stage_id)
            if stage.stage_id not in completed | active
            and set(stage.depends_on).issubset(completed)
        )
        remaining_capacity = max(0, contract.max_parallel_assignments - len(active))
        loads = dict(context.active_loads)
        selected: list[AssignmentProposal] = []
        selected_by_stage: dict[str, str] = {}
        unavailable: list[str] = []

        for stage in ready:
            if len(selected) >= remaining_capacity:
                break
            card = self._select_card(stage, context, loads)
            if card is None:
                unavailable.append(stage.stage_id)
                continue
            attempt = int(context.attempts.get(stage.stage_id, 0)) + 1
            selected.append(
                AssignmentProposal(
                    assignment_id=(
                        f"{context.run_id}:{stage.stage_id}:attempt:{attempt}"
                    ),
                    stage_id=stage.stage_id,
                    attempt=attempt,
                    agent_id=card.agent_id,
                    goal=stage.goal,
                    required_capabilities=stage.required_capabilities,
                    exit_criteria=stage.exit_criteria,
                    result_kind=stage.result_kind,
                    contract_version=contract.version,
                    lease_seconds=contract.lease_seconds,
                )
            )
            selected_by_stage[stage.stage_id] = card.agent_id
            loads[card.agent_id] = int(loads.get(card.agent_id, 0)) + 1

        blocked_reason = None
        if unavailable and not selected and not active:
            blocked_reason = f"no_available_capable_agent:{','.join(sorted(unavailable))}"
        if selected:
            reason = "capability_and_status_match:" + ",".join(
                item.stage_id for item in selected
            )
        elif active:
            reason = "waiting_for_active_assignments"
        elif blocked_reason:
            reason = blocked_reason
        else:
            reason = "waiting_for_stage_dependencies"

        return PlanPatch(
            revision=revision,
            contract_id=contract.contract_id,
            contract_version=contract.version,
            reason=reason,
            stages=self._stage_views(
                contract,
                completed,
                active,
                frozenset(stage.stage_id for stage in ready),
                selected_by_stage,
                blocked=frozenset(unavailable) if blocked_reason else frozenset(),
                active_agents=context.active_stage_agents,
            ),
            assignments=tuple(selected),
            blocked_reason=blocked_reason,
        )

    def _select_card(
        self,
        stage: TeamStageSpec,
        context: SupervisorContext,
        loads: dict[str, int],
    ) -> AgentCard | None:
        eligible = [
            card
            for card in context.cards
            if context.statuses.get(card.agent_id, AgentStatus.OFFLINE) in self._AVAILABLE
            and int(loads.get(card.agent_id, 0)) < card.max_concurrency
            and stage.required_capabilities.issubset(set(card.capabilities))
        ]
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda card: (
                int(loads.get(card.agent_id, 0)),
                len(set(card.capabilities) - stage.required_capabilities),
                card.agent_id,
            ),
        )

    @staticmethod
    def _stage_views(
        contract: TeamContract,
        completed: frozenset[str],
        active: frozenset[str],
        ready: frozenset[str],
        selected_by_stage: dict[str, str],
        *,
        blocked: frozenset[str] = frozenset(),
        active_agents: dict[str, str] | None = None,
    ) -> tuple[StagePlanView, ...]:
        current_agents = active_agents or {}
        views: list[StagePlanView] = []
        for stage in contract.stages:
            if stage.stage_id in completed:
                state = "completed"
            elif stage.stage_id in active or stage.stage_id in selected_by_stage:
                state = "active"
            elif stage.stage_id in blocked:
                state = "blocked"
            elif stage.stage_id in ready:
                state = "ready"
            else:
                state = "pending"
            views.append(
                StagePlanView(
                    stage_id=stage.stage_id,
                    state=state,
                    depends_on=stage.depends_on,
                    agent_id=selected_by_stage.get(
                        stage.stage_id,
                        current_agents.get(stage.stage_id),
                    ),
                )
            )
        return tuple(views)
