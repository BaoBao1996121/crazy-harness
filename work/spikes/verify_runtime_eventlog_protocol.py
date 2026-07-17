from crazy_harness.core.events import Event
from crazy_harness.core.runtime import CooperativeScheduler, DurableMailbox

class MemoryLog:
    def __init__(self): self.events = []
    def append(self, event): return self.events.append(event) or event
    def read_all(self, *, task_id=None): return [e for e in self.events if task_id is None or e.task_id == task_id]
    def last(self, *, task_id=None):
        return events[-1] if (events := self.read_all(task_id=task_id)) else None

log = MemoryLog()
seed = log.append(Event(run_id="r1", task_id="t1", type="task.created", source="spike", payload={}))
mailbox = DurableMailbox("agent-a", log)
mailbox.send(seed, delivery_id="d1")
scheduler = CooperativeScheduler(log)
scheduler.register("agent-a", mailbox, lambda delivery: None)
assert scheduler.wake("agent-a") and scheduler.run_once() and mailbox.peek() is None
print("runtime_eventlog_protocol=ok")
