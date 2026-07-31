from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event

with TemporaryDirectory() as root:
    store = SQLiteEventStore(Path(root) / "control.db")
    event = Event(
        id="control-1", run_id="run-1", task_id="task-1",
        type="run.pause.requested", source="spike", payload={"request_id": "p1"},
    )
    assert store.append(event) == store.append(event)
    assert len(store.read_all(run_id="run-1")) == 1
print("PASS: deterministic control events converge")
