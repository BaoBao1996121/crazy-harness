from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.runtime import ResidentRuntime

request = run_path("tests/control_plane/test_engineering_loop_runtime.py")["_repo_quality_request"]
with TemporaryDirectory() as raw:
    runtime = ResidentRuntime(Path(raw))
    created = runtime.create_engineering_loop(request())
    runtime.run_until_idle(max_steps=500)
    report = runtime.engineering_loop(created.loop_id)
    child_ids = [item.identity.child_run_id for item in report.iterations]
    assert report.status == "completed" and len(child_ids) == 2
    assert all(runtime.agent_run_view(run_id).identity.run_id == run_id for run_id in child_ids)
    print("PASS: parent iteration links drill into canonical AgentRuns")
