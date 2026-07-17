from pathlib import Path

from crazy_harness.worlds.cicd.team import build_dev_release_team_runtime


def test_mock_team_uses_dynamic_assignments_mailboxes_and_review(tmp_path):
    team = build_dev_release_team_runtime(
        repo_path=Path("examples/hello-crazy-api").resolve(),
        runs_dir=tmp_path / "runs",
    )

    run_dir = team.run()

    events = team.event_log.read_all()
    delegated = [event.payload["agent_id"] for event in events if event.type == "team.assignment.delegated"]
    assert delegated == ["scout", "builder", "reviewer"]
    assert any(event.type == "mailbox.delivery.sent" for event in events)
    assert any(event.type == "runtime.agent.busy" for event in events)
    assert any(event.type == "runtime.agent.step.completed" for event in events)
    assert any(event.type == "a2a.peer.authorized" for event in events)
    assert any(event.type == "plan.created" for event in events)
    assert any(event.type == "completion.gate.passed" for event in events)
    scout_operation = next(
        event
        for event in events
        if event.type == "operation.started" and event.task_id == "inspect-source"
    )
    assert scout_operation.payload["hook_patched"] is True
    assert scout_operation.payload["proposed_tool_args"] == {"path": "./app.py"}
    assert scout_operation.payload["tool_args"] == {"path": "app.py"}
    review = next(event for event in events if event.type == "review.completed")
    assert review.payload["verdict"] == "approve"
    assert events[-1].type == "team.completed"
    assert sorted(path.name for path in (run_dir / "operations").glob("*.jsonl")) == [
        "inspect-source.jsonl",
        "prepare-release.jsonl",
    ]
    assert (run_dir / "team_report.md").exists()

    scout_busy = next(index for index, event in enumerate(events) if event.type == "runtime.agent.busy" and event.payload["agent_id"] == "scout")
    scout_assignment = next(index for index, event in enumerate(events) if event.type == "assignment.created" and event.task_id == "inspect-source")
    scout_step_done = next(index for index, event in enumerate(events) if event.type == "runtime.agent.step.completed" and event.payload["agent_id"] == "scout")
    assert scout_busy < scout_assignment < scout_step_done
    assert any(event.type == "model.requested" and event.task_id == "review-release" for event in events)
    builder_wait = next(index for index, event in enumerate(events) if event.type == "agent.waiting" and event.task_id == "prepare-release")
    peer_response = next(index for index, event in enumerate(events) if event.type == "a2a.peer.responded" and event.task_id == "prepare-release")
    resumed_model_call = next(
        index
        for index, event in enumerate(events)
        if index > peer_response and event.type == "model.requested" and event.task_id == "prepare-release"
    )
    assert builder_wait < peer_response < resumed_model_call
