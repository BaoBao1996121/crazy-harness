from __future__ import annotations

import os
from pathlib import Path

import pytest

from crazy_harness.core.checkpoints import (
    CheckpointIntegrityError,
    CheckpointPolicyError,
    WorkspaceSnapshotStore,
)


def _write_workspace(root: Path) -> None:
    (root / "nested").mkdir(parents=True)
    (root / "README.md").write_bytes(b"line one\r\nline two\n")
    (root / "module.py").write_bytes(b"VALUE = 1\n")
    (root / "nested" / "payload.bin").write_bytes(bytes(range(64)))


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_snapshot_is_content_addressed_and_restores_exact_bytes(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_workspace(workspace)
    store = WorkspaceSnapshotStore(tmp_path / "checkpoint_objects")

    first = store.create(workspace)
    second = store.create(workspace)
    restored = tmp_path / "restored"
    store.restore(first, restored)

    assert first == second
    assert first.file_count == 3
    assert first.total_bytes == sum(len(value) for value in _tree(workspace).values())
    assert len(first.object_id) == 64
    assert _tree(restored) == _tree(workspace)
    assert store.verify(first) == first


def test_changed_content_produces_a_new_snapshot_identity(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_workspace(workspace)
    store = WorkspaceSnapshotStore(tmp_path / "checkpoint_objects")

    before = store.create(workspace)
    (workspace / "README.md").write_text("changed\n", encoding="utf-8")
    after = store.create(workspace)

    assert after.object_id != before.object_id
    assert after.manifest_sha256 != before.manifest_sha256


def test_tampered_snapshot_object_fails_closed(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_workspace(workspace)
    store = WorkspaceSnapshotStore(tmp_path / "checkpoint_objects")
    snapshot = store.create(workspace)
    store.archive_path(snapshot.object_id).write_bytes(b"tampered")

    with pytest.raises(CheckpointIntegrityError, match="archive hash"):
        store.verify(snapshot)
    with pytest.raises(CheckpointIntegrityError):
        store.restore(snapshot, tmp_path / "restored")


def test_snapshot_rejects_symbolic_links(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(CheckpointPolicyError, match="symbolic link"):
        WorkspaceSnapshotStore(tmp_path / "objects").create(workspace)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"max_files": 1}, "file count"),
        ({"max_total_bytes": 8}, "byte budget"),
    ],
)
def test_snapshot_enforces_file_and_byte_budgets(
    tmp_path: Path,
    kwargs: dict[str, int],
    expected: str,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_workspace(workspace)

    with pytest.raises(CheckpointPolicyError, match=expected):
        WorkspaceSnapshotStore(tmp_path / "objects", **kwargs).create(workspace)


def test_restore_refuses_an_existing_target(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_workspace(workspace)
    store = WorkspaceSnapshotStore(tmp_path / "objects")
    snapshot = store.create(workspace)
    target = tmp_path / "existing"
    target.mkdir()

    with pytest.raises(CheckpointPolicyError, match="already exists"):
        store.restore(snapshot, target)
