from decimal import Decimal

import pytest

from crazy_harness.control_plane.engineering_loops import EngineeringLoopRequest
from crazy_harness.control_plane.runtime import ResidentRuntime
from crazy_harness.core.engineering_loops import (
    EngineeringLoopBudget,
    MetricContract,
    MetricDirection,
    WorkerProfile,
)


def _request(request_id: str) -> EngineeringLoopRequest:
    return EngineeringLoopRequest(
        request_id=request_id,
        title="Authorized repository quality loop",
        objective="Improve the disposable repository under a frozen LoopPack grant",
        exit_criteria=("quality_score reaches 1",),
        loop_pack="repo-quality",
        worker=WorkerProfile(
            execution_mode="single",
            model_mode="scripted",
            task_pack="repo-quality",
        ),
        metric=MetricContract(
            name="quality_score",
            direction=MetricDirection.MAXIMIZE,
            target=Decimal("1"),
            evaluator_version="repo-quality-v1",
        ),
        budget=EngineeringLoopBudget(max_iterations=3),
        permissions=("disposable_workspace_write", "run_bounded_checks"),
        initial_state_ref="loop-pack://initial",
    )


@pytest.mark.parametrize("drift", ["identity", "version", "permissions"])
def test_persisted_loop_fails_closed_when_loop_pack_authorization_drifts(
    tmp_path,
    drift,
):
    data_dir = tmp_path / drift
    runtime = ResidentRuntime(data_dir)
    created = runtime.create_engineering_loop(_request(f"pack-recovery-{drift}"))

    restarted = ResidentRuntime(data_dir)
    current_pack = restarted.engineering_loop_packs["repo-quality"]
    if drift == "identity":
        current_pack.loop_pack_id = "repo-quality-replacement"
    elif drift == "version":
        current_pack.pack_version = "repo-quality-v2"
    else:
        current_pack.required_permissions = current_pack.required_permissions | {
            "external_network_write"
        }

    with pytest.raises(RuntimeError, match="LoopPack authorization"):
        restarted._advance_engineering_control()

    events = restarted.store.read_all(run_id=created.loop_id)
    assert not any(event.type == "engineering.iteration.planned" for event in events)
