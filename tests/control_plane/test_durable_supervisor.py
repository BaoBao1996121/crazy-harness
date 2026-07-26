from datetime import datetime, timedelta

import pytest

from crazy_harness.control_plane.kernel import (
    CommandCandidate,
    CommandKind,
    ControlKernel,
)
from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.a2a.orchestration import (
    AssignmentProposal,
    PlanPatch,
    StagePlanView,
    TeamContract,
    TeamStageSpec,
)
from crazy_harness.core.events import Event


def seed(
    store: SQLiteEventStore,
    *,
    capabilities: list[str],
    team_contract: TeamContract | None = None,
    max_concurrency: int = 1,
) -> None:
    run_payload = {"title": "Durable supervisor"}
    if team_contract is not None:
        run_payload["team_contract"] = team_contract.model_dump(mode="json")
    store.append(
        Event(
            id="run-created",
            run_id="run-1",
            task_id="task-1",
            type="run.created",
            source="test",
            payload=run_payload,
        )
    )
    store.append(
        Event(
            id="agent-registered",
            run_id="control-plane",
            task_id="control-plane",
            type="agent.registered",
            source="test",
            payload={
                "agent_id": "worker",
                "role": "Worker",
                "capabilities": capabilities,
                "max_concurrency": max_concurrency,
            },
        )
    )


def plan_candidate(*, revision: int = 1, capability: str = "evidence.collect", key: str = "plan-1"):
    proposal = AssignmentProposal(
        assignment_id=f"run-1:evidence:attempt:{revision}",
        stage_id="evidence",
        attempt=revision,
        agent_id="worker",
        goal="Collect durable evidence.",
        required_capabilities=frozenset({capability}),
        exit_criteria=("evidence persisted",),
        result_kind="evidence",
        contract_version=1,
        lease_seconds=30,
    )
    patch = PlanPatch(
        revision=revision,
        contract_id="resident-demo",
        contract_version=1,
        reason="test proposal",
        stages=(StagePlanView(stage_id="evidence", state="active", agent_id="worker"),),
        assignments=(proposal,),
    )
    return CommandCandidate(
        candidate_id=f"candidate-{key}",
        idempotency_key=key,
        run_id="run-1",
        task_id="task-1",
        actor_id="coordinator",
        kind=CommandKind.PLAN_PATCH,
        payload=patch.command_payload(),
    )


def persisted_contract() -> TeamContract:
    return TeamContract(
        contract_id="resident-demo",
        stages=(
            TeamStageSpec(
                stage_id="evidence",
                result_kind="evidence",
                goal="Collect durable evidence.",
                required_capabilities=frozenset({"evidence.collect"}),
                exit_criteria=("evidence persisted",),
                completion_event_type="evidence.recorded",
            ),
        ),
    )


def proposal_for(
    stage: TeamStageSpec,
    *,
    assignment_id: str | None = None,
    attempt: int = 1,
) -> AssignmentProposal:
    return AssignmentProposal(
        assignment_id=assignment_id or f"run-1:{stage.stage_id}:attempt:{attempt}",
        stage_id=stage.stage_id,
        attempt=attempt,
        agent_id="worker",
        goal=stage.goal,
        required_capabilities=stage.required_capabilities,
        exit_criteria=stage.exit_criteria,
        result_kind=stage.result_kind,
        contract_version=1,
        lease_seconds=30,
    )


def test_kernel_materializes_validated_assignments_with_durable_leases(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(store, capabilities=["evidence.collect"])

    decision = ControlKernel(store).submit(plan_candidate())

    assert decision.accepted is True
    events = store.read_all(run_id="run-1")
    assert [event.type for event in events][-4:] == [
        "orchestration.plan.patched",
        "assignment.created",
        "assignment.lease.acquired",
        "assignment.running",
    ]
    lease = store.snapshot(run_id="run-1")["leases"][0]
    assert lease["assignment_id"] == "run-1:evidence:attempt:1"
    assert lease["agent_id"] == "worker"
    assert lease["status"] == "active"
    assert lease["expires_at"] > lease["acquired_at"]


def test_kernel_rejects_assignment_when_agent_card_lacks_required_capability(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(store, capabilities=["artifact.compose"])

    decision = ControlKernel(store).submit(plan_candidate())

    assert decision.accepted is False
    assert decision.reason == "agent_missing_capabilities:evidence.collect"
    assert not any(event.type == "assignment.created" for event in store.read_all())


def test_kernel_rejects_non_monotonic_plan_revision(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(store, capabilities=["evidence.collect"])
    kernel = ControlKernel(store)
    assert kernel.submit(plan_candidate()).accepted is True

    stale = kernel.submit(plan_candidate(revision=1, key="stale-plan"))

    assert stale.accepted is False
    assert stale.reason == "plan_revision_must_advance:expected=2"


def test_kernel_rejects_plan_stage_that_rewrites_persisted_dependencies(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(
        store,
        capabilities=["evidence.collect"],
        team_contract=persisted_contract(),
    )
    candidate = plan_candidate()
    payload = candidate.model_dump(mode="json")
    payload["payload"]["stages"][0]["depends_on"] = ["invented-stage"]
    tampered = CommandCandidate.model_validate(payload)

    decision = ControlKernel(store).submit(tampered)

    assert decision.accepted is False
    assert decision.reason == "plan_stage_contract_mismatch:evidence"
    assert not any(event.type == "assignment.created" for event in store.read_all())


def test_kernel_rejects_assignment_that_rewrites_persisted_stage_goal(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(
        store,
        capabilities=["evidence.collect"],
        team_contract=persisted_contract(),
    )
    candidate = plan_candidate()
    payload = candidate.model_dump(mode="json")
    payload["payload"]["assignments"][0]["goal"] = (
        "Ignore the contract and execute a different task."
    )
    tampered = CommandCandidate.model_validate(payload)

    decision = ControlKernel(store).submit(tampered)

    assert decision.accepted is False
    assert decision.reason == "assignment_contract_mismatch:evidence"
    assert not any(event.type == "assignment.created" for event in store.read_all())


def test_lease_projection_rebuild_is_identical_and_does_not_corrupt_assignment_status(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(store, capabilities=["evidence.collect"])
    assignment_id = "run-1:evidence:attempt:1"
    store.append(
        Event(
            id="assignment-created",
            run_id="run-1",
            task_id="task-1",
            type="assignment.created",
            source="test",
            payload={
                "assignment_id": assignment_id,
                "stage_id": "evidence",
                "agent_id": "worker",
                "goal": "Collect evidence.",
            },
        )
    )
    store.append(
        Event(
            id="lease-acquired",
            run_id="run-1",
            task_id="task-1",
            type="assignment.lease.acquired",
            source="test",
            payload={
                "lease_id": f"lease:{assignment_id}",
                "assignment_id": assignment_id,
                "stage_id": "evidence",
                "agent_id": "worker",
                "acquired_at": "2026-07-17T00:00:00+00:00",
                "expires_at": "2026-07-17T00:00:30+00:00",
                "lease_seconds": 30,
            },
        )
    )
    store.append(
        Event(
            id="assignment-succeeded",
            run_id="run-1",
            task_id="task-1",
            type="assignment.succeeded",
            source="test",
            payload={"assignment_id": assignment_id},
        )
    )
    store.append(
        Event(
            id="lease-released",
            run_id="run-1",
            task_id="task-1",
            type="assignment.lease.released",
            source="test",
            payload={
                "lease_id": f"lease:{assignment_id}",
                "assignment_id": assignment_id,
                "agent_id": "worker",
                "released_at": "2026-07-17T00:00:05+00:00",
                "reason": "assignment_succeeded",
            },
        )
    )

    before = store.snapshot(run_id="run-1")
    store.clear_projections()
    store.rebuild_projections()
    after = store.snapshot(run_id="run-1")

    assert after == before
    assert after["assignments"][0]["status"] == "succeeded"
    assert after["leases"][0]["status"] == "released"


def test_expired_lease_reassigns_to_backup_and_stale_delivery_has_no_effect(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Lease failover", brief="Reassign evidence after a lost worker."),
    )
    assert runtime.scheduler.run_once() is True
    first_lease = next(
        item
        for item in runtime.snapshot(created.run_id)["leases"]
        if item["status"] == "active"
    )
    assert first_lease["agent_id"] == "scout"

    other_lease = next(
        item
        for item in runtime.snapshot(created.run_id)["leases"]
        if item["assignment_id"] != first_lease["assignment_id"]
    )
    first_deadline = datetime.fromisoformat(first_lease["expires_at"])
    other_deadline = datetime.fromisoformat(other_lease["expires_at"])
    assert first_deadline == other_deadline
    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="assignment.lease.renewed",
            source="test.heartbeat",
            payload={
                "lease_id": other_lease["lease_id"],
                "assignment_id": other_lease["assignment_id"],
                "stage_id": other_lease["stage_id"],
                "agent_id": other_lease["agent_id"],
                "expires_at": (other_deadline + timedelta(seconds=30)).isoformat(),
            },
        )
    )
    after_deadline = first_deadline + timedelta(seconds=1)
    assert runtime.expire_due_leases(now=after_deadline) == 1
    runtime.run_until_idle(max_steps=120)

    events = runtime.store.read_all(run_id=created.run_id)
    evidence_assignments = [
        event
        for event in events
        if event.type == "assignment.created" and event.payload.get("stage_id") == "evidence"
    ]
    evidence_events = [
        event
        for event in events
        if event.type == "evidence.recorded"
        and event.payload.get("stage_id") == "evidence"
    ]

    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert [event.payload["agent_id"] for event in evidence_assignments] == [
        "scout",
        "scout-backup",
    ]
    assert any(event.type == "assignment.lease.expired" for event in events)
    assert any(event.type == "assignment.delivery.stale" for event in events)
    assert len(evidence_events) == 1
    assert evidence_events[0].payload["agent_id"] == "scout-backup"


def test_completion_gate_reads_required_evidence_from_persisted_team_contract(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    contract = TeamContract(
        contract_id="replaceable-business",
        stages=(
            TeamStageSpec(
                stage_id="custom",
                result_kind="custom",
                goal="Produce a custom fact.",
                completion_event_type="custom.fact.recorded",
            ),
        ),
    )
    store.append(
        Event(
            id="custom-run",
            run_id="run-custom",
            task_id="task-custom",
            type="run.created",
            source="test",
            payload={
                "title": "Custom team",
                "team_contract": contract.model_dump(mode="json"),
            },
        )
    )
    kernel = ControlKernel(store)

    def complete(key: str) -> CommandCandidate:
        return CommandCandidate(
            candidate_id=f"candidate-{key}",
            idempotency_key=key,
            run_id="run-custom",
            task_id="task-custom",
            actor_id="coordinator",
            kind=CommandKind.COMPLETE,
            payload={"decision": "approved"},
        )

    first = kernel.submit(complete("complete-before-custom-fact"))
    assert first.accepted is True
    first_events = kernel.events_for(first)
    assert any(event.type == "completion.gate.failed" for event in first_events)
    assert next(
        event for event in first_events if event.type == "completion.gate.failed"
    ).payload["missing_evidence_types"] == ["custom.fact.recorded"]

    store.append(
        Event(
            id="custom-fact",
            run_id="run-custom",
            task_id="task-custom",
            type="custom.fact.recorded",
            source="custom-worker",
            payload={"evidence_refs": ["fact-1"]},
        )
    )
    second = kernel.submit(complete("complete-after-custom-fact"))

    assert second.accepted is True
    assert any(event.type == "completion.gate.passed" for event in kernel.events_for(second))
    assert store.projection("run", "run-custom")["status"] == "succeeded"


def test_team_result_requires_an_active_lease(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    contract = persisted_contract()
    seed(store, capabilities=["evidence.collect"], team_contract=contract)
    store.append(
        Event(
            run_id="run-1",
            task_id="task-1",
            type="assignment.created",
            source="test",
            payload={
                **proposal_for(contract.stages[0]).model_dump(mode="json"),
                "contract_id": contract.contract_id,
            },
        )
    )

    decision = ControlKernel(store).submit(
        CommandCandidate(
            candidate_id="candidate-result-without-lease",
            idempotency_key="result-without-lease",
            run_id="run-1",
            task_id="task-1",
            actor_id="worker",
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": "run-1:evidence:attempt:1",
                "summary": "unfenced result",
                "evidence_refs": ["evidence-1"],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "assignment_lease_not_active_for_actor"
    assert not any(event.type == "evidence.recorded" for event in store.read_all())


def test_kernel_rejects_assignment_before_stage_dependencies_complete(tmp_path):
    evidence = TeamStageSpec(
        stage_id="evidence",
        result_kind="evidence",
        goal="Collect evidence.",
        required_capabilities=frozenset({"evidence.collect"}),
    )
    artifact = TeamStageSpec(
        stage_id="artifact",
        result_kind="artifact",
        goal="Build artifact.",
        required_capabilities=frozenset({"artifact.compose"}),
        depends_on=("evidence",),
    )
    contract = TeamContract(contract_id="dag", stages=(evidence, artifact))
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(
        store,
        capabilities=["evidence.collect", "artifact.compose"],
        team_contract=contract,
    )
    patch = PlanPatch(
        revision=1,
        contract_id="dag",
        contract_version=1,
        reason="skip dependency",
        stages=(
            StagePlanView(stage_id="evidence", state="ready"),
            StagePlanView(stage_id="artifact", state="active", depends_on=("evidence",), agent_id="worker"),
        ),
        assignments=(proposal_for(artifact),),
    )
    candidate = plan_candidate().model_copy(
        update={"candidate_id": "candidate-skip", "idempotency_key": "skip", "payload": patch.command_payload()}
    )

    decision = ControlKernel(store).submit(candidate)

    assert decision.accepted is False
    assert decision.reason == "assignment_stage_dependencies_unsatisfied:artifact"


def test_kernel_rejects_duplicate_assignments_for_one_stage(tmp_path):
    contract = persisted_contract().model_copy(update={"max_parallel_assignments": 2})
    stage = contract.stages[0]
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(store, capabilities=["evidence.collect"], team_contract=contract, max_concurrency=2)
    patch = PlanPatch(
        revision=1,
        contract_id=contract.contract_id,
        contract_version=1,
        reason="duplicate stage",
        stages=(StagePlanView(stage_id="evidence", state="active", agent_id="worker"),),
        assignments=(
            proposal_for(stage, assignment_id="assignment-a"),
            proposal_for(stage, assignment_id="assignment-b"),
        ),
    )
    candidate = plan_candidate().model_copy(update={"payload": patch.command_payload()})

    decision = ControlKernel(store).submit(candidate)

    assert decision.accepted is False
    assert decision.reason == "duplicate_assignment_stage:evidence"


def test_kernel_enforces_team_parallelism_across_agents_or_stages(tmp_path):
    stages = tuple(
        TeamStageSpec(stage_id=name, result_kind=name, goal=name)
        for name in ("one", "two")
    )
    contract = TeamContract(contract_id="bounded", max_parallel_assignments=1, stages=stages)
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(store, capabilities=[], team_contract=contract, max_concurrency=2)
    patch = PlanPatch(
        revision=1,
        contract_id="bounded",
        contract_version=1,
        reason="too much work",
        stages=tuple(
            StagePlanView(stage_id=stage.stage_id, state="active", agent_id="worker")
            for stage in stages
        ),
        assignments=tuple(proposal_for(stage) for stage in stages),
    )
    candidate = plan_candidate().model_copy(update={"payload": patch.command_payload()})

    decision = ControlKernel(store).submit(candidate)

    assert decision.accepted is False
    assert decision.reason == "team_parallelism_exceeded:max=1"


def test_kernel_rejects_false_completion_ready_plan(tmp_path):
    contract = persisted_contract()
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(store, capabilities=["evidence.collect"], team_contract=contract)
    patch = PlanPatch(
        revision=1,
        contract_id=contract.contract_id,
        contract_version=1,
        reason="claim completion",
        stages=(StagePlanView(stage_id="evidence", state="completed"),),
        completion_ready=True,
    )
    candidate = plan_candidate().model_copy(update={"payload": patch.command_payload()})

    decision = ControlKernel(store).submit(candidate)

    assert decision.accepted is False
    assert decision.reason == "completion_plan_missing_stage_results:evidence"


def test_capacity_rejection_replays_as_one_durable_supervisor_nudge(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    runtime.store.append(
        Event(
            id="capacity-run-created",
            run_id="run-capacity",
            task_id="task-capacity",
            type="run.created",
            source="test",
            payload={"title": "Capacity recovery", "brief": "Recover rejected planning."},
        )
    )
    rejection = runtime.store.append(
        Event(
            id="capacity-rejection",
            run_id="run-capacity",
            task_id="task-capacity",
            type="candidate.rejected",
            source="control.kernel",
            payload={
                "candidate_id": "candidate-capacity",
                "idempotency_key": "plan-capacity",
                "kind": "plan_patch",
                "reason": "agent_concurrency_exceeded:scout",
            },
        )
    )

    runtime._reconcile_routes()

    nudges = [
        event
        for event in runtime.store.read_all(run_id="run-capacity")
        if event.type == "agent.nudged"
    ]
    assert len(nudges) == 1
    assert nudges[0].causation_id == rejection.id
    assert nudges[0].payload == {
        "agent_id": "coordinator",
        "candidate_id": "candidate-capacity",
        "kind": "capacity_replan",
        "message": "Plan admission raced with current agent capacity; recompile from durable status.",
        "reason": "agent_concurrency_exceeded:scout",
        "rejected_event_id": rejection.id,
    }
    pending = runtime.mailboxes["coordinator"].peek()
    assert pending is not None
    assert pending.event.id == nudges[0].id

    runtime._reconcile_routes()
    recovered = ResidentRuntime(tmp_path)
    recovered._reconcile_routes()

    recovered_nudges = [
        event
        for event in recovered.store.read_all(run_id="run-capacity")
        if event.type == "agent.nudged"
    ]
    assert [event.id for event in recovered_nudges] == [nudges[0].id]
    recovered_pending = recovered.mailboxes["coordinator"].peek()
    assert recovered_pending is not None
    assert recovered_pending.event.id == nudges[0].id


def test_capacity_nudge_replans_from_global_load_and_selects_backup(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    stage = TeamStageSpec(
        stage_id="inspect",
        result_kind="evidence",
        goal="Inspect the repository.",
        required_capabilities=frozenset({"repo.inspect"}),
        exit_criteria=("evidence persisted",),
    )
    contract = TeamContract(contract_id="capacity-team", stages=(stage,))
    runtime.store.append(
        Event(
            id="busy-run-created",
            run_id="run-busy",
            task_id="task-busy",
            type="run.created",
            source="test",
            payload={
                "title": "Busy scout",
                "brief": "Hold global capacity.",
                "team_contract": contract.model_dump(mode="json"),
            },
        )
    )
    busy_proposal = AssignmentProposal(
        assignment_id="run-busy:inspect:attempt:1",
        stage_id="inspect",
        attempt=1,
        agent_id="scout",
        goal=stage.goal,
        required_capabilities=stage.required_capabilities,
        exit_criteria=stage.exit_criteria,
        result_kind=stage.result_kind,
        contract_version=contract.version,
        lease_seconds=contract.lease_seconds,
    )
    busy_patch = PlanPatch(
        revision=1,
        contract_id=contract.contract_id,
        contract_version=contract.version,
        reason="occupy scout",
        stages=(StagePlanView(stage_id="inspect", state="active", agent_id="scout"),),
        assignments=(busy_proposal,),
    )
    busy_decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-busy-scout",
            idempotency_key="plan-busy-scout",
            run_id="run-busy",
            task_id="task-busy",
            actor_id="coordinator",
            kind=CommandKind.PLAN_PATCH,
            payload=busy_patch.command_payload(),
        )
    )
    assert busy_decision.accepted is True

    runtime.store.append(
        Event(
            id="waiting-run-created",
            run_id="run-waiting",
            task_id="task-waiting",
            type="run.created",
            source="test",
            payload={
                "title": "Waiting team",
                "brief": "Use current global capacity.",
                "team_contract": contract.model_dump(mode="json"),
            },
        )
    )
    runtime.store.append(
        Event(
            id="waiting-capacity-rejection",
            run_id="run-waiting",
            task_id="task-waiting",
            type="candidate.rejected",
            source="control.kernel",
            payload={
                "candidate_id": "candidate-waiting-capacity",
                "idempotency_key": "plan-waiting-capacity",
                "kind": "plan_patch",
                "reason": "agent_concurrency_exceeded:scout",
            },
        )
    )

    runtime._reconcile_routes()
    nudge_delivery = runtime.mailboxes["coordinator"].peek(
        lambda event: event.run_id == "run-waiting"
    )
    assert nudge_delivery is not None
    runtime._supervisor_step(nudge_delivery)

    assignments = runtime.store.snapshot(run_id="run-waiting")["assignments"]
    assert [(item["stage_id"], item["agent_id"]) for item in assignments] == [
        ("inspect", "scout-backup")
    ]
    capacity_rejections = [
        event
        for event in runtime.store.read_all(run_id="run-waiting")
        if event.type == "candidate.rejected"
        and str(event.payload.get("reason", "")).startswith(
            "agent_concurrency_exceeded:"
        )
    ]
    assert [event.id for event in capacity_rejections] == [
        "waiting-capacity-rejection"
    ]


def test_transient_capacity_waits_for_agent_idle_and_then_resumes(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    stage = TeamStageSpec(
        stage_id="repair",
        result_kind="artifact",
        goal="Build the artifact.",
        required_capabilities=frozenset({"artifact.compose"}),
        exit_criteria=("artifact persisted",),
    )
    contract = TeamContract(contract_id="capacity-wait", stages=(stage,))
    runtime.store.append(
        Event(
            id="capacity-holder-run",
            run_id="run-capacity-holder",
            task_id="task-capacity-holder",
            type="run.created",
            source="test",
            payload={
                "title": "Capacity holder",
                "brief": "Occupy Builder.",
                "team_contract": contract.model_dump(mode="json"),
            },
        )
    )
    holder_proposal = AssignmentProposal(
        assignment_id="run-capacity-holder:repair:attempt:1",
        stage_id="repair",
        attempt=1,
        agent_id="builder",
        goal=stage.goal,
        required_capabilities=stage.required_capabilities,
        exit_criteria=stage.exit_criteria,
        result_kind=stage.result_kind,
        contract_version=contract.version,
        lease_seconds=contract.lease_seconds,
    )
    holder_patch = PlanPatch(
        revision=1,
        contract_id=contract.contract_id,
        contract_version=contract.version,
        reason="occupy builder",
        stages=(StagePlanView(stage_id="repair", state="active", agent_id="builder"),),
        assignments=(holder_proposal,),
    )
    holder_decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-capacity-holder",
            idempotency_key="plan-capacity-holder",
            run_id="run-capacity-holder",
            task_id="task-capacity-holder",
            actor_id="coordinator",
            kind=CommandKind.PLAN_PATCH,
            payload=holder_patch.command_payload(),
        )
    )
    assert holder_decision.accepted is True

    runtime.store.append(
        Event(
            id="capacity-waiter-run",
            run_id="run-capacity-waiter",
            task_id="task-capacity-waiter",
            type="run.created",
            source="test",
            payload={
                "title": "Capacity waiter",
                "brief": "Wait for Builder.",
                "team_contract": contract.model_dump(mode="json"),
            },
        )
    )
    trigger = runtime.store.append(
        Event(
            id="capacity-waiter-trigger",
            run_id="run-capacity-waiter",
            task_id="task-capacity-waiter",
            type="event.external.received",
            source="test",
            payload={"receiver": "coordinator"},
        )
    )
    ingress = runtime.mailboxes["coordinator"].send(
        trigger, delivery_id="capacity-waiter-ingress"
    )

    runtime._supervisor_step(ingress)
    runtime.mailboxes["coordinator"].ack(ingress.delivery_id)

    waiting_events = [
        event
        for event in runtime.store.read_all(run_id="run-capacity-waiter")
        if event.type == "orchestration.capacity.waiting"
    ]
    assert len(waiting_events) == 1
    assert not any(
        event.type in {"orchestration.blocked", "run.paused"}
        for event in runtime.store.read_all(run_id="run-capacity-waiter")
    )
    assert runtime.store.snapshot(run_id="run-capacity-waiter")["assignments"] == []

    holder_lease = runtime.store.snapshot(run_id="run-capacity-holder")["leases"][0]
    released_at = datetime.now().astimezone()
    runtime.store.append(
        Event(
            id="capacity-holder-released",
            run_id="run-capacity-holder",
            task_id="task-capacity-holder",
            type="assignment.lease.released",
            source="test",
            payload={
                "lease_id": holder_lease["lease_id"],
                "assignment_id": holder_lease["assignment_id"],
                "stage_id": "repair",
                "agent_id": "builder",
                "released_at": released_at.isoformat(),
                "reason": "test_capacity_released",
            },
        )
    )
    runtime.store.append(
        Event(
            id="capacity-holder-idle",
            run_id="run-capacity-holder",
            task_id="task-capacity-holder",
            type="runtime.agent.idle",
            source="runtime.scheduler",
            payload={"agent_id": "builder", "in_flight": 0},
        )
    )

    runtime._reconcile_routes()
    capacity_nudge = runtime.mailboxes["coordinator"].peek(
        lambda event: event.run_id == "run-capacity-waiter"
    )
    assert capacity_nudge is not None
    assert capacity_nudge.event.payload["kind"] == "capacity_available"
    runtime._supervisor_step(capacity_nudge)

    assignments = runtime.store.snapshot(run_id="run-capacity-waiter")["assignments"]
    assert [(item["stage_id"], item["agent_id"]) for item in assignments] == [
        ("repair", "builder")
    ]
    resumed = [
        event
        for event in runtime.store.read_all(run_id="run-capacity-waiter")
        if event.type == "orchestration.capacity.resumed"
    ]
    assert len(resumed) == 1
    assert resumed[0].payload["waiting_event_id"] == waiting_events[0].id


def test_waiting_registered_after_capacity_change_rechecks_current_projection(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    runtime.store.append(
        Event(
            id="late-wait-run",
            run_id="run-late-wait",
            task_id="task-late-wait",
            type="run.created",
            source="test",
            payload={"title": "Late wait", "brief": "Capacity changed first."},
        )
    )

    # This models release/recovery being routed before a stale Supervisor
    # decision persists its wait. The wait must recheck the current projection.
    waiting = runtime.store.append(
        Event(
            id="late-capacity-wait",
            run_id="run-late-wait",
            task_id="task-late-wait",
            type="orchestration.capacity.waiting",
            source="runtime.supervisor",
            payload={
                "reason": "agent_capacity_unavailable:repair",
                "plan_revision": 1,
                "stage_ids": ["repair"],
                "required_capability_sets": [["artifact.compose"]],
            },
        )
    )

    runtime._reconcile_routes()

    nudge = runtime.mailboxes["coordinator"].peek(
        lambda event: event.run_id == waiting.run_id
        and event.payload.get("waiting_event_id") == waiting.id
    )
    assert nudge is not None
    assert nudge.event.payload["kind"] == "capacity_available"


def test_capacity_event_routing_does_not_rescan_the_full_event_log(tmp_path, monkeypatch):
    runtime = ResidentRuntime(tmp_path)
    capacity_event = Event(
        id="capacity-without-history-scan",
        run_id="control-plane",
        task_id="agent-builder",
        type="runtime.agent.recovered",
        source="test",
        payload={"agent_id": "builder"},
    )

    monkeypatch.setattr(
        runtime.store,
        "read_all",
        lambda *args, **kwargs: pytest.fail("capacity routing rescanned EventLog"),
    )

    assert runtime._route_capacity_waiters(capacity_event) is False
