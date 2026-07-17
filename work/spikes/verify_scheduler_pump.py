from pathlib import Path
from tempfile import TemporaryDirectory
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.runtime import CooperativeScheduler, DurableMailbox

with TemporaryDirectory() as root:
    log = EventLog(Path(root) / "events.jsonl")
    event = log.append(Event(run_id="r", task_id="t", type="message", source="test"))
    box = DurableMailbox("agent", log)
    box.send(event)
    scheduler = CooperativeScheduler(log)
    scheduler.register("agent", box, lambda delivery: None)
    assert scheduler.wake("agent") and scheduler.run_once()
    assert box.peek() is None
print("scheduler-pump-ok")
