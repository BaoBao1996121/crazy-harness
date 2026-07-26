import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event

def digest(events):
    payload = [event.model_dump(mode="json") for event in events]
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

with TemporaryDirectory() as root:
    path = Path(root) / "events.db"
    store = SQLiteEventStore(path)
    store.append(Event(run_id="run-1", task_id="task-1", type="run.created", source="spike"))
    count, expected = len(store.read_all(run_id="run-1")), digest(store.read_all(run_id="run-1"))
    store.append(Event(run_id="run-1", task_id="task-1", type="later.event", source="spike"))
    assert digest(SQLiteEventStore(path).read_all(run_id="run-1")[:count]) == expected
print("PASS checkpoint event prefix")
