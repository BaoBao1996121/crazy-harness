from tempfile import TemporaryDirectory

from crazy_harness.control_plane.paired_evals import PairedEvalRequest
from crazy_harness.control_plane.runtime import ResidentRuntime

with TemporaryDirectory() as root:
    runtime = ResidentRuntime(root)
    runtime.eval_service.fault_injector = lambda point: (
        (_ for _ in ()).throw(KeyboardInterrupt())
        if point == "after_eval_arm_prepared:single" else None
    )
    try:
        runtime.create_paired_eval(PairedEvalRequest(
            request_id="spike-orphan", title="spike", brief="spike"
        ))
    except KeyboardInterrupt:
        pass
    arms = [e for e in runtime.store.read_all() if e.type == "eval.arm.created"]
    assert len(arms) == 1 and runtime.store.projection("run", arms[0].payload["run_id"])
    print("PASS: orphan arm identity is durable before Pair Contract")
