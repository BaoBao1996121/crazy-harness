from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event


with TemporaryDirectory() as tmp:
    path = Path(tmp) / "eval.db"
    store = SQLiteEventStore(path)
    store.append(Event(run_id="run_team", task_id="task_team", type="run.created", source="spike", payload={"execution_mode": "team", "task_pack": "repo-maintainer"}))
    reopened = SQLiteEventStore(path)
    created = next(e for e in reopened.read_all(run_id="run_team") if e.type == "run.created")
    registry = {"repo-maintainer": object()}
    assert registry[str(created.payload["task_pack"])] is registry["repo-maintainer"]
print("team task-pack recovery key: ok")
