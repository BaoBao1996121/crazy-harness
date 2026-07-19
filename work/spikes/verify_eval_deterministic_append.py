from tempfile import TemporaryDirectory
from pathlib import Path

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event

with TemporaryDirectory() as root:
    store = SQLiteEventStore(Path(root) / "events.db")
    first = Event(id="eval-fixed", run_id="eval-1", task_id="eval-1", type="eval.pair.requested", source="spike")
    persisted = store.append(first)
    replayed = store.append(Event(id="eval-fixed", run_id="eval-1", task_id="eval-1", type="eval.pair.requested", source="spike"))
    assert replayed == persisted
    assert len(store.read_all(run_id="eval-1")) == 1
print("PASS deterministic eval append converges")
