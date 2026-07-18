from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from pathlib import Path

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event

with TemporaryDirectory() as directory:
    path = Path(directory) / "events.db"
    store = SQLiteEventStore(path)
    expires_at = datetime.now(UTC) + timedelta(seconds=30)
    store.append(Event(run_id="r", task_id="t", type="assignment.lease.acquired", source="kernel", payload={"assignment_id": "a", "lease_id": "l", "expires_at": expires_at.isoformat()}))
    rebuilt = SQLiteEventStore(path).read_all(run_id="r")
    lease = next(event for event in rebuilt if event.type == "assignment.lease.acquired")
    restored = datetime.fromisoformat(lease.payload["expires_at"])
    assert restored == expires_at
    assert restored.tzinfo is not None
print("lease_event_rebuild=pass")
