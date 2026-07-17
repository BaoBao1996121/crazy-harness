from __future__ import annotations

import json

from crazy_harness.core.context.manifest import (
    ContextManifest,
    ContextRepresentation,
    ContextTransform,
)
from crazy_harness.core.context.microcompact import ContextItem, MicrocompactResult, microcompact
from crazy_harness.core.events import Event
from crazy_harness.core.skills import skill_activation_from_event


class ContextBuilder:
    """Compile a bounded context view from durable events."""

    _HYGIENE_TYPES = {
        "mailbox.delivery.sent",
        "mailbox.delivery.acked",
        "runtime.agent.busy",
        "runtime.agent.idle",
        "runtime.agent.step.completed",
        "runtime.turn.ready",
        "loop.phase.changed",
        "context.manifest.compiled",
        "capability.manifest.compiled",
        "skill.catalog.compiled",
        "model.requested",
        "model.response.reused",
        "agent.command.reused",
    }

    def __init__(self, *, artifact_store, offload_chars: int = 8000, recent_event_limit: int = 20) -> None:
        self.artifact_store = artifact_store
        self.offload_chars = offload_chars
        self.recent_event_limit = recent_event_limit
        self._offloaded_by_event: dict[str, object] = {}
        self.last_manifest: ContextManifest | None = None

    def microcompact(self, events: list[Event]) -> MicrocompactResult:
        items = [self._event_to_item(event) for event in events]
        result = microcompact(items, artifact_store=self.artifact_store, offload_chars=self.offload_chars)
        for item in result.inline_items:
            if item.source_ref and item.artifact_ref:
                self._offloaded_by_event[item.source_ref] = item.artifact_ref
        return result

    def build_messages(self, events: list[Event]) -> list[dict[str, str]]:
        active_events = [event for event in events if event.type not in self._HYGIENE_TYPES]
        selected_events = active_events[-self.recent_event_limit :]
        result = self.microcompact(selected_events)
        messages = [{"role": item.role, "content": item.content} for item in result.inline_items]
        inline_by_ref = {item.source_ref: item for item in result.inline_items if item.source_ref}
        included_refs = list(inline_by_ref)
        excluded_refs = [event.id for event in events if event.id not in inline_by_ref]
        transforms = [
            ContextTransform(
                ref=ref,
                representation=(
                    ContextRepresentation.REF
                    if item.artifact_ref is not None
                    else ContextRepresentation.INLINE
                ),
                reason="large result offloaded" if item.artifact_ref is not None else "active recent fact",
            )
            for ref, item in inline_by_ref.items()
        ]
        transforms.extend(
            ContextTransform(
                ref=ref,
                representation=ContextRepresentation.DISCARD,
                reason="outside active context view",
            )
            for ref in excluded_refs
        )
        self.last_manifest = ContextManifest.from_messages(
            messages,
            included_refs=included_refs,
            excluded_refs=excluded_refs,
            transform=transforms,
            contract_version=1,
        )
        return messages

    def _event_to_item(self, event: Event) -> ContextItem:
        if skill_activation_from_event(event) is not None:
            return ContextItem(
                role="tool",
                kind="duplicate_ok",
                importance="low",
                source_ref=event.id,
                content="[Skill activation moved to the protected active_skill slot]",
            )
        kind = "tool_result" if event.type == "tool.completed" else "event"
        importance = "critical" if event.type in {"assignment.created", "policy.updated"} else "normal"
        ref = self._offloaded_by_event.get(event.id)
        if ref is not None:
            return ContextItem(
                role="tool" if kind == "tool_result" else "user",
                kind=kind,
                importance=importance,
                source_ref=event.id,
                artifact_ref=ref,
                content=f"[artifact_ref uri={ref.uri} kind={ref.kind} summary={ref.summary}]",
            )
        return ContextItem(
            role="tool" if kind == "tool_result" else "user",
            kind=kind,
            importance=importance,
            source_ref=event.id,
            content=json.dumps({"type": event.type, "payload": event.payload}, ensure_ascii=False, default=str),
        )
