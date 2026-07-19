from tempfile import TemporaryDirectory
from pathlib import Path

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event
from crazy_harness.core.runtime import DurableMailbox

with TemporaryDirectory() as root:
    path = Path(root) / "events.db"
    event = Event(run_id="run-1", task_id="task-1", type="assignment.created", source="spike")
    DurableMailbox("worker", SQLiteEventStore(path)).send(event, delivery_id="pair:prepared")
    recovered = DurableMailbox("worker", SQLiteEventStore(path)).peek()
    assert recovered is not None and recovered.event == event
print("PASS prestart delivery survives mailbox reconstruction")
