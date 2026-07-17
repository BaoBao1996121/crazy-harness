from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from crazy_harness.core.artifacts import ArtifactRef, ArtifactStore
from crazy_harness.core.events import Event


COMPACT_DIMENSION_FIELDS = (
    "primary_request_and_intent",
    "key_technical_concepts",
    "files_and_code_sections",
    "errors_and_fixes",
    "problem_solving",
    "all_user_messages_verbatim",
    "pending_tasks",
    "current_work",
    "optional_next_step",
)


class CompactedEventRange(BaseModel):
    first_event_id: str = Field(min_length=1)
    last_event_id: str = Field(min_length=1)
    event_count: int = Field(ge=1)


class CompactQualityChecks(BaseModel):
    authorized_user_messages_verbatim: bool
    code_and_errors_complete: bool
    claims_have_artifact_refs: bool
    paired_boundaries_preserved: bool
    raw_events_preserved: bool


class CompactSummaryArtifact(BaseModel):
    """Nine-section continuation artifact scoped to one agent assignment."""

    task_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    compacted_event_range: CompactedEventRange

    primary_request_and_intent: str
    key_technical_concepts: list[str]
    files_and_code_sections: list[str]
    errors_and_fixes: list[str]
    problem_solving: list[str]
    all_user_messages_verbatim: list[str]
    pending_tasks: list[str]
    current_work: str
    optional_next_step: str | None = None

    artifact_refs: list[str] = Field(default_factory=list)
    compact_quality_checks: CompactQualityChecks


PairRole = Literal["request", "response"]
_PAIR_EVENT_TYPES: dict[str, tuple[str, PairRole]] = {
    "tool.requested": ("tool", "request"),
    "tool.completed": ("tool", "response"),
    "tool.failed": ("tool", "response"),
    "peer.requested": ("peer", "request"),
    "peer.responded": ("peer", "response"),
    "a2a.requested": ("peer", "request"),
    "a2a.responded": ("peer", "response"),
    "approval.requested": ("approval", "request"),
    "approval.decided": ("approval", "response"),
    "approval.approved": ("approval", "response"),
    "approval.rejected": ("approval", "response"),
    "artifact.invalid": ("artifact_repair", "request"),
    "artifact.repaired": ("artifact_repair", "response"),
    "artifact.revision.completed": ("artifact_repair", "response"),
}
_CORRELATION_FIELDS = (
    "operation_id",
    "correlation_id",
    "request_id",
    "approval_id",
    "artifact_id",
    "message_id",
    "pair_id",
)


@dataclass(frozen=True)
class SafePrefixSelection:
    requested_count: int
    prefix: tuple[Event, ...]
    suffix: tuple[Event, ...]
    blocked_by_pair: bool

    @property
    def selected_count(self) -> int:
        return len(self.prefix)


@dataclass(frozen=True)
class FullCompactResult:
    summary_ref: ArtifactRef
    compacted_prefix: tuple[Event, ...]
    recent_suffix: tuple[Event, ...]


def full_compact(
    events: Sequence[Event],
    *,
    requested_count: int,
    summary: CompactSummaryArtifact,
    artifact_store: ArtifactStore,
) -> FullCompactResult:
    """Validate and activate a model-produced nine-dimension compact artifact."""

    selection = select_safe_prefix(events, requested_count=requested_count)
    if not selection.prefix:
        raise ValueError("full compact requires a non-empty safe prefix")
    expected = CompactedEventRange(
        first_event_id=selection.prefix[0].id,
        last_event_id=selection.prefix[-1].id,
        event_count=len(selection.prefix),
    )
    if summary.compacted_event_range != expected:
        raise ValueError("summary event range does not match the selected safe prefix")
    if any(event.task_id != summary.task_id for event in selection.prefix):
        raise ValueError("summary task_id does not match every compacted event")
    if not all(summary.compact_quality_checks.model_dump().values()):
        raise ValueError("full compact quality checks must all pass")

    summary_ref = artifact_store.write_json(
        "context_full_compact",
        {
            "summary": summary.model_dump(mode="json"),
            "compacted_event_ids": [event.id for event in selection.prefix],
            "raw_source": "EventLog",
        },
        summary=f"nine-dimension compact for {summary.assignment_id}",
    )
    return FullCompactResult(
        summary_ref=summary_ref,
        compacted_prefix=selection.prefix,
        recent_suffix=selection.suffix,
    )


def select_safe_prefix(events: Sequence[Event], *, requested_count: int) -> SafePrefixSelection:
    """Return a non-destructive prefix view that never bisects a known pair."""

    source = tuple(events)
    if requested_count < 0 or requested_count > len(source):
        raise ValueError("requested_count must be between zero and the event count")

    groups: dict[tuple[str, str], dict[PairRole, list[int]]] = defaultdict(
        lambda: {"request": [], "response": []}
    )
    request_keys: dict[tuple[str, str], tuple[str, str]] = {}
    for index, event in enumerate(source):
        pair_type = _PAIR_EVENT_TYPES.get(event.type)
        if pair_type is None:
            continue
        family, role = pair_type
        causal_key = request_keys.get((family, event.causation_id or ""))
        key = causal_key if role == "response" and causal_key is not None else _pair_key(
            event, family=family, role=role
        )
        if key is not None:
            groups[key][role].append(index)
            if role == "request":
                request_keys[(family, event.id)] = key

    cutoff = requested_count
    while True:
        unsafe_starts = []
        for roles in groups.values():
            requests = roles["request"]
            if not requests:
                continue
            start = min(requests)
            responses = roles["response"]
            end = max(requests + responses)
            if start < cutoff and (not responses or cutoff <= end):
                unsafe_starts.append(start)
        if not unsafe_starts:
            break
        cutoff = min(unsafe_starts)

    return SafePrefixSelection(
        requested_count=requested_count,
        prefix=source[:cutoff],
        suffix=source[cutoff:],
        blocked_by_pair=cutoff != requested_count,
    )


def _pair_key(event: Event, *, family: str, role: PairRole) -> tuple[str, str] | None:
    for field in _CORRELATION_FIELDS:
        value = event.payload.get(field)
        if value is not None:
            return family, str(value)
    if role == "request":
        return family, event.id
    if event.causation_id is not None:
        return family, event.causation_id
    return None
