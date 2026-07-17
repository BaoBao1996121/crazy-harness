from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from crazy_harness.core.artifacts import ArtifactRef, ArtifactStore


class ContextItem(BaseModel):
    role: str
    content: str
    importance: Literal["low", "normal", "high", "critical"] = "normal"
    kind: str = "message"
    artifact_ref: ArtifactRef | None = None
    hydration_turns_remaining: int = 0
    source_ref: str | None = None


class MicrocompactResult(BaseModel):
    inline_items: list[ContextItem] = Field(default_factory=list)
    offloaded_refs: list[ArtifactRef] = Field(default_factory=list)
    discarded_count: int = 0


def microcompact(
    items: list[ContextItem],
    *,
    artifact_store: ArtifactStore,
    offload_chars: int = 8000,
) -> MicrocompactResult:
    """Run a deterministic, non-destructive context hygiene pass."""

    inline: list[ContextItem] = []
    refs: list[ArtifactRef] = []
    discarded = 0
    protected_kinds = {"policy", "agent_card", "assignment_contract", "runtime_manifest", "active_skill"}

    for item in items:
        protected = item.importance in {"high", "critical"} or item.kind in protected_kinds
        if item.importance == "low" and item.kind in {"transient", "nudge", "duplicate_ok"}:
            discarded += 1
            continue
        if item.artifact_ref is not None and item.hydration_turns_remaining <= 0:
            refs.append(item.artifact_ref)
            inline.append(
                item.model_copy(
                    update={
                        "content": _artifact_placeholder(item.artifact_ref),
                        "hydration_turns_remaining": 0,
                    }
                )
            )
            continue
        if item.artifact_ref is not None and item.hydration_turns_remaining > 0:
            inline.append(item.model_copy(update={"hydration_turns_remaining": item.hydration_turns_remaining - 1}))
            refs.append(item.artifact_ref)
            continue
        if not protected and item.kind in {"tool_result", "log", "diff", "webpage", "file"} and len(item.content) > offload_chars:
            ref = artifact_store.write_text(
                item.kind,
                item.content,
                summary=f"offloaded {item.kind} ({len(item.content)} chars)",
            )
            refs.append(ref)
            inline.append(item.model_copy(update={"content": _artifact_placeholder(ref), "artifact_ref": ref}))
            continue
        inline.append(item)

    return MicrocompactResult(inline_items=inline, offloaded_refs=refs, discarded_count=discarded)


def hydrate(item: ContextItem, *, artifact_store: ArtifactStore) -> ContextItem:
    if item.artifact_ref is None:
        raise ValueError("cannot hydrate a context item without artifact_ref")
    return item.model_copy(
        update={
            "content": artifact_store.read_text(item.artifact_ref),
            "hydration_turns_remaining": 1,
        }
    )


def _artifact_placeholder(ref: ArtifactRef) -> str:
    return f"[artifact_ref uri={ref.uri} kind={ref.kind} summary={ref.summary}]"
