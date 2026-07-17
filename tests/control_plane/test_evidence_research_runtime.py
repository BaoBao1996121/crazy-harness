from pathlib import Path

from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.core.models import FakeModelProvider


def _request(
    brief: str = "Recommend a deployment strategy from the supplied evidence.",
) -> TaskRequest:
    return TaskRequest(
        title="Choose a deployment strategy",
        brief=brief,
        execution_mode="single",
        model_mode="scripted",
        task_pack="evidence-research",
    )


def test_resident_runtime_completes_research_with_browser_and_citation_evidence(
    tmp_path,
):
    runtime = ResidentRuntime(tmp_path)

    created = runtime.submit_task(_request())
    runtime.run_until_idle(max_steps=50)

    events = runtime.store.read_all(run_id=created.run_id)
    created_event = next(event for event in events if event.type == "run.created")
    workspace = Path(created_event.payload["workspace_path"])
    completed_tools = [
        event.payload["result"]["name"]
        for event in events
        if event.type == "tool.completed"
    ]
    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert completed_tools.count("research.source.open") == 3
    assert "research.report.validate" in completed_tools
    assert (workspace / "report.md").is_file()
    assert len(list((workspace / "browser").glob("*/page.png"))) == 3
    assert "completion.gate.passed" in [event.type for event in events]


def test_research_submission_without_validator_evidence_is_rejected(tmp_path):
    model = FakeModelProvider(
        [
            '{"type":"submit_output","reason":"trust me","artifact":{"recommendation":"canary","report_path":"report.md","citations":["source:requirements#rto"]}}',
            '{"type":"report_blocked","reason":"cannot provide validated citations"}',
        ]
    )
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)

    created = runtime.submit_task(_request())
    runtime.run_until_idle(max_steps=20)

    event_types = [
        event.type for event in runtime.store.read_all(run_id=created.run_id)
    ]
    assert runtime.snapshot(created.run_id)["run"]["status"] == "failed"
    assert "completion.gate.failed" in event_types
    assert "completion.gate.passed" not in event_types


def test_research_loop_is_recovered_from_persisted_pack_id_and_brief(tmp_path):
    brief = "Persist this exact research question across restart."
    first = ResidentRuntime(tmp_path)
    created = first.submit_task(_request(brief))
    assignment = next(
        event
        for event in first.store.read_all(run_id=created.run_id)
        if event.type == "assignment.created"
    )

    reopened = ResidentRuntime(tmp_path)
    loop = reopened._single_loop_for(assignment)

    assert loop.prompt_pack.prompt_version == "evidence-research-v1"
    assert loop.prompt_pack.task_brief_section == brief
    assert loop.tool_registry.has("research.sources.list")
    assert not loop.tool_registry.has("repo.write")
