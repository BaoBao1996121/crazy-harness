from tempfile import TemporaryDirectory
from pathlib import Path
from crazy_harness.control_plane.kernel import FaultController
from crazy_harness.control_plane.runtime import ResidentScheduler
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.agents import AgentLoop
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.events import Event
from crazy_harness.core.models import FakeModelProvider
from crazy_harness.core.runtime import DurableMailbox
from crazy_harness.core.tools import ToolRegistry
with TemporaryDirectory() as tmp:
    root, store = Path(tmp), SQLiteEventStore(Path(tmp) / "events.db")
    seed = store.append(Event(run_id="r1", task_id="t1", type="task.seeded", source="spike"))
    loop = AgentLoop("worker", FakeModelProvider(['{"type":"stop","reason":"done"}']), store, ArtifactStore(root / "artifacts"), ToolRegistry(), task_id="t1")
    mailbox, scheduler = DurableMailbox("worker", store), ResidentScheduler(store, FaultController())
    mailbox.send(seed, delivery_id="d1")
    scheduler.register("worker", mailbox, lambda _: loop.run_once())
    assert scheduler.run_once() and mailbox.peek() is None and store.last(task_id="t1").type == "runtime.agent.idle"
