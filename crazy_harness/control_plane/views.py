from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.control_plane.store import EventRecord


class _View(BaseModel):
    model_config = ConfigDict(extra="allow")


class RunView(_View):
    run_id: str
    task_id: str
    title: str
    brief: str = ""
    status: str
    phase: str
    model_mode: str
    behavior_version: str
    event_count: int = 0
    last_cursor: int = 0
    completion_gate: str | None = None


class AgentView(_View):
    agent_id: str
    role: str
    capabilities: list[str] = Field(default_factory=list)
    status: str
    max_concurrency: int = 1
    last_heartbeat_at: str | None = None
    active_assignment_id: str | None = None
    mailbox_pending: int = 0
    in_flight: int = 0
    active_run_id: str | None = None
    last_error: str | None = None


class AssignmentView(_View):
    assignment_id: str
    run_id: str
    task_id: str
    agent_id: str
    goal: str
    exit_criteria: list[str] = Field(default_factory=list)
    status: str


class LeaseView(_View):
    lease_id: str
    assignment_id: str
    run_id: str
    task_id: str
    agent_id: str
    status: str
    stage_id: str | None = None
    acquired_at: str
    expires_at: str
    renewed_at: str | None = None
    released_at: str | None = None
    expired_at: str | None = None


class ContextView(_View):
    run_id: str
    task_id: str
    agent_id: str
    trigger_event_id: str
    context_epoch: int
    manifest: dict[str, Any]
    microcompact: dict[str, int]
    message_preview: list[dict[str, str]] = Field(default_factory=list)


class CapabilityManifestView(_View):
    run_id: str
    task_id: str
    turn_id: str
    agent_id: str
    assignment_id: str
    strategy: str
    catalog_size: int = 0
    disclosed_count: int = 0
    withheld_count: int = 0
    excluded_count: int = 0
    manifest: dict[str, Any]


class MemoryView(_View):
    candidate_id: str
    run_id: str
    task_id: str
    slot: str | None = None
    content: str | None = None
    scope: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float | None = None
    risk: str | None = None
    admission_zone: str | None = None
    status: str


class EvolutionView(_View):
    candidate_id: str
    run_id: str
    task_id: str
    base_version: str | None = None
    proposed_version: str | None = None
    rationale: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    diffs: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    next_gate: str | None = None
    status: str


class DreamJobView(_View):
    job_id: str
    run_id: str
    task_id: str
    status: str
    signal_id: str | None = None
    memory_candidate_id: str | None = None


class RuntimeView(_View):
    status: str
    latest_event_id: str | None = None
    deepseek_configured: bool
    fact_source: str
    generated_at: str
    scheduler: dict[str, Any]


class QueuedDeliveryView(_View):
    delivery_id: str
    worker_id: str
    run_id: str
    task_id: str
    event_type: str
    assignment_id: str | None = None
    stage_id: str | None = None
    position: int = Field(ge=1)
    claim_state: str | None = None
    fencing_token: int | None = None


class WorkClaimView(_View):
    claim_key: str
    owner_id: str
    fencing_token: int = Field(ge=1)
    state: str
    claimed_at: str
    expires_at: str
    updated_at: str


class SnapshotView(BaseModel):
    run: RunView | None
    agents: list[AgentView]
    assignments: list[AssignmentView]
    leases: list[LeaseView]
    contexts: list[ContextView]
    capability_manifests: list[CapabilityManifestView]
    memories: list[MemoryView]
    evolutions: list[EvolutionView]
    dream_jobs: list[DreamJobView]
    queued_deliveries: list[QueuedDeliveryView]
    work_claims: list[WorkClaimView]
    runtime: RuntimeView


class EventPage(BaseModel):
    items: list[EventRecord]
    next_cursor: int = Field(ge=0)


class HealthView(BaseModel):
    status: str
    runtime: RuntimeView
    version: str


class DrainResult(BaseModel):
    run_id: str
    steps: int = Field(ge=0)


class CancelResult(BaseModel):
    run_id: str
    status: str
    active_cancelled: int = Field(ge=0)
    queued_cancelled: int = Field(ge=0)


class FaultResult(BaseModel):
    armed: str
    mode: str


class RebuildResult(BaseModel):
    status: str
