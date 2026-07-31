from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.core.checkpoints import WorkspaceSnapshotStore
with TemporaryDirectory() as raw:
    root = Path(raw)
    active = root / "active"
    active.mkdir()
    (active / "value.txt").write_text("v1", encoding="utf-8")
    store = WorkspaceSnapshotStore(root / "objects")
    parent = store.create(active)
    candidate = root / "candidate"
    store.restore(parent.object_id, candidate)
    (candidate / "value.txt").write_text("v2", encoding="utf-8")
    child = store.create(candidate)
    assert child.object_id != parent.object_id
    assert store.verify(parent.object_id).object_id == parent.object_id
    print("PASS: immutable parent and candidate snapshots form a lineage")
