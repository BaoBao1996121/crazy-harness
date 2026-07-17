import json
from pathlib import Path

from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.core.models import FakeModelProvider


def _action(**payload) -> str:
    return json.dumps(payload)


def _repo_maintainer_request() -> TaskRequest:
    return TaskRequest(
        title="Repair the calculator",
        brief="Find the implementation bug, make the smallest safe fix, and prove it with tests.",
        execution_mode="single",
        model_mode="scripted",
        task_pack="repo-maintainer",
    )


def _successful_repo_model() -> FakeModelProvider:
    fixed_source = """def clamp(value: int, lower: int, upper: int) -> int:
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return max(lower, min(value, upper))
"""
    return FakeModelProvider(
        [
            _action(type="call_tool", reason="inspect implementation", tool_name="repo.read", tool_args={"path": "calculator.py"}),
            _action(type="call_tool", reason="apply bounded fix", tool_name="repo.write", tool_args={"path": "calculator.py", "content": fixed_source}),
            _action(type="call_tool", reason="prove behavior", tool_name="test.run", tool_args={}),
            _action(type="call_tool", reason="record changed files", tool_name="repo.diff", tool_args={}),
            _action(
                type="submit_output",
                reason="tests and diff prove the fix",
                artifact={"summary": "Corrected clamp bounds handling.", "changed_files": ["calculator.py"]},
            ),
        ]
    )


def test_resident_single_agent_fixes_disposable_repo_and_passes_machine_gate(tmp_path):
    model = _successful_repo_model()
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)

    created = runtime.submit_task(_repo_maintainer_request())
    runtime.run_until_idle(max_steps=40)

    events = runtime.store.read_all(run_id=created.run_id)
    created_event = next(event for event in events if event.type == "run.created")
    workspace = Path(created_event.payload["workspace_path"])
    snapshot = runtime.snapshot(created.run_id)
    assert snapshot["run"]["status"] == "succeeded"
    assert snapshot["contexts"][-1]["agent_id"] == "generalist"
    assert snapshot["contexts"][-1]["manifest"]["included_refs"]
    assert workspace.is_dir() and "max(lower, min(value, upper))" in (workspace / "calculator.py").read_text(encoding="utf-8")
    assert any(event.type == "tool.completed" and event.payload["result"]["name"] == "test.run" for event in events)
    assert "completion.gate.passed" in [event.type for event in events]
    assert model.call_count == 5
    capability_events = [event for event in events if event.type == "capability.manifest.compiled"]
    assert len(capability_events) == model.call_count
    assert all(event.payload["strategy"] == "inline_all" for event in capability_events)
    assert set(capability_events[-1].payload["manifest"]["disclosed_names"]) == {
        "repo.list",
        "repo.read",
        "repo.search",
        "repo.write",
        "repo.replace",
        "test.run",
        "repo.diff",
        "shell.run",
        "skill.activate",
    }


def test_resident_single_agent_rejects_unsupported_completion_claim(tmp_path):
    model = FakeModelProvider(
        [
            _action(
                type="submit_output",
                reason="claim completion without checking",
                artifact={"summary": "Looks fixed.", "changed_files": ["calculator.py"]},
            ),
            _action(type="report_blocked", reason="cannot provide machine evidence"),
        ]
    )
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)

    created = runtime.submit_task(_repo_maintainer_request())
    runtime.run_until_idle(max_steps=20)

    event_types = [event.type for event in runtime.store.read_all(run_id=created.run_id)]
    assert runtime.snapshot(created.run_id)["run"]["status"] == "failed"
    assert "completion.gate.failed" in event_types
    assert "completion.gate.passed" not in event_types
    assert "run.succeeded" not in event_types


def test_resident_single_agent_reuses_model_response_after_persist_crash(tmp_path):
    model = _successful_repo_model()
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)
    runtime.arm_fault("after_model_persisted")

    created = runtime.submit_task(_repo_maintainer_request())
    runtime.run_until_idle(max_steps=50)

    events = runtime.store.read_all(run_id=created.run_id)
    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert sum(event.type == "runtime.agent.crashed" for event in events) == 1
    assert sum(event.type == "model.response.reused" for event in events) == 1
    assert sum(event.type == "model.completed" for event in events) == 5
    assert model.call_count == 5


def test_resident_single_agent_recovers_tool_effect_from_ledger_without_reexecution(tmp_path):
    model = _successful_repo_model()
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)
    created = runtime.submit_task(_repo_maintainer_request())
    assert runtime.scheduler.run_once() is True
    runtime.arm_fault("after_tool_effect")

    runtime.run_until_idle(max_steps=50)

    events = runtime.store.read_all(run_id=created.run_id)
    writes = [
        event
        for event in events
        if event.type == "tool.completed" and event.payload["result"]["name"] == "repo.write"
    ]
    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert len(writes) == 1
    assert writes[0].payload["recovered_from_ledger"] is True
    assert model.call_count == 5


def test_resident_single_agent_reuses_validated_command_after_persist_crash(tmp_path):
    model = _successful_repo_model()
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)
    runtime.arm_fault("after_command_persisted")

    created = runtime.submit_task(_repo_maintainer_request())
    runtime.run_until_idle(max_steps=50)

    events = runtime.store.read_all(run_id=created.run_id)
    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert sum(event.type == "runtime.agent.crashed" for event in events) == 1
    assert sum(event.type == "agent.command.reused" for event in events) == 1
    assert model.call_count == 5


def test_resident_single_agent_resumes_with_the_persisted_assignment_contract(tmp_path):
    model = _successful_repo_model()
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)
    created = runtime.submit_task(_repo_maintainer_request())
    assignment = next(
        event
        for event in runtime.store.read_all(run_id=created.run_id)
        if event.type == "assignment.created"
    )

    original = runtime.repo_maintainer_pack.assignment_contract()
    runtime.repo_maintainer_pack.assignment_contract = lambda: original.model_copy(
        update={"budgets": original.budgets.model_copy(update={"tool_calls": 0})}
    )
    runtime.run_until_idle(max_steps=40)

    assert assignment.payload["contract"]["budgets"]["tool_calls"] == 12
    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"

def test_default_scripted_runtime_activates_project_skill(tmp_path):
    runtime = ResidentRuntime(tmp_path)

    created = runtime.submit_task(_repo_maintainer_request())
    runtime.run_until_idle(max_steps=50)

    events = runtime.store.read_all(run_id=created.run_id)
    catalog = next(event for event in events if event.type == "skill.catalog.compiled")
    activation = next(
        event
        for event in events
        if event.type == "tool.completed"
        and event.payload["result"]["name"] == "skill.activate"
    )
    manifests = [
        event.payload["manifest"]
        for event in events
        if event.type == "capability.manifest.compiled"
    ]

    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert catalog.payload["entries"] == [
        {
            "name": "repo-maintainer",
            "description": catalog.payload["entries"][0]["description"],
            "scope": "project",
            "source_id": "crazy-project",
        }
    ]
    assert "body" not in catalog.payload["entries"][0]
    assert activation.payload["result"]["status"] == "ok"
    assert manifests[0]["kinds"]["skill.activate"] == "skill"
    assert manifests[0]["providers"]["skill.activate"] == "local:skills"
