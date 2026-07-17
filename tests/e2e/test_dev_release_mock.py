from pathlib import Path

from crazy_harness.worlds.cicd.world import build_dev_release_runtime


def test_mock_dev_release_runs_to_terminal_report(tmp_path):
    runtime = build_dev_release_runtime(
        mode="mock",
        repo_path=Path("examples/hello-crazy-api").resolve(),
        runs_dir=tmp_path / "runs",
    )

    run_dir = runtime.run()

    events = runtime.event_log.read_all()
    assert events[-1].type == "agent.stopped"
    assert [event.type for event in events].count("tool.completed") == 4
    assert all(len(event.payload["prompt_hash"]) == 64 for event in events if event.type == "model.requested")
    assert any(event.type == "plan.created" for event in events)
    assert all(
        event.payload["contract_version"] == 1 and event.payload["local_plan_version"] >= 1
        for event in events
        if event.type == "model.requested"
    )
    assert any(event.type == "context.manifest.compiled" for event in events)
    assert any(event.type == "completion.gate.passed" for event in events)
    assert (run_dir / "operations.jsonl").exists()
    assert "GuardedLocalRuntime" in (run_dir / "report.md").read_text(encoding="utf-8")
    assert list(runtime.artifact_store.root.glob("*.txt"))
    assert (run_dir / "report.md").exists()
    assert "volcengine.plan" in (run_dir / "report.md").read_text(encoding="utf-8")
