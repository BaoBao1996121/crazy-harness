from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any


from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.context import ContextItem, hydrate, microcompact
from crazy_harness.core.context.compact import (
    COMPACT_DIMENSION_FIELDS,
    CompactQualityChecks,
    CompactSummaryArtifact,
    CompactedEventRange,
    full_compact,
)
from crazy_harness.core.context.history import (
    HistoryACL,
    HistoryAccessDenied,
    HistoryPrincipal,
    HistoryRecord,
    HistoryService,
)
from crazy_harness.core.events import Event, EventLog


def _relative(path: str | Path, output: Path) -> str:
    return Path(path).resolve().relative_to(output.resolve()).as_posix()


def _representation(item: ContextItem) -> str:
    return "artifact_ref" if item.content.startswith("[artifact_ref ") else "inline"


def _event(
    event_id: str,
    event_type: str,
    source: str,
    payload: dict[str, Any],
    causation_id: str | None = None,
) -> Event:
    return Event(
        id=event_id,
        run_id="block-05-run",
        task_id="safe-release",
        type=event_type,
        source=source,
        payload=payload,
        causation_id=causation_id,
    )


def run_scenario(output: Path | None = None) -> dict[str, Any]:
    output = output.resolve() if output else Path(tempfile.mkdtemp(prefix="crazy-block-05-"))
    output.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="runtime_", dir=output))
    artifact_store = ArtifactStore(runtime / "artifacts")
    event_log = EventLog(runtime / "events.jsonl")

    large_result = "\n".join(
        f"release-evidence-marker line {index:03d}: tests passed with trace reference"
        for index in range(80)
    )
    events = [
        _event("event-00-user-request", "user.message", "course-owner", {"text": "Prepare the release safely and retain evidence."}),
        _event("event-01-tool-request", "tool.requested", "builder", {"operation_id": "test-op-1", "tool": "pytest"}),
        _event(
            "event-02-tool-result",
            "tool.completed",
            "builder",
            {"operation_id": "test-op-1", "text": large_result},
            "event-01-tool-request",
        ),
        _event("event-03-fix", "observation", "builder", {"text": "Timeout fixed with a bounded retry."}),
        _event("event-04-current-work", "current.work", "reviewer", {"text": "Review the compacted evidence."}),
    ]
    for event in events:
        event_log.append(event)

    initial = ContextItem(
        role="tool",
        kind="tool_result",
        content=large_result,
        source_ref=events[2].id,
    )
    offloaded = microcompact([initial], artifact_store=artifact_store, offload_chars=400)
    offloaded_item = offloaded.inline_items[0]
    hydrated_item = hydrate(offloaded_item, artifact_store=artifact_store)
    leased = microcompact([hydrated_item], artifact_store=artifact_store, offload_chars=400)
    leased_item = leased.inline_items[0]
    reoffloaded = microcompact([leased_item], artifact_store=artifact_store, offload_chars=400)
    reoffloaded_item = reoffloaded.inline_items[0]
    states = [
        {"step": "offload", "representation": _representation(offloaded_item), "hydration_turns_remaining": offloaded_item.hydration_turns_remaining},
        {"step": "hydrate", "representation": _representation(hydrated_item), "hydration_turns_remaining": hydrated_item.hydration_turns_remaining},
        {"step": "leased_turn", "representation": _representation(leased_item), "hydration_turns_remaining": leased_item.hydration_turns_remaining},
        {"step": "reoffload", "representation": _representation(reoffloaded_item), "hydration_turns_remaining": reoffloaded_item.hydration_turns_remaining},
    ]

    summary = CompactSummaryArtifact(
        task_id="safe-release",
        agent_id="builder",
        assignment_id="assignment-build",
        compacted_event_range=CompactedEventRange(
            first_event_id=events[0].id,
            last_event_id=events[3].id,
            event_count=4,
        ),
        primary_request_and_intent="Prepare the release safely and retain evidence.",
        key_technical_concepts=["artifact offloading", "hydration lease", "safe prefix"],
        files_and_code_sections=["release test result artifact"],
        errors_and_fixes=["Timeout -> bounded retry"],
        problem_solving=["Kept the completed tool request/response pair together."],
        all_user_messages_verbatim=["Prepare the release safely and retain evidence."],
        pending_tasks=["Review compacted evidence."],
        current_work="Reviewing the recent suffix.",
        optional_next_step="Approve or reject the release.",
        artifact_refs=[offloaded_item.artifact_ref.uri],
        compact_quality_checks=CompactQualityChecks(
            authorized_user_messages_verbatim=True,
            code_and_errors_complete=True,
            claims_have_artifact_refs=True,
            paired_boundaries_preserved=True,
            raw_events_preserved=True,
        ),
    )
    compacted = full_compact(
        events,
        requested_count=4,
        summary=summary,
        artifact_store=artifact_store,
    )

    owner = HistoryPrincipal(principal="builder", assignment_id="assignment-build")
    reviewer = HistoryPrincipal(principal="reviewer", assignment_id="assignment-review")
    shared_ref = "history://assignment-build/release-evidence"
    private_ref = "history://assignment-build/private-notes"
    history = HistoryService(
        [
            HistoryRecord(ref=shared_ref, owner=owner, acl=HistoryACL(grants=[reviewer]), event=events[2]),
            HistoryRecord(ref=private_ref, owner=owner, event=events[3]),
        ]
    )
    authorized = history.read(shared_ref, subject=reviewer)
    matches = history.search("release-evidence-marker", subject=reviewer)
    private_access_denied = False
    try:
        history.read(private_ref, subject=reviewer)
    except HistoryAccessDenied:
        private_access_denied = True

    raw_ids = [event.id for event in event_log.read_all()]
    roundtrip_exact = hydrated_item.content == leased_item.content == large_result
    checks = {
        "events_durable": raw_ids == [event.id for event in events],
        "large_result_offloaded": artifact_store.read_text(offloaded_item.artifact_ref) == large_result,
        "hydration_lease_reoffloaded": [state["representation"] for state in states] == ["artifact_ref", "inline", "inline", "artifact_ref"],
        "full_compact_nine_dimensions": len(COMPACT_DIMENSION_FIELDS) == 9,
        "raw_events_preserved": raw_ids == [event.id for event in events],
        "history_authorized_recall": authorized.ref == shared_ref and [record.ref for record in matches] == [shared_ref],
        "history_private_denied": private_access_denied,
    }
    evidence = {
        "scenario": "block_05_context_lifecycle",
        "result": "pass" if all(checks.values()) else "fail",
        "events": {"path": _relative(event_log.path, output), "count": len(raw_ids)},
        "microcompact": {
            "original_chars": len(large_result),
            "artifact_path": _relative(offloaded_item.artifact_ref.uri, output),
            "states": states,
            "roundtrip_exact": roundtrip_exact,
        },
        "full_compact": {
            "dimensions": list(COMPACT_DIMENSION_FIELDS),
            "dimension_count": len(COMPACT_DIMENSION_FIELDS),
            "compacted_prefix_count": len(compacted.compacted_prefix),
            "recent_suffix_count": len(compacted.recent_suffix),
            "summary_artifact_path": _relative(compacted.summary_ref.uri, output),
            "raw_events_preserved": checks["raw_events_preserved"],
        },
        "history": {
            "subject": reviewer.model_dump(),
            "shared_ref": shared_ref,
            "authorized_match_refs": [record.ref for record in matches],
            "private_access_denied": private_access_denied,
        },
        "checks": checks,
    }
    if not all(checks.values()) or not roundtrip_exact:
        raise RuntimeError("Block 5 scenario evidence checks failed")

    json_path = output / "evidence.json"
    markdown_path = output / "evidence.md"
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(
        "# Block 5 Context Lifecycle Evidence\n\n"
        f"- Result: **PASS**\n- Durable events: `{len(raw_ids)}`\n"
        f"- Microcompact: `{states[0]['representation']} -> inline -> inline -> {states[-1]['representation']}`\n"
        f"- Full compact dimensions: `{len(COMPACT_DIMENSION_FIELDS)}`\n"
        f"- Authorized history matches: `{len(matches)}`; private access denied: `{private_access_denied}`\n"
        f"- Raw runtime: `{runtime.name}/`\n",
        encoding="utf-8",
    )
    print(f"evidence_json={json_path}")
    print(f"evidence_markdown={markdown_path}")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Block 5 context lifecycle evidence scenario.")
    parser.add_argument("--output", type=Path, help="Evidence directory; defaults to a temporary directory.")
    args = parser.parse_args()
    run_scenario(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
