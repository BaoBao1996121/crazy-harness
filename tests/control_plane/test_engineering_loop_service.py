from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from crazy_harness.control_plane.engineering_loops import (
    ChildRunOutcome,
    EngineeringLoopIdempotencyConflict,
    EngineeringLoopRequest,
    EngineeringLoopService,
)
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.engineering_loops import (
    CandidateProposal,
    EngineeringIterationIdentity,
    EngineeringLoopBudget,
    EngineeringLoopContract,
    IterationEvaluation,
    MetricContract,
    MetricDirection,
    WorkerProfile,
    engineering_iteration_identity,
)
from crazy_harness.core.events import Event


def request(*, request_id: str = "quality-loop-request") -> EngineeringLoopRequest:
    return EngineeringLoopRequest(
        request_id=request_id,
        title="Repository quality climb",
        objective="Reach every frozen repository quality gate",
        exit_criteria=("quality_score reaches 1",),
        loop_pack="repo-quality",
        worker=WorkerProfile(
            execution_mode="single",
            model_mode="scripted",
            task_pack="repo-quality",
        ),
        metric=MetricContract(
            name="quality_score",
            direction=MetricDirection.MAXIMIZE,
            target=Decimal("1"),
            evaluator_version="repo-quality-v1",
        ),
        budget=EngineeringLoopBudget(
            max_iterations=3,
            max_no_progress_iterations=2,
        ),
        permissions=("disposable_workspace_write", "run_bounded_checks"),
        initial_state_ref="snapshot://initial",
        input_payload={"fixture": "quality-climb-v1"},
    )


class DeterministicPorts:
    def __init__(self, scores: tuple[str, ...] = ("0.5", "1")) -> None:
        self.scores = scores
        self.proposals: list[CandidateProposal] = []
        self.launches: list[EngineeringIterationIdentity] = []

    def propose(
        self,
        contract: EngineeringLoopContract,
        identity: EngineeringIterationIdentity,
        active_state_ref: str,
    ) -> CandidateProposal:
        proposal = CandidateProposal(
            candidate_id=identity.candidate_id,
            iteration=identity.iteration,
            base_state_ref=active_state_ref,
            change_set={"kind": "scripted", "iteration": identity.iteration},
            rationale=f"Improve quality in iteration {identity.iteration}",
            expected_effect="Increase quality_score",
            proposer_attestation={"provider": "scripted", "version": "v1"},
        )
        self.proposals.append(proposal)
        return proposal

    def launch(
        self,
        contract: EngineeringLoopContract,
        identity: EngineeringIterationIdentity,
        candidate: CandidateProposal,
    ) -> None:
        assert candidate.iteration == identity.iteration
        self.launches.append(identity)

    def observe(self, identity: EngineeringIterationIdentity) -> ChildRunOutcome:
        return ChildRunOutcome(
            run_id=identity.child_run_id,
            task_id=identity.child_task_id,
            status="succeeded",
            terminal_event_id=f"terminal-{identity.iteration}",
            candidate_state_ref=f"snapshot://candidate-{identity.iteration}",
            artifact_refs=(f"artifact://submission-{identity.iteration}",),
            evidence_refs=(f"event://test-{identity.iteration}",),
        )

    def evaluate(
        self,
        contract: EngineeringLoopContract,
        candidate: CandidateProposal,
        outcome: ChildRunOutcome,
    ) -> IterationEvaluation:
        score = Decimal(self.scores[candidate.iteration - 1])
        return IterationEvaluation(
            iteration=candidate.iteration,
            candidate_id=candidate.candidate_id,
            candidate_state_ref=outcome.candidate_state_ref,
            evaluator_version=contract.metric.evaluator_version,
            metrics={contract.metric.name: score},
            hard_gates={"workspace_confined": True},
            evidence_refs=outcome.evidence_refs,
            valid=True,
        )


def advance_until_terminal(
    service: EngineeringLoopService,
    loop_id: str,
    ports: DeterministicPorts,
) -> None:
    for _ in range(40):
        report = service.report(loop_id)
        if report.status != "running":
            return
        service.advance_one(
            loop_id,
            propose=ports.propose,
            launch_child=ports.launch,
            child_outcome=ports.observe,
            evaluate=ports.evaluate,
        )
    raise AssertionError("engineering loop did not reach a terminal state")


def test_create_is_idempotent_but_rejects_request_key_reuse(tmp_path) -> None:
    service = EngineeringLoopService(SQLiteEventStore(tmp_path / "events.db"))
    created = service.create(request())

    assert service.create(request()) == created
    with pytest.raises(EngineeringLoopIdempotencyConflict):
        service.create(request().model_copy(update={"objective": "Different objective"}))


def test_two_iterations_form_a_persisted_active_state_lineage(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    service = EngineeringLoopService(store)
    created = service.create(request())
    ports = DeterministicPorts()

    advance_until_terminal(service, created.loop_id, ports)

    report = service.report(created.loop_id)
    assert report.status == "completed"
    assert report.active_state_ref == "snapshot://candidate-2"
    assert report.active_score == Decimal("1")
    assert [iteration.status for iteration in report.iterations] == ["decided", "decided"]
    assert report.iterations[0].decision.accepted is True
    assert report.iterations[1].decision.accepted is True
    assert report.iterations[1].candidate.base_state_ref == "snapshot://candidate-1"
    assert [item.child_run_id for item in ports.launches] == [
        report.iterations[0].identity.child_run_id,
        report.iterations[1].identity.child_run_id,
    ]
    event_types = [event.type for event in store.read_all(run_id=created.loop_id)]
    assert event_types.count("engineering.iteration.planned") == 2
    assert event_types.count("engineering.evaluation.completed") == 2
    assert event_types[-1] == "engineering.loop.completed"


def test_parent_iteration_link_is_persisted_before_child_launch(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    service = EngineeringLoopService(store)
    loop_id = service.create(request()).loop_id
    ports = DeterministicPorts()

    for _ in range(4):
        assert service.advance_one(
            loop_id,
            propose=ports.propose,
            launch_child=ports.launch,
            child_outcome=ports.observe,
            evaluate=ports.evaluate,
        ) is True

    report = service.report(loop_id)
    assert report.iterations[0].status == "running"
    assert ports.launches == []

    def launch_after_parent_fact(contract, identity, candidate) -> None:
        assert service.report(loop_id).iterations[0].status == "running"
        ports.launch(contract, identity, candidate)

    assert service.advance_one(
        loop_id,
        propose=ports.propose,
        launch_child=launch_after_parent_fact,
        child_outcome=ports.observe,
        evaluate=ports.evaluate,
    ) is True
    assert [item.child_run_id for item in ports.launches] == [
        report.iterations[0].identity.child_run_id
    ]


def test_persisted_candidate_is_reused_after_process_crash(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    service = EngineeringLoopService(store)
    loop_id = service.create(request()).loop_id
    ports = DeterministicPorts()
    assert service.advance_one(
        loop_id,
        propose=ports.propose,
        launch_child=ports.launch,
        child_outcome=ports.observe,
        evaluate=ports.evaluate,
    ) is True

    armed = True

    def crash_after_candidate(phase: str) -> None:
        nonlocal armed
        if phase == "after_candidate_persisted" and armed:
            armed = False
            raise RuntimeError("injected process crash")

    crashing = EngineeringLoopService(store, fault_injector=crash_after_candidate)
    with pytest.raises(RuntimeError, match="injected process crash"):
        crashing.advance_one(
            loop_id,
            propose=ports.propose,
            launch_child=ports.launch,
            child_outcome=ports.observe,
            evaluate=ports.evaluate,
        )

    recovered = EngineeringLoopService(
        SQLiteEventStore(tmp_path / "events.db")
    )
    assert recovered.advance_one(
        loop_id,
        propose=ports.propose,
        launch_child=ports.launch,
        child_outcome=ports.observe,
        evaluate=ports.evaluate,
    ) is True
    assert len(ports.proposals) == 1
    assert recovered.report(loop_id).iterations[0].status == "candidate_validated"


def test_child_prepare_retry_reuses_the_same_identity(tmp_path) -> None:
    service = EngineeringLoopService(SQLiteEventStore(tmp_path / "events.db"))
    loop_id = service.create(request()).loop_id
    ports = DeterministicPorts()
    # The fourth durable phase commits engineering.iteration.started before
    # child preparation is allowed to create any separately scheduled work.
    for _ in range(4):
        assert service.advance_one(
            loop_id,
            propose=ports.propose,
            launch_child=ports.launch,
            child_outcome=ports.observe,
            evaluate=ports.evaluate,
        ) is True

    calls: list[str] = []

    def interrupted_launch(contract, identity, candidate) -> None:
        calls.append(identity.child_run_id)
        if len(calls) == 1:
            raise ConnectionError("crash after idempotent child prepare")

    with pytest.raises(ConnectionError):
        service.advance_one(
            loop_id,
            propose=ports.propose,
            launch_child=interrupted_launch,
            child_outcome=ports.observe,
            evaluate=ports.evaluate,
        )
    assert service.advance_one(
        loop_id,
        propose=ports.propose,
        launch_child=interrupted_launch,
        child_outcome=ports.observe,
        evaluate=ports.evaluate,
    ) is True
    assert calls[0] == calls[1]
    assert service.report(loop_id).iterations[0].status == "completed"


def test_nonterminal_child_does_not_create_fake_progress(tmp_path) -> None:
    service = EngineeringLoopService(SQLiteEventStore(tmp_path / "events.db"))
    loop_id = service.create(request()).loop_id
    ports = DeterministicPorts()
    for _ in range(4):
        service.advance_one(
            loop_id,
            propose=ports.propose,
            launch_child=ports.launch,
            child_outcome=ports.observe,
            evaluate=ports.evaluate,
        )

    progressed = service.advance_one(
        loop_id,
        propose=ports.propose,
        launch_child=ports.launch,
        child_outcome=lambda identity: None,
        evaluate=ports.evaluate,
    )

    assert progressed is False
    assert service.report(loop_id).iterations[0].status == "running"


def test_projection_rejects_a_forged_iteration_identity_or_state_lineage(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    service = EngineeringLoopService(store)
    loop_id = service.create(request()).loop_id
    identity = engineering_iteration_identity(loop_id, 1)
    forged = identity.model_copy(update={"child_run_id": "run-forged"})
    store.append(
        Event(
            id="forged-engineering-plan",
            run_id=loop_id,
            task_id=loop_id,
            type="engineering.iteration.planned",
            source="test",
            payload={
                "iteration": 1,
                "identity": forged.model_dump(mode="json"),
                "base_state_ref": "snapshot://stale",
            },
        )
    )

    with pytest.raises(RuntimeError, match="deterministic identity"):
        service.report(loop_id)


def test_wrong_child_outcome_is_recorded_and_blocks_instead_of_being_scored(tmp_path) -> None:
    service = EngineeringLoopService(SQLiteEventStore(tmp_path / "events.db"))
    loop_id = service.create(request()).loop_id
    ports = DeterministicPorts()
    for _ in range(4):
        service.advance_one(
            loop_id,
            propose=ports.propose,
            launch_child=ports.launch,
            child_outcome=ports.observe,
            evaluate=ports.evaluate,
        )
    identity = service.report(loop_id).iterations[0].identity
    forged = ports.observe(identity).model_copy(update={"run_id": "run-forged"})

    assert service.advance_one(
        loop_id,
        propose=ports.propose,
        launch_child=ports.launch,
        child_outcome=lambda _identity: forged,
        evaluate=ports.evaluate,
    ) is True
    assert service.report(loop_id).iterations[0].status == "failed"
    assert service.advance_one(
        loop_id,
        propose=ports.propose,
        launch_child=ports.launch,
        child_outcome=ports.observe,
        evaluate=ports.evaluate,
    ) is True
    assert service.report(loop_id).status == "blocked"


def test_competing_services_cannot_propose_two_candidates_for_one_iteration(tmp_path) -> None:
    database = tmp_path / "events.db"
    first = EngineeringLoopService(SQLiteEventStore(database))
    second = EngineeringLoopService(SQLiteEventStore(database))
    loop_id = first.create(request()).loop_id
    ports = DeterministicPorts()
    assert first.advance_one(
        loop_id,
        propose=ports.propose,
        launch_child=ports.launch,
        child_outcome=ports.observe,
        evaluate=ports.evaluate,
    ) is True
    entered = threading.Event()
    release = threading.Event()

    def slow_propose(contract, identity, active_state_ref):
        entered.set()
        assert release.wait(timeout=5)
        return ports.propose(contract, identity, active_state_ref)

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(
            first.advance_one,
            loop_id,
            propose=slow_propose,
            launch_child=ports.launch,
            child_outcome=ports.observe,
            evaluate=ports.evaluate,
        )
        assert entered.wait(timeout=5)
        loser = pool.submit(
            second.advance_one,
            loop_id,
            propose=ports.propose,
            launch_child=ports.launch,
            child_outcome=ports.observe,
            evaluate=ports.evaluate,
        )
        assert loser.result(timeout=5) is False
        release.set()
        assert winner.result(timeout=5) is True

    events = first.store.read_all(run_id=loop_id)
    assert sum(event.type == "engineering.candidate.proposed" for event in events) == 1
    assert len(ports.proposals) == 1
