from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from crazy_harness.core.evals.paired import (
    EvidenceTier,
    PairedEvalContract,
    RecommendationOutcome,
    TeamRecommendationDecision,
)

PPM = 1_000_000


class CampaignRecommendationPolicy(BaseModel):
    """所有数值都是初始治理门槛，必须由真实 Live Campaign 调优。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = "campaign-policy-v1"
    minimum_interval_trials: int = Field(default=5, ge=2)
    minimum_live_trials: int = Field(default=10, ge=2)
    minimum_success_delta_ppm: int = 0
    minimum_quality_delta_ppm: int = 10_000
    maximum_cost_ratio_ppm: int = Field(default=1_500_000, gt=0)
    maximum_duration_ratio_ppm: int = Field(default=1_500_000, gt=0)
    familywise_confidence_ppm: int = Field(default=950_000, gt=0, lt=1_000_000)
    bootstrap_resamples: int = Field(default=10_000, ge=100, le=100_000)

    @model_validator(mode="after")
    def live_gate_requires_an_interval(self) -> CampaignRecommendationPolicy:
        if self.minimum_live_trials < self.minimum_interval_trials:
            raise ValueError("minimum live trials cannot be below interval trials")
        return self

    def decide(
        self, evidence: CampaignRecommendationEvidence
    ) -> TeamRecommendationDecision:
        invalid = []
        if not evidence.scope_valid:
            invalid.append("scope_mismatch")
        if evidence.completed_trial_count != evidence.planned_trial_count:
            invalid.append("incomplete_trial_plan")
        if evidence.invalid_trial_count:
            invalid.append("invalid_trials")
        if evidence.aggregate is None:
            invalid.append("missing_aggregate")
        if invalid:
            return TeamRecommendationDecision(
                outcome=RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE,
                reason="campaign evidence is incomplete or invalid",
                failed_thresholds=tuple(invalid),
            )

        aggregate = evidence.aggregate
        if aggregate.hard_reliability_regression:
            return TeamRecommendationDecision(
                outcome=RecommendationOutcome.KEEP_SINGLE,
                reason="Team introduced a hard reliability regression",
                failed_thresholds=("hard_reliability_regression",),
            )

        if evidence.evidence_tier is not EvidenceTier.LIVE_PAIRED:
            scripted_gates = self._failed_scripted_veto_gates(aggregate)
            if scripted_gates:
                return TeamRecommendationDecision(
                    outcome=RecommendationOutcome.KEEP_SINGLE,
                    reason="Team regressed under deterministic campaign evidence",
                    failed_thresholds=scripted_gates,
                )
            return TeamRecommendationDecision(
                outcome=RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE,
                reason="deterministic campaigns can veto regressions but cannot promote Team",
            )

        point_gates = self._failed_point_gates(aggregate)
        if point_gates:
            return TeamRecommendationDecision(
                outcome=RecommendationOutcome.KEEP_SINGLE,
                reason="Team failed at least one campaign point-estimate gate",
                failed_thresholds=point_gates,
            )
        if aggregate.sample_count < self.minimum_live_trials:
            return TeamRecommendationDecision(
                outcome=RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE,
                reason="valid live trial count is below the initial promotion minimum",
                failed_thresholds=("minimum_live_trials",),
            )
        confidence_gates = self._failed_confidence_gates(aggregate)
        if confidence_gates:
            return TeamRecommendationDecision(
                outcome=RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE,
                reason="point estimates pass, but family-wise promotion bounds do not",
                failed_thresholds=confidence_gates,
            )
        return TeamRecommendationDecision(
            outcome=RecommendationOutcome.RECOMMEND_TEAM,
            reason="Team passed every point and family-wise confidence gate",
        )

    def _failed_scripted_veto_gates(
        self, aggregate: CampaignAggregate
    ) -> tuple[str, ...]:
        """Scripted 只证明明确退化，不把“未达到晋升幅度”伪装成回归。"""

        failed = []
        if (
            aggregate.success_rate_delta.point_ppm is None
            or aggregate.success_rate_delta.point_ppm < 0
        ):
            failed.append("scripted_success_regression")
        if (
            aggregate.quality_delta.point_ppm is None
            or aggregate.quality_delta.point_ppm < 0
        ):
            failed.append("scripted_quality_regression")
        if (
            aggregate.cost_ratio.point_ppm is None
            or aggregate.cost_ratio.point_ppm > self.maximum_cost_ratio_ppm
        ):
            failed.append("scripted_cost_regression")
        if (
            aggregate.duration_ratio.point_ppm is None
            or aggregate.duration_ratio.point_ppm
            > self.maximum_duration_ratio_ppm
        ):
            failed.append("scripted_duration_regression")
        return tuple(failed)

    def _failed_point_gates(self, aggregate: CampaignAggregate) -> tuple[str, ...]:
        failed = []
        values = (
            (
                "minimum_success_delta_ppm",
                aggregate.success_rate_delta.point_ppm,
                self.minimum_success_delta_ppm,
                "minimum",
            ),
            (
                "minimum_quality_delta_ppm",
                aggregate.quality_delta.point_ppm,
                self.minimum_quality_delta_ppm,
                "minimum",
            ),
            (
                "maximum_cost_ratio_ppm",
                aggregate.cost_ratio.point_ppm,
                self.maximum_cost_ratio_ppm,
                "maximum",
            ),
            (
                "maximum_duration_ratio_ppm",
                aggregate.duration_ratio.point_ppm,
                self.maximum_duration_ratio_ppm,
                "maximum",
            ),
        )
        for name, value, threshold, direction in values:
            if value is None or (
                direction == "minimum" and value < threshold
            ) or (direction == "maximum" and value > threshold):
                failed.append(name)
        return tuple(failed)

    def _failed_confidence_gates(
        self, aggregate: CampaignAggregate
    ) -> tuple[str, ...]:
        failed = []
        bounds = (
            (
                "success_confidence_bound",
                aggregate.success_rate_delta.lower_bound_ppm,
                self.minimum_success_delta_ppm,
                "minimum",
            ),
            (
                "quality_confidence_bound",
                aggregate.quality_delta.lower_bound_ppm,
                self.minimum_quality_delta_ppm,
                "minimum",
            ),
            (
                "cost_confidence_bound",
                aggregate.cost_ratio.upper_bound_ppm,
                self.maximum_cost_ratio_ppm,
                "maximum",
            ),
            (
                "duration_confidence_bound",
                aggregate.duration_ratio.upper_bound_ppm,
                self.maximum_duration_ratio_ppm,
                "maximum",
            ),
        )
        for name, value, threshold, direction in bounds:
            if value is None or (
                direction == "minimum" and value < threshold
            ) or (direction == "maximum" and value > threshold):
                failed.append(name)
        return tuple(failed)


class CampaignTrialPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_index: int = Field(ge=1, le=30)
    pair_request_id: str = Field(min_length=8, max_length=128)
    eval_id: str = Field(min_length=1)


class CampaignScope(BaseModel):
    """跨 Pair 聚合时必须保持不变的实验语义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_pack: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    fixture_hash: str = Field(min_length=1)
    input_hash: str = Field(min_length=1)
    scorer_version: str = Field(min_length=1)
    evidence_tier: EvidenceTier
    model_profile: dict[str, JsonValue] = Field(min_length=1)
    model_budget: dict[str, JsonValue] = Field(min_length=1)
    harness_profile: dict[str, JsonValue] = Field(min_length=1)

    @classmethod
    def from_pair_contract(cls, contract: PairedEvalContract) -> CampaignScope:
        if not contract.harness_profile:
            raise ValueError("campaign pair has no persisted harness profile")
        return cls(
            task_pack=contract.task_pack,
            case_id=contract.case_id,
            fixture_hash=contract.fixture_hash,
            input_hash=contract.single.input_hash,
            scorer_version=contract.scorer_version,
            evidence_tier=contract.evidence_tier,
            model_profile=contract.single.model_profile,
            model_budget=contract.single.model_budget,
            harness_profile=contract.harness_profile,
        )

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


class CampaignBudgetEnvelope(BaseModel):
    """父实验对全部 Single/Team Run 的静态最坏预算证明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_count: int = Field(ge=1, le=30)
    per_arm_max_tokens: int = Field(ge=1)
    per_arm_max_cost_usd: Decimal = Field(gt=0)
    max_total_tokens: int = Field(ge=1)
    max_total_cost_usd: Decimal = Field(gt=0)

    @property
    def required_total_tokens(self) -> int:
        return 2 * self.trial_count * self.per_arm_max_tokens

    @property
    def required_total_cost_usd(self) -> Decimal:
        return 2 * self.trial_count * self.per_arm_max_cost_usd

    @model_validator(mode="after")
    def cap_covers_every_planned_arm(self) -> CampaignBudgetEnvelope:
        if self.required_total_tokens > self.max_total_tokens:
            raise ValueError("campaign token cap does not cover every planned arm")
        if self.required_total_cost_usd > self.max_total_cost_usd:
            raise ValueError("campaign cost cap does not cover every planned arm")
        return self


class EvalCampaignContract(BaseModel):
    """任何子 Pair 执行前必须持久化的不可变实验计划。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "eval-campaign-v1"
    aggregator_version: str = "paired-bootstrap-v1"
    campaign_id: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(min_length=1, max_length=4000)
    task_pack: str = Field(min_length=1)
    model_mode: Literal["scripted", "deepseek"]
    evidence_tier: EvidenceTier
    planned_trial_count: int = Field(ge=1, le=30)
    trials: tuple[CampaignTrialPlan, ...] = Field(min_length=1, max_length=30)
    pair_model_budget: dict[str, JsonValue] = Field(min_length=1)
    budget: CampaignBudgetEnvelope
    max_parallel_pairs: int = Field(default=1, ge=1, le=8)
    policy: CampaignRecommendationPolicy = Field(
        default_factory=CampaignRecommendationPolicy
    )

    @model_validator(mode="after")
    def validate_pre_registered_plan(self) -> EvalCampaignContract:
        if len(self.trials) != self.planned_trial_count:
            raise ValueError("planned trial count does not match trial identities")
        if self.budget.trial_count != self.planned_trial_count:
            raise ValueError("campaign budget does not match planned trial count")
        if int(self.pair_model_budget.get("max_total_tokens", 0)) != (
            self.budget.per_arm_max_tokens
        ):
            raise ValueError("campaign pair token budget differs from its envelope")
        if Decimal(str(self.pair_model_budget.get("max_cost_usd", "0"))) != (
            self.budget.per_arm_max_cost_usd
        ):
            raise ValueError("campaign pair cost budget differs from its envelope")
        if self.max_parallel_pairs > self.planned_trial_count:
            raise ValueError("parallel pair limit cannot exceed planned trials")
        expected_indices = list(range(1, self.planned_trial_count + 1))
        if [trial.trial_index for trial in self.trials] != expected_indices:
            raise ValueError("campaign trial indices must be ordered and contiguous")
        request_ids = [trial.pair_request_id for trial in self.trials]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("campaign trials require unique pair_request_id values")
        eval_ids = [trial.eval_id for trial in self.trials]
        if len(eval_ids) != len(set(eval_ids)):
            raise ValueError("campaign trials require unique eval_id values")
        expected_tier = (
            EvidenceTier.DETERMINISTIC
            if self.model_mode == "scripted"
            else EvidenceTier.LIVE_PAIRED
        )
        if self.evidence_tier is not expected_tier:
            raise ValueError("campaign evidence tier does not match model mode")
        return self


class PairedTrialSample(BaseModel):
    """一个 Pair 的不可拆分统计样本，只接受已验证的机器事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_index: int = Field(ge=1, le=30)
    eval_id: str = Field(min_length=1)
    pair_report_sha256: str = Field(min_length=1)
    single_run_id: str = Field(min_length=1)
    team_run_id: str = Field(min_length=1)
    single_terminal_event_id: str = Field(min_length=1)
    team_terminal_event_id: str = Field(min_length=1)
    single_success: bool
    team_success: bool
    single_quality_ppm: int = Field(ge=0, le=PPM)
    team_quality_ppm: int = Field(ge=0, le=PPM)
    single_committed_cost_microusd: int = Field(ge=0)
    team_committed_cost_microusd: int = Field(ge=0)
    single_duration_ms: int = Field(ge=0)
    team_duration_ms: int = Field(ge=0)
    hard_reliability_regression: bool = False

    @model_validator(mode="after")
    def identities_are_distinct(self) -> PairedTrialSample:
        if self.single_run_id == self.team_run_id:
            raise ValueError("trial sample requires distinct run identities")
        if self.single_terminal_event_id == self.team_terminal_event_id:
            raise ValueError("trial sample requires distinct terminal events")
        return self


class CampaignMetricEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: Literal[
        "success_rate_delta",
        "quality_delta",
        "cost_ratio",
        "duration_ratio",
    ]
    point_ppm: int | None
    lower_bound_ppm: int | None
    upper_bound_ppm: int | None
    sample_size: int = Field(ge=1)
    method: Literal["point_only", "deterministic_paired_percentile"]
    resamples: int = Field(ge=0)
    familywise_confidence_ppm: int = Field(gt=0, lt=PPM)
    unbounded: bool = False

    @model_validator(mode="after")
    def bounds_match_method(self) -> CampaignMetricEstimate:
        has_bounds = (
            self.lower_bound_ppm is not None and self.upper_bound_ppm is not None
        )
        if self.method == "point_only" and (has_bounds or self.resamples):
            raise ValueError("point-only metric cannot contain bootstrap bounds")
        if self.method != "point_only" and not self.unbounded and not has_bounds:
            raise ValueError("bounded bootstrap metric requires both bounds")
        return self


class CampaignAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    aggregator_version: str
    sample_count: int = Field(ge=1)
    samples_sha256: str = Field(min_length=1)
    bootstrap_seed_sha256: str = Field(min_length=1)
    hard_reliability_regression: bool
    success_rate_delta: CampaignMetricEstimate
    quality_delta: CampaignMetricEstimate
    cost_ratio: CampaignMetricEstimate
    duration_ratio: CampaignMetricEstimate


class CampaignRecommendationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_tier: EvidenceTier
    planned_trial_count: int = Field(ge=1)
    completed_trial_count: int = Field(ge=0)
    invalid_trial_count: int = Field(ge=0)
    scope_valid: bool
    aggregate: CampaignAggregate | None

    @model_validator(mode="after")
    def counts_match_aggregate(self) -> CampaignRecommendationEvidence:
        if self.completed_trial_count > self.planned_trial_count:
            raise ValueError("completed trials cannot exceed the campaign plan")
        if self.invalid_trial_count > self.completed_trial_count:
            raise ValueError("invalid trials cannot exceed completed trials")
        valid_count = self.completed_trial_count - self.invalid_trial_count
        if self.aggregate is not None and self.aggregate.sample_count != valid_count:
            raise ValueError("campaign aggregate sample count does not match valid trials")
        return self


MetricFunction = Callable[[Sequence[PairedTrialSample]], int | None]


class CampaignAggregator:
    VERSION = "paired-bootstrap-v1"

    def aggregate(
        self,
        *,
        samples: Sequence[PairedTrialSample],
        policy: CampaignRecommendationPolicy,
        seed_material: str,
    ) -> CampaignAggregate:
        ordered = tuple(sorted(samples, key=lambda item: item.trial_index))
        self._validate_samples(ordered)
        serialized = json.dumps(
            [sample.model_dump(mode="json") for sample in ordered],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        samples_hash = sha256(serialized).hexdigest()
        seed = sha256(f"{seed_material}:{self.VERSION}".encode("utf-8")).hexdigest()
        metrics: tuple[tuple[str, MetricFunction], ...] = (
            ("success_rate_delta", self._success_delta),
            ("quality_delta", self._quality_delta),
            ("cost_ratio", self._cost_ratio),
            ("duration_ratio", self._duration_ratio),
        )
        estimates = {
            name: self._estimate(
                name=name,
                point=measure(ordered),
                measure=measure,
                samples=ordered,
                policy=policy,
                seed=seed,
            )
            for name, measure in metrics
        }
        return CampaignAggregate(
            aggregator_version=self.VERSION,
            sample_count=len(ordered),
            samples_sha256=samples_hash,
            bootstrap_seed_sha256=seed,
            hard_reliability_regression=any(
                sample.hard_reliability_regression for sample in ordered
            ),
            **estimates,
        )

    @staticmethod
    def _validate_samples(samples: tuple[PairedTrialSample, ...]) -> None:
        if not samples:
            raise ValueError("campaign aggregation requires at least one trial")
        if [sample.trial_index for sample in samples] != list(
            range(1, len(samples) + 1)
        ):
            raise ValueError("campaign samples must cover contiguous trial indices")
        CampaignAggregator._require_unique(
            (sample.eval_id for sample in samples), "eval_id"
        )
        CampaignAggregator._require_unique(
            (
                run_id
                for sample in samples
                for run_id in (sample.single_run_id, sample.team_run_id)
            ),
            "run_id",
        )
        CampaignAggregator._require_unique(
            (
                event_id
                for sample in samples
                for event_id in (
                    sample.single_terminal_event_id,
                    sample.team_terminal_event_id,
                )
            ),
            "terminal_event_id",
        )

    @staticmethod
    def _require_unique(values: Sequence[str] | object, label: str) -> None:
        materialized = tuple(values)  # type: ignore[arg-type]
        if len(materialized) != len(set(materialized)):
            raise ValueError(f"campaign samples require unique {label} values")

    def _estimate(
        self,
        *,
        name: str,
        point: int | None,
        measure: MetricFunction,
        samples: tuple[PairedTrialSample, ...],
        policy: CampaignRecommendationPolicy,
        seed: str,
    ) -> CampaignMetricEstimate:
        metric = name  # Pydantic validates the literal metric vocabulary.
        if len(samples) < policy.minimum_interval_trials:
            return CampaignMetricEstimate(
                metric=metric,  # type: ignore[arg-type]
                point_ppm=point,
                lower_bound_ppm=None,
                upper_bound_ppm=None,
                sample_size=len(samples),
                method="point_only",
                resamples=0,
                familywise_confidence_ppm=policy.familywise_confidence_ppm,
                unbounded=point is None,
            )

        distribution: list[int] = []
        unbounded = point is None
        for round_index in range(policy.bootstrap_resamples):
            selected = tuple(
                samples[index]
                for index in self._resample_indices(
                    seed=seed,
                    size=len(samples),
                    round_index=round_index,
                )
            )
            value = measure(selected)
            if value is None:
                unbounded = True
            else:
                distribution.append(value)
        lower: int | None = None
        upper: int | None = None
        if not unbounded:
            distribution.sort()
            tail_ppm = (PPM - policy.familywise_confidence_ppm) // 4
            last = len(distribution) - 1
            lower_index = tail_ppm * last // PPM
            upper_index = ((PPM - tail_ppm) * last + PPM - 1) // PPM
            lower = distribution[lower_index]
            upper = distribution[min(upper_index, last)]
        return CampaignMetricEstimate(
            metric=metric,  # type: ignore[arg-type]
            point_ppm=point,
            lower_bound_ppm=lower,
            upper_bound_ppm=upper,
            sample_size=len(samples),
            method="deterministic_paired_percentile",
            resamples=policy.bootstrap_resamples,
            familywise_confidence_ppm=policy.familywise_confidence_ppm,
            unbounded=unbounded,
        )

    @staticmethod
    def _resample_indices(
        *, seed: str, size: int, round_index: int
    ) -> tuple[int, ...]:
        return tuple(
            int.from_bytes(
                sha256(f"{seed}:{round_index}:{slot}".encode("utf-8")).digest()[:8],
                "big",
            )
            % size
            for slot in range(size)
        )

    @staticmethod
    def _success_delta(samples: Sequence[PairedTrialSample]) -> int:
        total = sum(
            int(sample.team_success) - int(sample.single_success) for sample in samples
        )
        return _rounded_div(total * PPM, len(samples))

    @staticmethod
    def _quality_delta(samples: Sequence[PairedTrialSample]) -> int:
        total = sum(
            sample.team_quality_ppm - sample.single_quality_ppm
            for sample in samples
        )
        return _rounded_div(total, len(samples))

    @staticmethod
    def _cost_ratio(samples: Sequence[PairedTrialSample]) -> int | None:
        return _ratio_ppm(
            sum(sample.team_committed_cost_microusd for sample in samples),
            sum(sample.single_committed_cost_microusd for sample in samples),
        )

    @staticmethod
    def _duration_ratio(samples: Sequence[PairedTrialSample]) -> int | None:
        return _ratio_ppm(
            sum(sample.team_duration_ms for sample in samples),
            sum(sample.single_duration_ms for sample in samples),
        )


def _ratio_ppm(numerator: int, denominator: int) -> int | None:
    if denominator == 0:
        return PPM if numerator == 0 else None
    return _rounded_div(numerator * PPM, denominator)


def _rounded_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("rounded division requires a positive denominator")
    sign = -1 if numerator < 0 else 1
    return sign * ((abs(numerator) + denominator // 2) // denominator)
