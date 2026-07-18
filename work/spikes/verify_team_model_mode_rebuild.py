from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event

with TemporaryDirectory() as root:
    path = Path(root) / "team.db"
    store = SQLiteEventStore(path)
    store.append(Event(run_id="run-1", task_id="root", type="run.created", source="spike", payload={"model_mode": "deepseek"}))
    rebuilt = SQLiteEventStore(path).read_all(run_id="run-1")
    assert next(event for event in rebuilt if event.type == "run.created").payload["model_mode"] == "deepseek"
print("team_model_mode_rebuild=PASS")
