import json
from tempfile import TemporaryDirectory
from pathlib import Path

from crazy_harness.core.events import Event, EventLog

with TemporaryDirectory() as directory:
    path = Path(directory) / "events.jsonl"
    log = EventLog(path)
    output = json.dumps({"name": "repo-review", "body": "Inspect, test, report.", "body_hash": "abc"})
    log.append(Event(run_id="r1", task_id="t1", type="tool.completed", source="agent", payload={"result": {"name": "skill.activate", "status": "ok", "output": output}}))
    reopened = EventLog(path).read_all(task_id="t1")[-1]
    assert json.loads(reopened.payload["result"]["output"])["body"] == "Inspect, test, report."
print("skill_activation_reopen=ok")
