import pytest

from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.context.compact import (
    COMPACT_DIMENSION_FIELDS,
    CompactQualityChecks,
    CompactSummaryArtifact,
    CompactedEventRange,
    full_compact,
    select_safe_prefix,
)
from crazy_harness.core.context.manifest import (
    ContextManifest,
    ContextRepresentation,
    ContextTransform,
)
from crazy_harness.core.context.history import (
    HistoryACL,
    HistoryAccessDenied,
    HistoryNotFound,
    HistoryPrincipal,
    HistoryRecord,
    HistoryService,
)
from crazy_harness.core.events import Event


@pytest.mark.smoke
def test_context_manifest_audits_compiled_prompt_deterministically():
    messages = [
        {"role": "system", "content": "latest contract"},
        {"role": "user", "content": "release the service"},
    ]
    transforms = [
        ContextTransform(
            ref="event://1",
            representation=ContextRepresentation.INLINE,
            reason="current assignment",
        ),
        ContextTransform(
            ref="event://0",
            representation=ContextRepresentation.DISCARD,
            reason="superseded observation",
        ),
    ]

    first = ContextManifest.from_messages(
        messages,
        included_refs=["event://1"],
        excluded_refs=["event://0"],
        transform=transforms,
        contract_version=3,
    )
    second = ContextManifest.from_messages(
        messages,
        included_refs=["event://1"],
        excluded_refs=["event://0"],
        transform=transforms,
        contract_version=3,
    )

    assert first.included_refs == ["event://1"]
    assert first.excluded_refs == ["event://0"]
    assert first.transform == transforms
    assert first.token_estimate > 0
    assert first.contract_version == 3
    assert first.prompt_hash == second.prompt_hash
    assert len(first.prompt_hash) == 64

    with pytest.raises(ValueError, match="both included and excluded"):
        ContextManifest.from_messages(
            messages,
            included_refs=["event://1"],
            excluded_refs=["event://1"],
            transform=[],
            contract_version=3,
        )


def test_compact_summary_artifact_has_all_nine_dimensions():
    summary = CompactSummaryArtifact(
        task_id="task-1",
        agent_id="builder-1",
        assignment_id="assignment-1",
        compacted_event_range=CompactedEventRange(
            first_event_id="event-1",
            last_event_id="event-8",
            event_count=8,
        ),
        primary_request_and_intent="Ship the toy service safely.",
        key_technical_concepts=["event sourcing", "safe prefix"],
        files_and_code_sections=["crazy_harness/core/context/compact.py: prefix selection"],
        errors_and_fixes=["TimeoutError -> bounded retry"],
        problem_solving=["Kept the paired operation in the recent suffix."],
        all_user_messages_verbatim=["Ship the toy service safely."],
        pending_tasks=["Run the reviewer."],
        current_work="Compacting the completed history prefix.",
        optional_next_step="Resume from the recent suffix.",
        artifact_refs=["artifact://tool-log"],
        compact_quality_checks=CompactQualityChecks(
            authorized_user_messages_verbatim=True,
            code_and_errors_complete=True,
            claims_have_artifact_refs=True,
            paired_boundaries_preserved=True,
            raw_events_preserved=True,
        ),
    )

    expected_dimensions = {
        "primary_request_and_intent",
        "key_technical_concepts",
        "files_and_code_sections",
        "errors_and_fixes",
        "problem_solving",
        "all_user_messages_verbatim",
        "pending_tasks",
        "current_work",
        "optional_next_step",
    }
    assert set(COMPACT_DIMENSION_FIELDS) == expected_dimensions
    assert expected_dimensions <= summary.model_dump().keys()
    assert summary.all_user_messages_verbatim == ["Ship the toy service safely."]


def test_safe_prefix_never_splits_a_tool_pair_or_deletes_raw_events():
    events = [
        Event(id="event-0", run_id="run-1", task_id="task-1", type="seed", source="test"),
        Event(
            id="event-1",
            run_id="run-1",
            task_id="task-1",
            type="tool.requested",
            source="builder",
            payload={"operation_id": "operation-1"},
        ),
        Event(id="event-2", run_id="run-1", task_id="task-1", type="observation", source="builder"),
        Event(
            id="event-3",
            run_id="run-1",
            task_id="task-1",
            type="tool.completed",
            source="builder",
            payload={"operation_id": "operation-1", "result": "ok"},
            causation_id="event-1",
        ),
        Event(id="event-4", run_id="run-1", task_id="task-1", type="followup", source="builder"),
    ]
    original_ids = [event.id for event in events]

    split_pair = select_safe_prefix(events, requested_count=3)
    completed_pair = select_safe_prefix(events, requested_count=4)

    assert [event.id for event in split_pair.prefix] == ["event-0"]
    assert [event.id for event in split_pair.suffix][:3] == ["event-1", "event-2", "event-3"]
    assert split_pair.blocked_by_pair is True
    assert [event.id for event in completed_pair.prefix] == ["event-0", "event-1", "event-2", "event-3"]
    assert [event.id for event in events] == original_ids
    assert split_pair.suffix[0] is events[1]


def test_paired_boundary_matches_response_by_causation_id():
    events = [
        Event(id="event-0", run_id="run-1", task_id="task-1", type="seed", source="test"),
        Event(
            id="event-1",
            run_id="run-1",
            task_id="task-1",
            type="peer.requested",
            source="coordinator",
            payload={"correlation_id": "delegation-1"},
        ),
        Event(
            id="event-2",
            run_id="run-1",
            task_id="task-1",
            type="peer.responded",
            source="builder",
            payload={"answer": "done"},
            causation_id="event-1",
        ),
    ]

    selection = select_safe_prefix(events, requested_count=3)

    assert selection.selected_count == 3
    assert selection.blocked_by_pair is False


def test_history_read_and_search_enforce_principal_assignment_and_acl():
    owner = HistoryPrincipal(principal="builder-1", assignment_id="assignment-build")
    reviewer = HistoryPrincipal(principal="reviewer-1", assignment_id="assignment-review")
    wrong_assignment = HistoryPrincipal(principal="builder-1", assignment_id="assignment-other")
    private = HistoryRecord(
        ref="history://builder/private",
        owner=owner,
        event=Event(
            id="private-event",
            run_id="run-1",
            task_id="task-1",
            type="observation",
            source="builder-1",
            payload={"text": "needle private transcript"},
        ),
    )
    shared = HistoryRecord(
        ref="history://builder/shared",
        owner=owner,
        acl=HistoryACL(grants=[reviewer]),
        event=Event(
            id="shared-event",
            run_id="run-1",
            task_id="task-1",
            type="artifact.created",
            source="builder-1",
            payload={"text": "needle approved evidence"},
        ),
    )
    history = HistoryService([private, shared])

    assert history.read(private.ref, subject=owner).event.id == "private-event"
    assert history.read(shared.ref, subject=reviewer).event.id == "shared-event"
    assert [record.ref for record in history.search("needle", subject=reviewer)] == [shared.ref]

    with pytest.raises(HistoryAccessDenied):
        history.read(private.ref, subject=reviewer)
    with pytest.raises(HistoryAccessDenied):
        history.read(private.ref, subject=wrong_assignment)


def test_history_read_never_treats_an_unregistered_ref_as_a_disk_path(tmp_path):
    transcript_path = tmp_path / "other-agent-transcript.jsonl"
    transcript_path.write_text('{"secret": "must not leak"}\n', encoding="utf-8")
    history = HistoryService()
    subject = HistoryPrincipal(principal="builder-1", assignment_id="assignment-build")

    with pytest.raises(HistoryNotFound):
        history.read(str(transcript_path), subject=subject)


def test_full_compact_validates_nine_dimensions_and_preserves_raw_events(tmp_path):
    events = [
        Event(id=f"event-{index}", run_id="run-1", task_id="task-1", type="observation", source="builder")
        for index in range(3)
    ]
    summary = CompactSummaryArtifact(
        task_id="task-1",
        agent_id="builder",
        assignment_id="task-1",
        compacted_event_range=CompactedEventRange(
            first_event_id="event-0",
            last_event_id="event-1",
            event_count=2,
        ),
        primary_request_and_intent="Prepare the release safely.",
        key_technical_concepts=["event sourcing"],
        files_and_code_sections=["app.py"],
        errors_and_fixes=["none"],
        problem_solving=["kept recent work active"],
        all_user_messages_verbatim=["Prepare the release safely."],
        pending_tasks=["review"],
        current_work="building",
        optional_next_step="review",
        artifact_refs=["event://event-0"],
        compact_quality_checks=CompactQualityChecks(
            authorized_user_messages_verbatim=True,
            code_and_errors_complete=True,
            claims_have_artifact_refs=True,
            paired_boundaries_preserved=True,
            raw_events_preserved=True,
        ),
    )

    result = full_compact(
        events,
        requested_count=2,
        summary=summary,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
    )

    assert [event.id for event in result.compacted_prefix] == ["event-0", "event-1"]
    assert [event.id for event in result.recent_suffix] == ["event-2"]
    assert result.summary_ref.kind == "context_full_compact"
    assert [event.id for event in events] == ["event-0", "event-1", "event-2"]
