from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event
from crazy_harness.core.runtime import DurableMailbox

with TemporaryDirectory() as root:
    store = SQLiteEventStore(Path(root) / "events.db")
    event = store.append(Event(run_id="run", task_id="task", type="probe", source="spike"))
    DurableMailbox("worker", store).send(event, delivery_id="delivery-1")
    recovered = DurableMailbox("worker", SQLiteEventStore(store.path))
    assert recovered.peek().delivery_id == "delivery-1"
    recovered.ack("delivery-1")
    assert DurableMailbox("worker", SQLiteEventStore(store.path)).peek() is None
print("unacked_mailbox_recovery=PASS")
