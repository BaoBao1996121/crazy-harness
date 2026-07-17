import json
from tempfile import TemporaryDirectory
from pathlib import Path

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.agents import AgentLoop
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.events import Event
from crazy_harness.core.models import FakeModelProvider
from crazy_harness.core.tools import ToolRegistry

with TemporaryDirectory() as tmp:
    root = Path(tmp)
    store = SQLiteEventStore(root / "events.db")
    store.append(Event(run_id="r1", task_id="t1", type="task.seeded", source="spike"))
    loop = AgentLoop("worker", FakeModelProvider([json.dumps({"type": "stop", "reason": "done"})]), store, ArtifactStore(root / "artifacts"), ToolRegistry(), task_id="t1")
    loop.run_once()
    assert store.last(task_id="t1").type == "agent.stopped"
    print("PASS: canonical AgentLoop runs directly on SQLiteEventStore")
