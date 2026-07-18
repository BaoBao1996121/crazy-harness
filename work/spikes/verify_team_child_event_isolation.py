from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event


with TemporaryDirectory() as directory:
    store = SQLiteEventStore(Path(directory) / "events.db")
    store.append(
        Event(run_id="run", task_id="child-a", type="agent.submitted", source="a")
    )
    store.append(
        Event(run_id="run", task_id="child-b", type="model.requested", source="b")
    )
    assert [event.type for event in store.read_all(task_id="child-a")] == [
        "agent.submitted"
    ]
    assert [event.type for event in store.read_all(task_id="child-b")] == [
        "model.requested"
    ]
    print("team child event isolation: ok")
