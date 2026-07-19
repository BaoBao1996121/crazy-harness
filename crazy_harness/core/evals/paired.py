from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from crazy_harness.core.events import Event


class EvidenceTier(StrEnum):
    DETERMINISTIC = "deterministic"
    LIVE_PAIRED = "live_paired"


class PairedEvalArm(BaseModel):
    """Immutable identity and resource envelope for one side of an eval pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_mode: Literal["single", "team"]
    run_id: str = Field(min_length=1)
    workspace: Path
    input_hash: str = Field(min_length=1)
    model_profile: dict[str, JsonValue] = Field(min_length=1)
    model_budget: dict[str, JsonValue] = Field(min_length=1)

    @field_validator("workspace")
    @classmethod
    def canonicalize_workspace(cls, value: Path) -> Path:
        return value.resolve()


class PairedEvalContract(BaseModel):
    """Fail-closed proof that Single and Team are mechanically comparable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    task_pack: str = Field(min_length=1)
    fixture_hash: str = Field(min_length=1)
    scorer_version: str = Field(min_length=1)
    evidence_tier: EvidenceTier
    single: PairedEvalArm
    team: PairedEvalArm

    @model_validator(mode="after")
    def validate_fair_pair(self) -> PairedEvalContract:
        if self.single.execution_mode != "single":
            raise ValueError("single arm must use execution_mode=single")
        if self.team.execution_mode != "team":
            raise ValueError("team arm must use execution_mode=team")
        if self.single.run_id == self.team.run_id:
            raise ValueError("paired arms must use different run_id values")
        if self.single.workspace == self.team.workspace:
            raise ValueError("paired arms must use different workspaces")
        for field_name in ("input_hash", "model_profile", "model_budget"):
            if getattr(self.single, field_name) != getattr(self.team, field_name):
                raise ValueError(f"paired arms must have identical {field_name}")
        return self


class RunTraceMetrics(BaseModel):
    """Replay-stable metrics derived only from durable events and budget facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    terminal_status: Literal["succeeded", "failed", "cancelled"]
    terminal_event_id: str
    duration_ms: int = Field(ge=0)
    model_requests: int = Field(ge=0)
    model_completions: int = Field(ge=0)
    physical_model_attempts: int = Field(ge=0)
    tool_requests: int = Field(ge=0)
    tool_completions: int = Field(ge=0)
    operations_started: int = Field(ge=0)
    operations_completed: int = Field(ge=0)
    a2a_requests: int = Field(ge=0)
    a2a_responses: int = Field(ge=0)
    assignment_failures: int = Field(ge=0)
    assignment_retries: int = Field(ge=0)
    operation_unknowns: int = Field(ge=0)
    model_unknown_calls: int = Field(ge=0)
    dead_letters: int = Field(ge=0)
    spent_tokens: int = Field(ge=0)
    committed_tokens: int = Field(ge=0)
    spent_cost_microusd: int = Field(ge=0)
    committed_cost_microusd: int = Field(ge=0)


class _BudgetStatus(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    run_id: str = Field(min_length=1)
    spent_tokens: int = Field(ge=0)
    committed_tokens: int = Field(ge=0)
    estimated_spent_microusd: int = Field(ge=0)
    committed_cost_microusd: int = Field(ge=0)
    unknown_calls: int = Field(ge=0)

    @model_validator(mode="after")
    def committed_totals_cover_spend(self) -> _BudgetStatus:
        if self.committed_tokens < self.spent_tokens:
            raise ValueError("committed_tokens cannot be lower than spent_tokens")
        if self.committed_cost_microusd < self.estimated_spent_microusd:
            raise ValueError("committed cost cannot be lower than spent cost")
        return self


class RunTraceAggregator:
    TERMINAL_TYPES = {
        "run.succeeded": "succeeded",
        "run.failed": "failed",
        "run.cancelled": "cancelled",
    }

    def aggregate(
        self,
        *,
        events: Sequence[Event],
        model_budget_status: Mapping[str, Any],
    ) -> RunTraceMetrics:
        trace = tuple(events)
        run_ids = {event.run_id for event in trace}
        if len(run_ids) != 1:
            raise ValueError("trace must contain events from exactly one run")
        run_id = next(iter(run_ids))
        created = [event for event in trace if event.type == "run.created"]
        if len(created) != 1:
            raise ValueError("trace must contain exactly one run.created event")
        terminals = [event for event in trace if event.type in self.TERMINAL_TYPES]
        if len(terminals) != 1:
            raise ValueError("trace must contain exactly one trusted run terminal")
        terminal = terminals[0]
        elapsed = terminal.created_at - created[0].created_at
        if elapsed.total_seconds() < 0:
            raise ValueError("run terminal cannot precede run.created")

        budget = _BudgetStatus.model_validate(model_budget_status)
        if budget.run_id != run_id:
            raise ValueError("budget status run_id does not match trace run_id")

        counts = Counter(event.type for event in trace)
        return RunTraceMetrics(
            run_id=run_id,
            terminal_status=self.TERMINAL_TYPES[terminal.type],
            terminal_event_id=terminal.id,
            duration_ms=elapsed // timedelta(milliseconds=1),
            model_requests=counts["model.requested"],
            model_completions=counts["model.completed"],
            physical_model_attempts=counts["model.call.attempt.started"],
            tool_requests=counts["tool.requested"],
            tool_completions=counts["tool.completed"],
            operations_started=counts["operation.started"],
            operations_completed=counts["operation.completed"],
            a2a_requests=counts["a2a.peer.requested"],
            a2a_responses=counts["a2a.peer.responded"],
            assignment_failures=counts["assignment.failed"],
            assignment_retries=sum(
                event.type == "assignment.created"
                and _assignment_attempt(event) > 1
                for event in trace
            ),
            operation_unknowns=counts["operation.unknown"],
            model_unknown_calls=budget.unknown_calls,
            dead_letters=counts["mailbox.delivery.dead_lettered"],
            spent_tokens=budget.spent_tokens,
            committed_tokens=budget.committed_tokens,
            spent_cost_microusd=budget.estimated_spent_microusd,
            committed_cost_microusd=budget.committed_cost_microusd,
        )


def _assignment_attempt(event: Event) -> int:
    value = event.payload.get("attempt", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("assignment.created attempt must be a positive integer")
    return value


class RecommendationOutcome(StrEnum):
    INSUFFICIENT_LIVE_EVIDENCE = "insufficient_live_evidence"
    RECOMMEND_TEAM = "recommend_team"
    KEEP_SINGLE = "keep_single"


class TeamRecommendationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    evidence_tier: EvidenceTier
    paired_live_trials: int = Field(ge=0)
    success_rate_delta: float
    quality_delta: float
    cost_ratio: float = Field(ge=0)
    duration_ratio: float = Field(ge=0)
    hard_reliability_regression: bool = False


class TeamRecommendationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: RecommendationOutcome
    reason: str
    failed_thresholds: tuple[str, ...] = ()


class TeamRecommendationPolicy(BaseModel):
    """Initial gates only; every threshold must be tuned with real paired data."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    minimum_live_trials: int = Field(
        default=5, ge=2, description="Initial value; tune with live paired trials."
    )
    minimum_success_rate_delta: float = Field(
        default=0.0, description="Initial value; negative means reliability regression."
    )
    minimum_quality_delta: float = Field(
        default=0.01, description="Initial value; assumes a normalized quality score."
    )
    maximum_cost_ratio: float = Field(
        default=1.5, gt=0, description="Initial value; Team cost divided by Single cost."
    )
    maximum_duration_ratio: float = Field(
        default=1.5, gt=0, description="Initial value; Team duration divided by Single."
    )

    def decide(
        self, evidence: TeamRecommendationEvidence
    ) -> TeamRecommendationDecision:
        if evidence.evidence_tier is not EvidenceTier.LIVE_PAIRED:
            return TeamRecommendationDecision(
                outcome=RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE,
                reason="deterministic evidence cannot establish a Team advantage",
            )
        if evidence.paired_live_trials < self.minimum_live_trials:
            return TeamRecommendationDecision(
                outcome=RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE,
                reason="paired live trial count is below the initial minimum",
            )

        failed = []
        if evidence.hard_reliability_regression:
            failed.append("hard_reliability_regression")
        if evidence.success_rate_delta < self.minimum_success_rate_delta:
            failed.append("minimum_success_rate_delta")
        if evidence.quality_delta < self.minimum_quality_delta:
            failed.append("minimum_quality_delta")
        if evidence.cost_ratio > self.maximum_cost_ratio:
            failed.append("maximum_cost_ratio")
        if evidence.duration_ratio > self.maximum_duration_ratio:
            failed.append("maximum_duration_ratio")
        if failed:
            return TeamRecommendationDecision(
                outcome=RecommendationOutcome.KEEP_SINGLE,
                reason="Team did not pass every initial recommendation gate",
                failed_thresholds=tuple(failed),
            )
        return TeamRecommendationDecision(
            outcome=RecommendationOutcome.RECOMMEND_TEAM,
            reason="Team passed every initial gate with sufficient live evidence",
        )
