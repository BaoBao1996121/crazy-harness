from pathlib import Path

from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.taskpacks import RepoMaintainerTeamTaskPack


def test_repo_maintainer_team_contract_uses_role_specific_stages(tmp_path):
    pack = RepoMaintainerTeamTaskPack(tmp_path)

    contract = pack.team_contract()

    assert [stage.stage_id for stage in contract.stages] == [
        "inspect",
        "repair",
        "review",
    ]
    assert pack.stage("repair").depends_on == ("inspect",)
    assert pack.stage("review").depends_on == ("repair",)
    assert pack.stage("repair").assignment_contract is not None
    assert pack.stage("repair").assignment_contract.evidence_requirements == (
        "repo.write",
        "test.run",
        "repo.diff",
    )


def test_team_repo_maintainer_runs_the_same_real_workspace_task(tmp_path):
    runtime = ResidentRuntime(tmp_path)

    created = runtime.submit_task(
        TaskRequest(
            title="Paired repo repair",
            brief="Repair the clamp implementation without changing tests.",
            model_mode="scripted",
            execution_mode="team",
            task_pack="repo-maintainer",
        )
    )
    runtime.run_until_idle(max_steps=200)

    run = runtime.store.projection("run", created.run_id)
    events = runtime.store.read_all(run_id=created.run_id)
    created_event = next(event for event in events if event.type == "run.created")
    workspace = Path(str(created_event.payload["workspace_path"]))

    assert run is not None
    assert run["status"] == "succeeded"
    assert created_event.payload["task_pack"] == "repo-maintainer"
    assert (workspace / "calculator.py").read_text(encoding="utf-8").startswith(
        "def clamp"
    )
    assert "return max(lower, min(value, upper))" in (
        workspace / "calculator.py"
    ).read_text(encoding="utf-8")
    assert [
        event.type
        for event in events
        if event.type in {"evidence.recorded", "artifact.recorded", "review.recorded"}
    ] == ["evidence.recorded", "artifact.recorded", "review.recorded"]
    assert any(event.type == "a2a.peer.requested" for event in events)
    assert any(event.type == "completion.gate.passed" for event in events)


def test_team_repo_task_pack_is_recovered_from_persisted_run_metadata(tmp_path):
    first = ResidentRuntime(tmp_path)
    created = first.submit_task(
        TaskRequest(
            title="Recoverable team repair",
            brief="Repair the fixture.",
            execution_mode="team",
            task_pack="repo-maintainer",
        )
    )

    reopened = ResidentRuntime(tmp_path)

    assert reopened.team_task_pack_for(created.run_id).task_pack_id == (
        "repo-maintainer"
    )
