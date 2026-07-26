from __future__ import annotations

from pathlib import Path

import pytest

from crazy_harness.control_plane.checkpoints import (
    CheckpointCreateRequest,
    CheckpointIdempotencyConflict,
    CheckpointRestoreBlocked,
    CheckpointService,
    UnsafeCheckpointBoundary,
)
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.checkpoints import CheckpointIntegrityError, WorkspaceSnapshotStore
from crazy_harness.core.events import Event


def _append(
    store: SQLiteEventStore,
    event_type: str,
    *,
    payload: dict | None = None,
    causation_id: str | None = None,
) -> Event:
    return store.append(
        Event(
            run_id="run-1",
            task_id="task-1",
            type=event_type,
            source="test",
            payload=payload or {},
            causation_id=causation_id,
        )
    )


def _service(tmp_path: Path) -> tuple[CheckpointService, SQLiteEventStore, Path]:
    store = SQLiteEventStore(tmp_path / "control.db")
    workspace = tmp_path / "workspaces" / "run-1"
    workspace.mkdir(parents=True)
    (workspace / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    created = _append(
        store,
        "run.created",
        payload={
            "title": "Checkpoint demo",
            "brief": "Continue from verified state.",
            "execution_mode": "single",
            "model_mode": "scripted",
            "task_pack": "repo-maintainer",
            "workspace_path": str(workspace),
            "baseline_path": str(tmp_path / "baselines" / "run-1"),
            "fixture_hash": "a" * 64,
        },
    )
    _append(
        store,
        "assignment.created",
        payload={
            "assignment_id": "task-1",
            "agent_id": "generalist",
            "goal": "repair",
            "contract": {"version": 1, "goal": "repair", "exit_criteria": ["tests pass"]},
        },
        causation_id=created.id,
    )
    _append(store, "runtime.turn.ready", payload={"turn_id": "turn-1", "phase": "observing"})
    return (
        CheckpointService(
            store,
            WorkspaceSnapshotStore(tmp_path / "checkpoint_objects"),
            artifact_root=tmp_path / "artifacts",
        ),
        store,
        workspace,
    )


def test_checkpoint_create_is_idempotent_and_read_is_pure(tmp_path: Path):
    service, store, _ = _service(tmp_path)
    request = CheckpointCreateRequest(request_id="checkpoint-request-1", label="after inspect")

    first = service.create("run-1", request)
    event_count = len(store.read_all(run_id="run-1"))
    second = service.create("run-1", request)
    fetched = service.contract(first.checkpoint_id)
    listed = service.list_for_run("run-1")

    assert first == second == fetched == listed[0]
    assert len(store.read_all(run_id="run-1")) == event_count
    assert first.source.event_id
    assert first.source.event_count == 3
    assert first.workspace.file_count == 1
    assert first.state_refs.run_created_event_id
    assert first.state_refs.assignment_event_id
    assert first.restore_policy == "fork_only"
    serialized = first.model_dump(mode="json")
    assert serialized["baseline_identity"] == f"fixture_sha256:{'a' * 64}"
    assert "baseline_path" not in serialized
    assert [event.type for event in store.read_all(run_id="run-1")][-2:] == [
        "checkpoint.requested",
        "checkpoint.committed",
    ]


def test_checkpoint_request_id_rejects_payload_drift(tmp_path: Path):
    service, _, _ = _service(tmp_path)
    service.create(
        "run-1",
        CheckpointCreateRequest(request_id="same-request", label="first"),
    )

    with pytest.raises(CheckpointIdempotencyConflict):
        service.create(
            "run-1",
            CheckpointCreateRequest(request_id="same-request", label="changed"),
        )


def test_checkpoint_recovers_after_requested_was_persisted(tmp_path: Path):
    service, store, _ = _service(tmp_path)
    request = CheckpointCreateRequest(request_id="crash-request", label="crash window")

    def crash(stage: str) -> None:
        if stage == "after_checkpoint_requested":
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        service.create("run-1", request, fault_injector=crash)
    assert [event.type for event in store.read_all(run_id="run-1")][-1] == "checkpoint.requested"

    recovered = CheckpointService(
        SQLiteEventStore(tmp_path / "control.db"),
        WorkspaceSnapshotStore(tmp_path / "checkpoint_objects"),
        artifact_root=tmp_path / "artifacts",
    ).create("run-1", request)

    assert recovered == service.contract(recovered.checkpoint_id)
    assert [event.type for event in store.read_all(run_id="run-1")].count("checkpoint.committed") == 1


def test_later_events_do_not_change_the_frozen_prefix(tmp_path: Path):
    service, store, _ = _service(tmp_path)
    checkpoint = service.create(
        "run-1",
        CheckpointCreateRequest(request_id="prefix-request"),
    )
    _append(store, "later.event", payload={"value": 2})

    assert service.validate_restore(checkpoint.checkpoint_id) == checkpoint


def test_unresolved_operation_rejects_checkpoint_creation(tmp_path: Path):
    service, store, _ = _service(tmp_path)
    _append(
        store,
        "operation.started",
        payload={
            "turn_id": "turn-2",
            "operation_id": "op-1",
            "tool_name": "external.write",
            "side_effect_level": "unknown",
        },
    )

    with pytest.raises(UnsafeCheckpointBoundary, match="unresolved operation"):
        service.create("run-1", CheckpointCreateRequest(request_id="unsafe-request"))


def test_unresolved_tool_request_rejects_checkpoint_even_with_operation_terminal(tmp_path: Path):
    service, store, _ = _service(tmp_path)
    started = _append(
        store,
        "operation.started",
        payload={
            "turn_id": "turn-2",
            "operation_id": "op-partial",
            "tool_name": "repo.write",
            "side_effect_level": "workspace_write",
        },
    )
    _append(
        store,
        "tool.requested",
        payload={"turn_id": "turn-2", "operation_id": "op-partial", "tool_name": "repo.write"},
        causation_id=started.id,
    )
    _append(
        store,
        "operation.completed",
        payload={"turn_id": "turn-2", "operation_id": "op-partial"},
        causation_id=started.id,
    )

    with pytest.raises(UnsafeCheckpointBoundary, match="unresolved tool request"):
        service.create("run-1", CheckpointCreateRequest(request_id="partial-tool-request"))


def test_unknown_effect_is_persisted_but_blocks_restore(tmp_path: Path):
    service, store, _ = _service(tmp_path)
    started = _append(
        store,
        "operation.started",
        payload={
            "turn_id": "turn-2",
            "operation_id": "op-unknown",
            "tool_name": "external.charge",
            "side_effect_level": "unknown",
        },
    )
    _append(
        store,
        "operation.unknown",
        payload={"turn_id": "turn-2", "operation_id": "op-unknown", "reason": "timeout"},
        causation_id=started.id,
    )
    _append(store, "runtime.turn.ready", payload={"turn_id": "turn-2", "phase": "waiting"})
    checkpoint = service.create(
        "run-1",
        CheckpointCreateRequest(request_id="unknown-effect-request"),
    )

    assert checkpoint.effects.restore_blockers == ("operation:op-unknown:unknown",)
    with pytest.raises(CheckpointRestoreBlocked, match="op-unknown"):
        service.validate_restore(checkpoint.checkpoint_id)


def test_artifact_is_hashed_and_tampering_blocks_restore(tmp_path: Path):
    service, store, _ = _service(tmp_path)
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    artifact = artifact_store.write_text("evidence", "verified evidence")
    _append(
        store,
        "artifact.created",
        payload={"artifact_ref": artifact.model_dump(mode="json")},
    )
    _append(store, "runtime.turn.ready", payload={"turn_id": "turn-2", "phase": "observing"})
    checkpoint = service.create(
        "run-1",
        CheckpointCreateRequest(request_id="artifact-request"),
    )
    Path(artifact.uri).write_text("tampered", encoding="utf-8")

    assert checkpoint.state_refs.artifacts[0].sha256
    assert checkpoint.state_refs.artifacts[0].artifact_id == Path(artifact.uri).name
    assert "uri" not in checkpoint.state_refs.artifacts[0].model_dump(mode="json")
    with pytest.raises(CheckpointIntegrityError, match="artifact hash"):
        service.validate_restore(checkpoint.checkpoint_id)


def test_checkpoint_rejects_artifact_reference_outside_managed_root(tmp_path: Path):
    service, store, _ = _service(tmp_path)
    unmanaged = tmp_path / "private.txt"
    unmanaged.write_text("must not be exposed", encoding="utf-8")
    _append(
        store,
        "artifact.created",
        payload={
            "artifact_ref": {
                "uri": str(unmanaged),
                "kind": "evidence",
                "summary": "untrusted path",
            }
        },
    )

    with pytest.raises(CheckpointIntegrityError, match="outside managed root"):
        service.create(
            "run-1",
            CheckpointCreateRequest(request_id="unmanaged-artifact-request"),
        )
