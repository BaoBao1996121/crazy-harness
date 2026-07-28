from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class PromotionMode(StrEnum):
    AUTO_DISPOSABLE = "auto_disposable"
    APPROVAL_REQUIRED = "approval_required"
    CANDIDATE_ONLY = "candidate_only"


class LoopDecisionKind(StrEnum):
    ACCEPT_CONTINUE = "accept_continue"
    REJECT_CONTINUE = "reject_continue"
    COMPLETE = "complete"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"


class WorkerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_mode: Literal["single", "team"]
    model_mode: Literal["scripted", "deepseek"]
    task_pack: str = Field(min_length=1)
    model_budget: dict[str, JsonValue] = Field(default_factory=dict)


class MetricContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    direction: MetricDirection
    target: Decimal
    evaluator_version: str = Field(min_length=1)


class EngineeringLoopBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(default=3, ge=1, le=100)
    max_no_progress_iterations: int = Field(default=2, ge=0, le=100)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_total_cost_usd: Decimal | None = Field(default=None, gt=0)
    max_wall_time_seconds: int | None = Field(default=None, ge=1)


class EngineeringLoopContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["engineering-loop-v1"] = "engineering-loop-v1"
    loop_id: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=4000)
    exit_criteria: tuple[str, ...] = Field(min_length=1)
    loop_pack: str = Field(min_length=1)
    worker: WorkerProfile
    metric: MetricContract
    budget: EngineeringLoopBudget
    promotion_mode: PromotionMode = PromotionMode.AUTO_DISPOSABLE
    permissions: tuple[str, ...] = ()
    initial_state_ref: str = Field(min_length=1)


class EngineeringIterationIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    iteration: int = Field(ge=1)
    iteration_id: str = Field(min_length=1)
    child_run_id: str = Field(min_length=1)
    child_task_id: str = Field(min_length=1)


def engineering_iteration_identity(
    loop_id: str,
    iteration: int,
) -> EngineeringIterationIdentity:
    if not loop_id.strip():
        raise ValueError("loop_id must not be empty")
    if iteration < 1:
        raise ValueError("iteration must be positive")
    key = f"crazy:engineering-loop:{loop_id}:iteration:{iteration}"
    return EngineeringIterationIdentity(
        iteration=iteration,
        iteration_id=f"iteration_{uuid5(NAMESPACE_URL, key).hex[:16]}",
        child_run_id=f"run_{uuid5(NAMESPACE_URL, key + ':run').hex[:12]}",
        child_task_id=f"task_{uuid5(NAMESPACE_URL, key + ':task').hex[:12]}",
    )


class CandidateProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    base_state_ref: str = Field(min_length=1)
    change_set: dict[str, JsonValue] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    expected_effect: str = Field(min_length=1)
    proposer_attestation: dict[str, JsonValue] = Field(min_length=1)

    def validate_base(self, active_state_ref: str) -> None:
        if self.base_state_ref != active_state_ref:
            raise ValueError("candidate is not based on the current active state")


class IterationEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    iteration: int = Field(ge=1)
    candidate_id: str = Field(min_length=1)
    candidate_state_ref: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    metrics: dict[str, Decimal] = Field(min_length=1)
    hard_gates: dict[str, bool] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    valid: bool


class LoopDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: LoopDecisionKind
    iteration: int = Field(ge=1)
    candidate_id: str = Field(min_length=1)
    accepted: bool
    score: Decimal | None = None
    active_state_ref: str | None = None
    pending_state_ref: str | None = None
    next_no_progress_count: int = Field(ge=0)
    reason: str = Field(min_length=1)
