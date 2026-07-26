from tempfile import TemporaryDirectory

from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.core.events import Event

with TemporaryDirectory() as root:
    runtime = ResidentRuntime(root)
    created = runtime.submit_task(TaskRequest(
        title="spike", brief="spike", execution_mode="single"
    ))
    runtime.store.append(Event(
        run_id=created.run_id, task_id=created.task_id,
        type="run.failure.requested", source="spike", payload={"reason": "stop"},
    ))
    runtime._reconcile_routes()
    assert runtime.store.projection("run", created.run_id)["status"] == "failed"
    assert not [e for e in runtime.store.read_all(run_id=created.run_id) if e.type == "model.requested"]
    print("PASS: failure request becomes a write barrier before dispatch")
