from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.artifacts import ArtifactRef, ArtifactStore
from crazy_harness.core.context.manifest import (
    ContextManifest,
    ContextRepresentation,
    ContextTransform,
)
from crazy_harness.core.events import Event


@dataclass(frozen=True)
class CompiledContext:
    messages: list[dict[str, str]]
    manifest: ContextManifest
    event: Event


class PersistentContextCompiler:
    """Rebuild a bounded prompt view every turn and persist its exact manifest."""

    _HYGIENE_TYPES = {
        "mailbox.delivery.sent",
        "mailbox.delivery.acked",
        "runtime.agent.busy",
        "runtime.agent.idle",
        "runtime.agent.step.completed",
        "candidate.submitted",
        "candidate.accepted",
        "context.compiled",
        "context.item.offloaded",
        "capability.manifest.compiled",
    }

    def __init__(
        self,
        store: SQLiteEventStore,
        artifact_store: ArtifactStore,
        *,
        offload_chars: int = 700,
        recent_event_limit: int = 24,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store
        self.offload_chars = offload_chars
        self.recent_event_limit = recent_event_limit

    def compile(self, *, agent_id: str, trigger: Event) -> CompiledContext:
        all_events = self.store.read_all(run_id=trigger.run_id)
        active = [event for event in all_events if event.type not in self._HYGIENE_TYPES]
        selected = active[-self.recent_event_limit :]
        selected_ids = {event.id for event in selected}
        messages: list[dict[str, str]] = []
        transforms: list[ContextTransform] = []
        offloaded_count = 0

        for source in selected:
            content = json.dumps(
                {"type": source.type, "source": source.source, "payload": source.payload},
                ensure_ascii=False,
                default=str,
            )
            representation = ContextRepresentation.INLINE
            reason = "active recent fact"
            if source.type == "tool.completed" and len(content) > self.offload_chars:
                ref = self._offload(source, content)
                content = f"[artifact_ref uri={ref.uri} kind={ref.kind} summary={ref.summary}]"
                representation = ContextRepresentation.REF
                reason = "large tool result offloaded before prompt assembly"
                offloaded_count += 1
            messages.append({"role": "user", "content": content})
            transforms.append(
                ContextTransform(ref=source.id, representation=representation, reason=reason)
            )

        excluded = [event.id for event in all_events if event.id not in selected_ids]
        transforms.extend(
            ContextTransform(
                ref=event_id,
                representation=ContextRepresentation.DISCARD,
                reason="microcompact removed runtime noise or stale history",
            )
            for event_id in excluded
        )
        manifest = ContextManifest.from_messages(
            messages,
            included_refs=[event.id for event in selected],
            excluded_refs=excluded,
            transform=transforms,
            contract_version=1,
        )
        context_event = self.store.append(
            Event(
                id=str(uuid5(NAMESPACE_URL, f"context:{trigger.id}:{agent_id}")),
                run_id=trigger.run_id,
                task_id=trigger.task_id,
                type="context.compiled",
                source=f"context.compiler:{agent_id}",
                payload={
                    "agent_id": agent_id,
                    "trigger_event_id": trigger.id,
                    "context_epoch": 1,
                    "manifest": manifest.model_dump(mode="json"),
                    "microcompact": {
                        "retained_count": len(selected),
                        "discarded_count": len(excluded),
                        "offloaded_count": offloaded_count,
                    },
                    "message_preview": messages[-3:],
                },
                causation_id=trigger.id,
            )
        )
        return CompiledContext(messages=messages, manifest=manifest, event=context_event)

    def _offload(self, source: Event, content: str) -> ArtifactRef:
        existing = next(
            (
                event
                for event in self.store.read_all(run_id=source.run_id)
                if event.type == "context.item.offloaded"
                and event.payload.get("source_event_id") == source.id
            ),
            None,
        )
        if existing is not None:
            return ArtifactRef.model_validate(existing.payload["artifact_ref"])

        ref = self.artifact_store.write_text(
            "tool_result",
            content,
            summary=f"offloaded tool result ({len(content)} chars)",
        )
        self.store.append(
            Event(
                id=str(uuid5(NAMESPACE_URL, f"context-offload:{source.id}")),
                run_id=source.run_id,
                task_id=source.task_id,
                type="context.item.offloaded",
                source="context.compiler",
                payload={
                    "source_event_id": source.id,
                    "artifact_ref": ref.model_dump(mode="json"),
                    "original_chars": len(content),
                },
                causation_id=source.id,
            )
        )
        return ref
