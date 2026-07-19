from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event


with TemporaryDirectory() as tmp:
    path, start = Path(tmp) / "trace.db", datetime.now(timezone.utc)
    store = SQLiteEventStore(path)
    for index, kind in enumerate(("run.created", "model.completed", "tool.completed", "run.succeeded")):
        store.append(Event(run_id="run_1", task_id="task_1", type=kind, source="spike", created_at=start + timedelta(milliseconds=index * 10)))
    replay = SQLiteEventStore(path).read_all(run_id="run_1")
    summary = (sum(e.type == "model.completed" for e in replay), sum(e.type == "tool.completed" for e in replay), int((replay[-1].created_at - replay[0].created_at).total_seconds() * 1000))
    assert summary == (1, 1, 30)
print("eval trace replay: ok")
