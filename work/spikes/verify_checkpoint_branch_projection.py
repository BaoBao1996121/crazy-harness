from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event

with TemporaryDirectory() as root:
    store = SQLiteEventStore(Path(root) / "control.db")
    store.append(Event(run_id="child", task_id="task-child", type="checkpoint.restore.committed", source="spike", payload={"checkpoint_id": "cp-1", "source_run_id": "parent", "source_event_id": "event-1", "source_turn_id": "turn-2"}))
    relation = next(event for event in store.read_all() if event.type == "checkpoint.restore.committed")
    assert (relation.payload["source_run_id"], relation.run_id) == ("parent", "child")
    assert relation.payload["checkpoint_id"] == "cp-1"
print("PASS: restore facts contain durable parent-child lineage")
