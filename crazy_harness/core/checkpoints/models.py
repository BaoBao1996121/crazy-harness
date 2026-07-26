from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceFileEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspaceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["workspace-snapshot-v1"] = "workspace-snapshot-v1"
    object_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    files: tuple[WorkspaceFileEntry, ...] = ()


class VerifiedArtifactRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    summary: str = ""
    source_event_id: str = Field(min_length=1)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CheckpointStateRefs(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_created_event_id: str = Field(min_length=1)
    assignment_event_id: str | None = None
    local_plan_event_id: str | None = None
    context_manifest_event_id: str | None = None
    artifacts: tuple[VerifiedArtifactRef, ...] = ()


class CheckpointEffect(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    terminal_state: Literal["completed", "failed", "unknown"]
    side_effect_level: str = Field(min_length=1)
    disposition: Literal[
        "read_only",
        "workspace_restored",
        "no_effect",
        "requires_reconciliation",
        "irreversible",
        "unknown",
    ]
    idempotency_key: str | None = None


class CheckpointEffectBoundary(BaseModel):
    model_config = ConfigDict(frozen=True)

    effects: tuple[CheckpointEffect, ...] = ()
    restore_blockers: tuple[str, ...] = ()


class CheckpointSourceBoundary(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    event_cursor: int = Field(ge=1)
    event_count: int = Field(ge=1)
    event_prefix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    turn_id: str | None = None
    phase: str = Field(min_length=1)


class CheckpointContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["composite-checkpoint-v1"] = "composite-checkpoint-v1"
    checkpoint_id: str = Field(pattern=r"^checkpoint_[0-9a-f]{32}$")
    request_id: str = Field(min_length=1, max_length=128)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    label: str = Field(default="", max_length=200)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    task_pack: str = Field(min_length=1)
    baseline_identity: str = Field(min_length=1)
    source: CheckpointSourceBoundary
    workspace: WorkspaceSnapshot
    state_refs: CheckpointStateRefs
    effects: CheckpointEffectBoundary
    restore_policy: Literal["fork_only"] = "fork_only"
    context_policy: Literal["replan_from_verified_facts"] = "replan_from_verified_facts"
    unknown_effect_policy: Literal["block"] = "block"
