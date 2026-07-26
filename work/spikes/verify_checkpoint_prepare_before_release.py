from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.paired_evals import EvalRunIdentity
from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest

with TemporaryDirectory() as root:
    runtime = ResidentRuntime(Path(root))
    identity = EvalRunIdentity(run_id="run-restored", task_id="task-restored")
    request = TaskRequest(title="restore", brief="continue", execution_mode="single", task_pack="repo-maintainer")
    runtime._prepare_single_task(request, identity)
    workspace = Path(runtime.store.read_all(run_id=identity.run_id)[0].payload["workspace_path"])
    assert runtime.mailboxes["generalist"].peek() is None
    (workspace / "calculator.py").write_text("RESTORED = True\n", encoding="utf-8")
    runtime._release_prepared_task("single", identity)
    assert runtime.mailboxes["generalist"].peek() is not None
    assert (workspace / "calculator.py").read_text(encoding="utf-8") == "RESTORED = True\n"
print("PASS checkpoint prepare before release")
