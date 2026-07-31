from __future__ import annotations

from decimal import Decimal

import pytest

from crazy_harness.control_plane.engineering_loops import (
    ChildRunOutcome,
    EngineeringLoopRequest,
    EngineeringLoopService,
)
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.engineering_loops import (
    CandidateProposal,
    EngineeringIterationIdentity,
    EngineeringLoopBudget,
    EngineeringLoopContract,
    MetricContract,
    MetricDirection,
    WorkerProfile,
)
from crazy_harness.core.events import Event


def _request(request_id: str = "engineering-loop-terminal-child") -> EngineeringLoopRequest:
    return EngineeringLoopRequest(
        request_id=request_id,
        title="Repository quality climb",
        objective="Reach the frozen repository quality gates",
        exit_criteria=("quality_score reaches 1",),
        loop_pack="repo-quality",
        worker=WorkerProfile(execution_mode="single", model_mode="scripted", task_pack="repo-quality"),
        metric=MetricContract(name="quality_score", direction=MetricDirection.MAXIMIZE, target=Decimal("1"), evaluator_version="repo-quality-v1"),
        budget=EngineeringLoopBudget(max_iterations=2, max_no_progress_iterations=1),
        permissions=("disposable_workspace_write", "run_bounded_checks"),
        initial_state_ref="snapshot://initial",
    )


def _propose(
    _contract: EngineeringLoopContract,
    identity: EngineeringIterationIdentity,
    active_state_ref: str,
) -> CandidateProposal:
    return CandidateProposal(
        candidate_id=identity.candidate_id,
        iteration=identity.iteration,
        base_state_ref=active_state_ref,
        change_set={"kind": "scripted"},
        rationale="Exercise child terminal handling",
        expected_effect="No failed child can be promoted",
        proposer_attestation={"provider": "test", "version": "v1"},
    )


def _advance_to_running(service: EngineeringLoopService, loop_id: str) -> None:
    for _ in range(4):
        assert _advance(service, loop_id) is True
    assert service.report(loop_id).iterations[0].status == "running"


def _advance(
    service: EngineeringLoopService,
    loop_id: str,
    outcome: ChildRunOutcome | None = None,
) -> bool:
    return service.advance_one(
        loop_id,
        propose=_propose,
        launch_child=lambda *_args: None,
        child_outcome=lambda _identity: outcome,
        evaluate=lambda *_args: pytest.fail("unsuccessful child reached evaluation"),
    )


@pytest.mark.parametrize(
    ("child_status", "candidate_state_ref"),
    [
        ("failed", "snapshot://must-not-be-promoted"),
        ("cancelled", "snapshot://must-not-be-promoted"),
        ("failed", None),
    ],
)
def test_unsuccessful_child_is_failed_without_completion_or_evaluation(
    tmp_path,
    child_status: str,
    candidate_state_ref: str | None,
) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    service = EngineeringLoopService(store)
    suffix = "without-snapshot" if candidate_state_ref is None else "with-snapshot"
    loop_id = service.create(_request(f"terminal-{child_status}-{suffix}")).loop_id
    _advance_to_running(service, loop_id)
    identity = service.report(loop_id).iterations[0].identity
    outcome = ChildRunOutcome(
        run_id=identity.child_run_id,
        task_id=identity.child_task_id,
        status=child_status,
        terminal_event_id=f"terminal-{child_status}",
        candidate_state_ref=candidate_state_ref,
        evidence_refs=(f"event://terminal-{child_status}",),
    )

    assert _advance(service, loop_id, outcome) is True

    report = service.report(loop_id)
    event_types = [event.type for event in store.read_all(run_id=loop_id)]
    assert report.iterations[0].status == "failed"
    assert child_status in (report.iterations[0].failure_reason or "")
    assert "engineering.iteration.failed" in event_types
    assert "engineering.iteration.completed" not in event_types
    assert "engineering.evaluation.completed" not in event_types
    if candidate_state_ref is None:
        assert "candidate snapshot absent" in (report.iterations[0].failure_reason or "")

    assert _advance(service, loop_id, outcome) is True
    terminal = service.report(loop_id)
    terminal_event_types = [event.type for event in store.read_all(run_id=loop_id)]
    assert terminal.status == "blocked"
    assert terminal.active_state_ref == "snapshot://initial"
    assert "engineering.evaluation.completed" not in terminal_event_types
    assert "engineering.decision.recorded" not in terminal_event_types
    assert "engineering.loop.completed" not in terminal_event_types
    assert _advance(service, loop_id, outcome) is False
    if candidate_state_ref is None:
        assert "candidate snapshot absent" in (terminal.terminal_reason or "")


def test_succeeded_child_requires_candidate_snapshot() -> None:
    with pytest.raises(ValueError, match="requires candidate_state_ref"):
        ChildRunOutcome(
            run_id="run-success-without-snapshot",
            task_id="task-success-without-snapshot",
            status="succeeded",
            terminal_event_id="terminal-success-without-snapshot",
            candidate_state_ref=None,
            evidence_refs=("event://terminal-success-without-snapshot",),
        )


def test_legacy_unsuccessful_completion_is_projected_failed_and_never_evaluated(
    tmp_path,
) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    service = EngineeringLoopService(store)
    loop_id = service.create(_request("legacy-failed-completion")).loop_id
    _advance_to_running(service, loop_id)
    identity = service.report(loop_id).iterations[0].identity
    legacy_outcome = ChildRunOutcome(
        run_id=identity.child_run_id,
        task_id=identity.child_task_id,
        status="failed",
        terminal_event_id="legacy-terminal-failed",
        candidate_state_ref=None,
        evidence_refs=("event://legacy-terminal-failed",),
    )
    store.append(
        Event(
            id="legacy-invalid-iteration-completed",
            run_id=loop_id,
            task_id=loop_id,
            type="engineering.iteration.completed",
            source="test.legacy",
            payload={"iteration": 1, "outcome": legacy_outcome.model_dump(mode="json")},
        )
    )

    iteration = service.report(loop_id).iterations[0]
    assert iteration.status == "failed"
    assert "legacy completion" in (iteration.failure_reason or "")
    assert _advance(service, loop_id, legacy_outcome) is True
    assert service.report(loop_id).status == "blocked"
    assert not store.read_all(
        run_id=loop_id,
        event_type="engineering.evaluation.completed",
    )


class RecordingEventStore(SQLiteEventStore):
    def __init__(self, path) -> None:
        super().__init__(path)
        self.read_queries: list[tuple[str | None, str | None]] = []

    def read_all(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        event_type: str | None = None,
    ) -> list[Event]:
        self.read_queries.append((run_id, event_type))
        return super().read_all(task_id=task_id, run_id=run_id, event_type=event_type)


def test_list_reports_discovers_loops_through_created_event_index(tmp_path) -> None:
    store = RecordingEventStore(tmp_path / "events.db")
    service = EngineeringLoopService(store)
    loop_id = service.create(_request("indexed-loop-discovery")).loop_id
    store.read_queries.clear()

    reports = service.list_reports()

    assert [report.loop_id for report in reports] == [loop_id]
    assert (None, "engineering.loop.created") in store.read_queries
    assert (None, None) not in store.read_queries
