from tempfile import TemporaryDirectory
from pathlib import Path

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event
from crazy_harness.core.runtime import DurableMailbox

with TemporaryDirectory() as root:
    store = SQLiteEventStore(Path(root) / "events.db")
    mailbox = DurableMailbox("worker", store)
    event = Event(run_id="run-1", task_id="task-1", type="assignment.created", source="spike")
    mailbox.send(event, delivery_id="pair:single")
    mailbox.ack("pair:single")
    mailbox.send(event, delivery_id="pair:single")
    assert mailbox.peek() is None
print("PASS repeated release does not resurrect acked work")
