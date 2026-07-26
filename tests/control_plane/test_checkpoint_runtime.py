from pathlib import Path

import pytest

from crazy_harness.control_plane.checkpoints import (
    CheckpointCreateRequest,
    CheckpointRestoreRequest,
)
from crazy_harness.control_plane.kernel import InjectedKernelCrash
from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest


def _request() -> TaskRequest:
    return TaskRequest(
        title="Repair from a checkpoint",
        brief="Repair the implementation and prove it with tests.",
        execution_mode="single",
        model_mode="scripted",
        task_pack="repo-maintainer",
    )


def test_checkpoint_barrier_blocks_dispatch_during_snapshot(tmp_path: Path, monkeypatch):
    runtime = ResidentRuntime(tmp_path)
    source = runtime.submit_task(_request())
    original_create = runtime.checkpoint_snapshots.create

    def create_while_probing_dispatch(workspace: Path):
        assert runtime.scheduler.run_once() is False
        return original_create(workspace)

    monkeypatch.setattr(runtime.checkpoint_snapshots, "create", create_while_probing_dispatch)
    checkpoint = runtime.create_checkpoint(
        source.run_id,
        CheckpointCreateRequest(request_id="barrier-checkpoint"),
    )

    event_types = [event.type for event in runtime.store.read_all(run_id=source.run_id)]
    assert event_types.index("checkpoint.barrier.acquired") < event_types.index(
        "checkpoint.requested"
    )
    assert event_types.index("checkpoint.committed") < event_types.index(
        "checkpoint.barrier.released"
    )
    assert checkpoint.source.event_id
    assert runtime.scheduler.run_once() is True


def test_fork_restore_prepares_verified_workspace_before_releasing_new_run(tmp_path: Path):
    runtime = ResidentRuntime(tmp_path)
    source = runtime.submit_task(_request())
    for _ in range(4):
        assert runtime.scheduler.run_once() is True

    checkpoint = runtime.create_checkpoint(
        source.run_id,
        CheckpointCreateRequest(request_id="checkpoint-after-write", label="after write"),
    )
    assert checkpoint.source.phase == "result_recording"
    restored = runtime.restore_checkpoint(
        checkpoint.checkpoint_id,
        CheckpointRestoreRequest(request_id="restore-once"),
    )
    repeated = runtime.restore_checkpoint(
        checkpoint.checkpoint_id,
        CheckpointRestoreRequest(request_id="restore-once"),
    )

    assert restored == repeated
    assert restored.run_id != source.run_id
    source_created = next(
        event for event in runtime.store.read_all(run_id=source.run_id) if event.type == "run.created"
    )
    restored_events = runtime.store.read_all(run_id=restored.run_id)
    restored_created = next(event for event in restored_events if event.type == "run.created")
    restored_workspace = Path(restored_created.payload["workspace_path"])
    source_workspace = Path(source_created.payload["workspace_path"])
    assert (restored_workspace / "calculator.py").read_bytes() == (
        source_workspace / "calculator.py"
    ).read_bytes()
    assert restored_created.payload["restored_from_checkpoint_id"] == checkpoint.checkpoint_id
    assert not any(event.type == "model.completed" for event in restored_events)
    event_types = [event.type for event in restored_events]
    assert event_types.index("checkpoint.restore.committed") < event_types.index(
        "mailbox.delivery.sent"
    )
    capsule = next(
        event.payload
        for event in restored_events
        if event.type == "checkpoint.restore.committed"
    )
    assert capsule["source_event_id"] == checkpoint.source.event_id
    assert capsule["context_policy"] == "replan_from_verified_facts"

    runtime.run_until_idle(max_steps=120)
    assert runtime.snapshot(restored.run_id)["run"]["status"] == "succeeded"


@pytest.mark.parametrize(
    ("fault_point", "event_before_crash"),
    [
        ("after_restore_workspace", None),
        ("after_restore_committed", "checkpoint.restore.committed"),
    ],
)
def test_fork_restore_retries_same_run_after_crash(
    tmp_path: Path,
    fault_point: str,
    event_before_crash: str | None,
):
    runtime = ResidentRuntime(tmp_path)
    source = runtime.submit_task(_request())
    for _ in range(4):
        assert runtime.scheduler.run_once() is True
    checkpoint = runtime.create_checkpoint(
        source.run_id,
        CheckpointCreateRequest(request_id=f"checkpoint-{fault_point}"),
    )
    restore_request = CheckpointRestoreRequest(request_id=f"restore-{fault_point}")
    runtime.arm_fault(fault_point)

    with pytest.raises(InjectedKernelCrash, match=fault_point):
        runtime.restore_checkpoint(checkpoint.checkpoint_id, restore_request)

    expected_run_id = runtime.checkpoint_restore_identity(
        checkpoint.checkpoint_id,
        restore_request.request_id,
    ).run_id
    crashed_events = runtime.store.read_all(run_id=expected_run_id)
    assert any(event.type == event_before_crash for event in crashed_events) is bool(
        event_before_crash
    )
    assert not any(event.type == "mailbox.delivery.sent" for event in crashed_events)

    restarted = ResidentRuntime(tmp_path)
    restored = restarted.restore_checkpoint(checkpoint.checkpoint_id, restore_request)
    restored_events = restarted.store.read_all(run_id=restored.run_id)

    assert restored.run_id == expected_run_id
    assert sum(event.type == "run.created" for event in restored_events) == 1
    assert sum(
        event.type == "checkpoint.restore.committed" for event in restored_events
    ) == 1
    assert sum(event.type == "mailbox.delivery.sent" for event in restored_events) == 1
