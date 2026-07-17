from tempfile import TemporaryDirectory
from pathlib import Path

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event

with TemporaryDirectory() as root:
    path = Path(root) / "facts.db"
    SQLiteEventStore(path).append(Event(run_id="r", task_id="t", type="capability.manifest.compiled", source="spike", payload={"disclosed_names": ["repo.read"]}))
    event = SQLiteEventStore(path).read_all(run_id="r")[-1]
    assert event.payload["disclosed_names"] == ["repo.read"]
print("capability manifest survives SQLite reopen: ok")
