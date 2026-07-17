import json

from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.skills import SKILL_ACTIVATE_TOOL_NAME, SkillActivation
from crazy_harness.core.tools import ToolCall
from crazy_harness.taskpacks.evidence_research import (
    EvidenceResearchCompletionGate,
    EvidenceResearchTaskPack,
)


VALID_REPORT = """# Recommendation

Use a ten-percent canary with an explicit rollback trigger.

## Findings

The service requires rollback within ten minutes [source:requirements#rto].
The canary drill exposed a regression within three minutes [source:experiment#canary-result].

## Risks

Every traffic change needs an owned rollback plan [source:policy#rollback-plan].

## Sources

- requirements
- experiment
- policy
"""


def test_research_source_catalog_discloses_metadata_before_body(tmp_path):
    pack = EvidenceResearchTaskPack(tmp_path)
    prepared = pack.prepare("run-catalog")
    tools = pack.build_tools(prepared)

    result = tools.call(ToolCall(name="research.sources.list"))
    payload = json.loads(result.output)

    assert result.status == "ok"
    assert [item["source_id"] for item in payload["sources"]] == [
        "experiment",
        "policy",
        "requirements",
    ]
    assert all("evidence" not in item for item in payload["sources"])
    assert "rollback within ten minutes" not in result.output


def test_research_source_open_uses_real_browser_and_rejects_unknown_sources(tmp_path):
    pack = EvidenceResearchTaskPack(tmp_path)
    prepared = pack.prepare("run-browser")
    tools = pack.build_tools(prepared)

    opened = tools.call(
        ToolCall(name="research.source.open", args={"source_id": "requirements"})
    )
    payload = json.loads(opened.output)
    unknown = tools.call(
        ToolCall(name="research.source.open", args={"source_id": "outside-catalog"})
    )

    assert opened.status == "ok"
    assert payload["canonical_uri"] == "source://requirements"
    assert {item["evidence_id"] for item in payload["evidence"]} == {
        "rto",
        "zero-downtime",
    }
    for relative in payload["artifacts"].values():
        assert (prepared.workspace / relative).is_file()
    assert unknown.status == "error"
    assert "unknown source_id" in (unknown.error or "")


def test_research_report_validator_rejects_unknown_and_single_source_citations(
    tmp_path,
):
    pack = EvidenceResearchTaskPack(tmp_path)
    prepared = pack.prepare("run-invalid-report")
    tools = pack.build_tools(prepared)
    invalid = VALID_REPORT.replace(
        "[source:experiment#canary-result]",
        "[source:requirements#zero-downtime]",
    ).replace("[source:policy#rollback-plan]", "[source:ghost#claim]")

    assert (
        tools.call(
            ToolCall(name="research.report.write", args={"content": invalid})
        ).status
        == "ok"
    )
    result = tools.call(ToolCall(name="research.report.validate"))

    assert result.status == "error"
    assert "unknown citation" in (result.error or "")


def test_research_report_validator_returns_hash_and_multi_source_evidence(tmp_path):
    pack = EvidenceResearchTaskPack(tmp_path)
    prepared = pack.prepare("run-valid-report")
    tools = pack.build_tools(prepared)

    write = tools.call(
        ToolCall(name="research.report.write", args={"content": VALID_REPORT})
    )
    validated = tools.call(ToolCall(name="research.report.validate"))
    payload = json.loads(validated.output)

    assert write.status == "ok"
    assert validated.status == "ok"
    assert payload["report_path"] == "report.md"
    assert len(payload["report_sha256"]) == 64
    assert payload["source_ids"] == ["experiment", "policy", "requirements"]
    assert len(payload["citations"]) == 3


def test_research_pack_exposes_only_its_required_project_skill(tmp_path):
    pack = EvidenceResearchTaskPack(tmp_path)
    skills = pack.build_skills()
    prepared = pack.prepare("run-skill")
    tools = pack.build_tools(prepared, skills=skills)

    assert skills.names() == ("evidence-research",)
    result = tools.call(
        ToolCall(name=SKILL_ACTIVATE_TOOL_NAME, args={"name": "evidence-research"})
    )
    activation = SkillActivation.model_validate_json(result.output)
    assert result.status == "ok"
    assert "canonical citation" in activation.body
    assert "repo-maintainer" not in result.output


def test_research_completion_rejects_artifact_not_bound_to_validated_report(tmp_path):
    task_id = "task-binding"
    event_log = EventLog(tmp_path / "events.jsonl")
    for source_id in ("requirements", "experiment"):
        event_log.append(
            Event(
                run_id="run-binding",
                task_id=task_id,
                type="tool.completed",
                source="test",
                payload={
                    "result": {
                        "name": "research.source.open",
                        "output": json.dumps({"source_id": source_id}),
                    }
                },
            )
        )
    citations = [
        "source:requirements#rto",
        "source:experiment#canary-result",
        "source:policy#rollback-plan",
    ]
    event_log.append(
        Event(
            run_id="run-binding",
            task_id=task_id,
            type="tool.completed",
            source="test",
            payload={
                "result": {
                    "name": "research.report.validate",
                    "output": json.dumps(
                        {
                            "report_path": "report.md",
                            "report_sha256": "a" * 64,
                            "citations": citations,
                        }
                    ),
                }
            },
        )
    )

    result = EvidenceResearchCompletionGate(event_log, task_id).evaluate(
        EvidenceResearchTaskPack.assignment_contract(),
        output={
            "recommendation": "canary",
            "report_path": "report.md",
            "report_sha256": "b" * 64,
            "citations": citations,
        },
        evidence={
            "research.source.open": ["source-open-event"],
            "research.report.validate": ["validator-event"],
        },
    )

    assert result.passed is False
    assert any(finding.path == "$.report_sha256" for finding in result.findings)


def test_research_source_open_rejects_a_tampered_workspace_fixture(tmp_path):
    pack = EvidenceResearchTaskPack(tmp_path)
    prepared = pack.prepare("run-tampered-source")
    fixture = prepared.workspace / "sources" / "requirements.html"
    fixture.write_text(
        '<html><body><li data-evidence-id="rto">tampered claim</li></body></html>',
        encoding="utf-8",
    )
    tools = pack.build_tools(prepared)

    result = tools.call(
        ToolCall(name="research.source.open", args={"source_id": "requirements"})
    )

    assert result.status == "error"
    assert "does not match the immutable source catalog" in (result.error or "")


def test_research_completion_rejects_validation_older_than_latest_report_write(
    tmp_path,
):
    task_id = "task-stale-validation"
    event_log = EventLog(tmp_path / "events.jsonl")
    for source_id in ("requirements", "experiment"):
        event_log.append(
            Event(
                run_id="run-stale-validation",
                task_id=task_id,
                type="tool.completed",
                source="test",
                payload={
                    "result": {
                        "name": "research.source.open",
                        "output": json.dumps({"source_id": source_id}),
                    }
                },
            )
        )
    citations = [
        "source:requirements#rto",
        "source:experiment#canary-result",
        "source:policy#rollback-plan",
    ]
    event_log.append(
        Event(
            run_id="run-stale-validation",
            task_id=task_id,
            type="tool.completed",
            source="test",
            payload={
                "result": {
                    "name": "research.report.validate",
                    "output": json.dumps(
                        {
                            "report_path": "report.md",
                            "report_sha256": "a" * 64,
                            "citations": citations,
                        }
                    ),
                }
            },
        )
    )
    event_log.append(
        Event(
            run_id="run-stale-validation",
            task_id=task_id,
            type="tool.completed",
            source="test",
            payload={
                "result": {
                    "name": "research.report.write",
                    "output": "updated report.md",
                }
            },
        )
    )

    result = EvidenceResearchCompletionGate(event_log, task_id).evaluate(
        EvidenceResearchTaskPack.assignment_contract(),
        output={
            "recommendation": "canary",
            "report_path": "report.md",
            "report_sha256": "a" * 64,
            "citations": citations,
        },
        evidence={
            "research.source.open": ["source-open-event"],
            "research.report.validate": ["validator-event"],
        },
    )

    assert result.passed is False
    assert any("newer report write" in finding.message for finding in result.findings)
