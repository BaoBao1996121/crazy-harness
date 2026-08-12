from __future__ import annotations

from decimal import Decimal

import pytest

from crazy_harness.control_plane.engineering_loops import (
    EngineeringLoopPauseRequest,
    EngineeringLoopRequest,
)
from crazy_harness.control_plane.runtime import ResidentRuntime
from crazy_harness.core.engineering_loops import (
    EngineeringLoopBudget,
    MetricContract,
    MetricDirection,
    WorkerProfile,
)
from crazy_harness.core.events import Event


def _repo_quality_request() -> EngineeringLoopRequest:
    return EngineeringLoopRequest(
        request_id="runtime-repo-quality-loop",
        title="Repository quality climb",
        objective="Repair the repository, then remove the temporary quality marker",
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
        initial_state_ref="loop-pack://initial",
        input_payload={"fixture": "quality-climb-v1"},
    )


def test_repo_quality_loop_runs_two_real_child_agent_runs(tmp_path) -> None:
    runtime = ResidentRuntime(tmp_path)

    created = runtime.create_engineering_loop(_repo_quality_request())
    runtime.run_until_idle(max_steps=200)

    report = runtime.engineering_loop(created.loop_id)
    assert report.status == "completed"
    assert report.active_score == Decimal("1")
    assert [
        item.evaluation.metrics["quality_score"] for item in report.iterations
    ] == [
        Decimal("0.5"),
        Decimal("1"),
    ]
    assert (
        report.iterations[1].candidate.base_state_ref
        == report.iterations[0].outcome.candidate_state_ref
    )

    child_run_ids = [item.identity.child_run_id for item in report.iterations]
    assert len(child_run_ids) == len(set(child_run_ids)) == 2
    for child_run_id in child_run_ids:
        child = runtime.snapshot(child_run_id)
        child_events = runtime.store.read_all(run_id=child_run_id)
        assert child["run"]["status"] == "succeeded"
        assert any(event.type == "model.completed" for event in child_events)
        assert any(
            event.type == "tool.completed"
            and event.payload["result"]["name"] == "test.run"
            for event in child_events
        )
        assert any(event.type == "completion.gate.passed" for event in child_events)
        terminal = next(
            event for event in child_events if event.type == "run.succeeded"
        )
        assert terminal.payload["candidate_state_ref"].startswith("snapshot://")

    restored = tmp_path / "final-restored"
    runtime.checkpoint_snapshots.restore(
        report.active_state_ref.removeprefix("snapshot://"),
        restored,
    )
    source = (restored / "calculator.py").read_text(encoding="utf-8")
    assert "max(lower, min(value, upper))" in source
    assert "QUALITY-TODO" not in source


def test_repo_quality_loop_rejects_missing_pack_permissions(tmp_path) -> None:
    runtime = ResidentRuntime(tmp_path)

    with pytest.raises(ValueError, match="permissions"):
        runtime.create_engineering_loop(
            _repo_quality_request().model_copy(update={"permissions": ()})
        )

    assert runtime.engineering_loops() == []


def test_repo_quality_loop_recovers_child_terminal_before_parent_observation(
    tmp_path,
) -> None:
    runtime = ResidentRuntime(tmp_path)
    created = runtime.create_engineering_loop(_repo_quality_request())

    for _ in range(4):
        assert runtime._advance_engineering_control() is True
    running = runtime.engineering_loop(created.loop_id)
    first_child_id = running.iterations[0].identity.child_run_id
    assert running.iterations[0].status == "running"

    # The durable parent link is committed in cycle four. A later cycle may now
    # idempotently prepare/release the child without risking an orphan AgentRun.
    assert runtime._advance_engineering_control() is False

    for _ in range(20):
        child = runtime.store.projection("run", first_child_id)
        if child is not None and child.get("status") == "succeeded":
            break
        assert runtime.scheduler.run_once(
            allowed_run_ids=frozenset({first_child_id})
        )
    else:
        raise AssertionError("first engineering child did not terminate")

    parent_events = runtime.store.read_all(run_id=created.loop_id)
    assert not any(
        event.type == "engineering.iteration.completed" for event in parent_events
    )

    recovered = ResidentRuntime(tmp_path)
    recovered.run_until_idle(max_steps=200)

    report = recovered.engineering_loop(created.loop_id)
    assert report.status == "completed"
    assert len(report.iterations) == 2
    assert report.iterations[0].identity.child_run_id == first_child_id
    assert sum(
        event.type == "engineering.iteration.completed"
        and event.payload.get("iteration") == 1
        for event in recovered.store.read_all(run_id=created.loop_id)
    ) == 1


def test_failed_engineering_child_without_snapshot_blocks_parent_loop(tmp_path) -> None:
    runtime = ResidentRuntime(tmp_path)
    created = runtime.create_engineering_loop(_repo_quality_request())

    for _ in range(4):
        assert runtime._advance_engineering_control() is True
    running = runtime.engineering_loop(created.loop_id)
    identity = running.iterations[0].identity

    # The next parent cycle prepares the child, but no worker step has run yet.
    assert runtime._advance_engineering_control() is False
    child_created = next(
        event
        for event in runtime.store.read_all(run_id=identity.child_run_id)
        if event.type == "run.created"
    )
    runtime.store.append(
        Event(
            run_id=identity.child_run_id,
            task_id=identity.child_task_id,
            type="run.failed",
            source="runtime.single",
            payload={"reason": "injected child failure before candidate snapshot"},
            causation_id=child_created.id,
        )
    )

    assert runtime._advance_engineering_control() is True
    failed = runtime.engineering_loop(created.loop_id)
    assert failed.iterations[0].status == "failed"
    assert "candidate snapshot absent" in failed.iterations[0].failure_reason

    assert runtime._advance_engineering_control() is True
    blocked = runtime.engineering_loop(created.loop_id)
    assert blocked.status == "blocked"
    assert blocked.terminal_reason == failed.iterations[0].failure_reason


def test_restart_repairs_contract_authorization_gap_before_iteration(tmp_path) -> None:
    runtime = ResidentRuntime(tmp_path)
    created = runtime.engineering_loop_service.create(_repo_quality_request())
    assert not any(
        event.type == "engineering.loop_pack.authorized"
        for event in runtime.store.read_all(run_id=created.loop_id)
    )

    restarted = ResidentRuntime(tmp_path)
    assert restarted.advance_engineering_loop(created.loop_id) is True
    events = restarted.store.read_all(run_id=created.loop_id)

    assert sum(event.type == "engineering.loop_pack.authorized" for event in events) == 1
    assert sum(event.type == "engineering.iteration.planned" for event in events) == 1


def test_waiting_engineering_loop_does_not_starve_a_ready_peer(tmp_path) -> None:
    runtime = ResidentRuntime(tmp_path)
    first = runtime.create_engineering_loop(_repo_quality_request())
    second = runtime.create_engineering_loop(
        _repo_quality_request().model_copy(
            update={"request_id": "runtime-repo-quality-peer"}
        )
    )
    for _ in range(4):
        assert runtime.advance_engineering_loop(first.loop_id) is True

    assert runtime.engineering_loop(first.loop_id).iterations[-1].status == "running"
    assert runtime._advance_engineering_control() is True
    assert runtime.engineering_loop(second.loop_id).iterations[0].status == "planned"


def test_pending_pause_settles_after_advance_claim_release_and_restart(tmp_path) -> None:
    runtime = ResidentRuntime(tmp_path)
    created = runtime.create_engineering_loop(_repo_quality_request())
    owner = "test-active-parent-step"
    claims = runtime.store.claim_work(
        claim_keys=(f"engineering-loop-advance:{created.loop_id}",),
        owner_id=owner,
        ttl_seconds=30,
    )
    assert claims is not None

    pausing = runtime.pause_engineering_loop(
        created.loop_id,
        EngineeringLoopPauseRequest(
            request_id="runtime-pause-pending-1",
            reason="pause after the current parent phase",
        ),
    )
    assert pausing.status == "pausing"
    assert pausing.iterations == ()
    assert runtime.store.finish_work_claims(
        claims=claims,
        owner_id=owner,
        state="released",
    )

    restarted = ResidentRuntime(tmp_path)
    assert restarted._reconcile_engineering_loop_controls() is True
    paused = restarted.engineering_loop(created.loop_id)
    assert paused.status == "paused"
    assert paused.iterations == ()
    assert sum(
        event.type == "engineering.loop.paused"
        for event in restarted.store.read_all(run_id=created.loop_id)
    ) == 1
