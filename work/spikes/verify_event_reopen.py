from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.core.events import Event, EventLog


with TemporaryDirectory() as root:
    path = Path(root) / "events.jsonl"
    event = Event(run_id="r1", task_id="t1", type="model.completed", source="model", payload={"content": "ok"})
    EventLog(path).append(event)
    restored = EventLog(path).read_all()
    assert restored == [event]
    print("event_reopen=ok")
