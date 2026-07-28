from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event
with TemporaryDirectory() as raw:
    store = SQLiteEventStore(Path(raw) / "events.db")
    event = Event(
        id="iteration-demo-child-linked",
        run_id="loop-demo",
        task_id="loop-demo",
        type="engineering.iteration.child.linked",
        source="spike",
        payload={"iteration": 1, "child_run_id": "run-demo"},
    )
    store.append(event)
    store.append(event)
    assert [item.id for item in store.read_all(run_id="loop-demo")] == [event.id]
    print("PASS: deterministic parent-child event replay commits once")
