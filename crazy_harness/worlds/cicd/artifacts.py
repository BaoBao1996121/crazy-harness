from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RiskReport(BaseModel):
    risks: list[str] = Field(default_factory=list)
    missing_context: list[str] = Field(default_factory=list)
    recommended_checks: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    notes: str = ""


class ReleasePlan(BaseModel):
    steps: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"]
    approval_required: bool
    rollback_plan: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    notes: str = ""


class ToolExecutionResult(BaseModel):
    tool_name: str
    args_ref: str | None = None
    status: Literal["ok", "error", "skipped"]
    output_ref: str | None = None
    summary: str = ""
    error: str | None = None


class ReviewDecision(BaseModel):
    decision: Literal["approve", "reject", "needs_human"]
    reasons: list[str] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    notes: str = ""


class RunReport(BaseModel):
    task_id: str
    final_status: str
    timeline_summary: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    lessons_learned: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class ProgressReport(BaseModel):
    claims: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    unverified_claims: list[str] = Field(default_factory=list)
    next_action: str = ""


class UserNotice(BaseModel):
    message: str
    reason: str
    task_id: str
    evidence_refs: list[str] = Field(default_factory=list)
