from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event

with TemporaryDirectory() as root:
    store = SQLiteEventStore(Path(root) / "control.db")
    created = Event(run_id="run-1", task_id="task-1", type="run.created", source="spike")
    store.append(created)
    active = store.claim_work(claim_keys=("agent-run:run-1",), owner_id="active-turn", ttl_seconds=5, run_id="run-1")
    store.append(Event(run_id="run-1", task_id="task-1", type="run.pause.requested", source="spike", payload={"request_id": "pause-1"}))
    blocked = store.claim_work(claim_keys=("agent-run:run-1:new",), owner_id="new-turn", ttl_seconds=5, run_id="run-1")
    assert active and store.list_work_claims(state="active") and blocked is None
print("PASS: pausing preserves the active claim but blocks every new claim")
