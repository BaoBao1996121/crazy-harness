from __future__ import annotations

import errno
import json
import sqlite3
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from crazy_harness.control_plane.model_governance import ModelBudgetConfig
from crazy_harness.control_plane.paired_evals import (
    PairedEvalCreated,
    PairedEvalReport,
    PairedEvalRequest,
    paired_eval_id,
)
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.evals import (
    CampaignAggregate,
    CampaignAggregator,
    CampaignBudgetEnvelope,
    CampaignRecommendationEvidence,
    CampaignRecommendationPolicy,
    CampaignScope,
    CampaignTrialPlan,
    EvalCampaignContract,
    EvidenceTier,
    PairedEvalContract,
    PairedTrialSample,
    TeamRecommendationDecision,
)
from crazy_harness.core.events import Event


CancelPair = Callable[[str], object]


class EvalCampaignIdempotencyConflict(ValueError):
    code = "eval_campaign_idempotency_conflict"


class EvalCampaignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(min_length=1, max_length=4000)
    model_mode: Literal["scripted", "deepseek"] = "scripted"
    task_pack: Literal["repo-maintainer"] = "repo-maintainer"
    trial_count: int = Field(default=3, ge=1, le=30)
    max_parallel_pairs: int = Field(default=1, ge=1, le=8)
    model_budget: ModelBudgetConfig = Field(default_factory=ModelBudgetConfig)
    campaign_max_total_tokens: int = Field(default=2_500_000, ge=1)
    campaign_max_cost_usd: Decimal = Field(default=Decimal("1.00"), gt=0)

    @model_validator(mode="after")
    def validate_campaign_envelope(self) -> EvalCampaignRequest:
        if self.max_parallel_pairs > self.trial_count:
            raise ValueError("parallel pair limit cannot exceed trial count")
        self.budget_envelope()
        return self

    def budget_envelope(self) -> CampaignBudgetEnvelope:
        return CampaignBudgetEnvelope(
            trial_count=self.trial_count,
            per_arm_max_tokens=self.model_budget.max_total_tokens,
            per_arm_max_cost_usd=self.model_budget.max_cost_usd,
            max_total_tokens=self.campaign_max_total_tokens,
            max_total_cost_usd=self.campaign_max_cost_usd,
        )


class EvalCampaignCreated(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str
    status: Literal["queued"] = "queued"
    planned_trial_count: int
    budget: CampaignBudgetEnvelope


class CampaignTrialSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_index: int = Field(ge=1, le=30)
    eval_id: str = Field(min_length=1)
    status: Literal[
        "planned",
        "started",
        "linked",
        "released",
        "observed",
        "creation_failed",
    ]
    evidence_valid: bool | None = None
    invalid_reasons: tuple[str, ...] = ()
    sample: PairedTrialSample | None = None


class EvalCampaignReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str = Field(min_length=1)
    status: Literal["running", "completed", "cancelled"]
    contract: EvalCampaignContract
    scope: CampaignScope | None = None
    scope_fingerprint: str | None = None
    planned_trial_count: int = Field(ge=1)
    linked_trial_count: int = Field(ge=0)
    released_trial_count: int = Field(ge=0)
    completed_trial_count: int = Field(ge=0)
    invalid_trial_count: int = Field(ge=0)
    trials: tuple[CampaignTrialSummary, ...]
    evidence_valid: bool
    invalid_reasons: tuple[str, ...] = ()
    aggregate: CampaignAggregate | None = None
    recommendation: TeamRecommendationDecision | None = None


class EvalCampaignService:
    """持久化父实验计划；子 Pair 由独立后台推进阶段创建。"""

    _CREATE_CLAIM_TTL_SECONDS = 15
    _ADVANCE_CLAIM_TTL_SECONDS = 300
    _MAX_PAIR_CREATION_ATTEMPTS = 3

    def __init__(
        self,
        store: SQLiteEventStore,
        *,
        policy: CampaignRecommendationPolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or CampaignRecommendationPolicy()
        self.aggregator = CampaignAggregator()

    def create(self, request: EvalCampaignRequest) -> EvalCampaignCreated:
        campaign_id = self.campaign_id(request.request_id)
        owner_id = f"campaign-creator:{uuid4().hex}"
        claims = self.store.claim_work(
            claim_keys=(f"eval-campaign-create:{campaign_id}",),
            owner_id=owner_id,
            ttl_seconds=self._CREATE_CLAIM_TTL_SECONDS,
        )
        if claims is None:
            raise TimeoutError("eval campaign creation is already in progress")
        claim_closed = False
        try:
            requested = self._request_event(campaign_id, request)
            existing = self._events(campaign_id, "eval.campaign.created")
            if len(existing) > 1:
                raise RuntimeError(f"campaign has multiple contracts: {campaign_id}")
            if existing:
                contract = EvalCampaignContract.model_validate(
                    existing[0].payload["contract"]
                )
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="completed",
                )
                if not claim_closed:
                    raise RuntimeError("campaign creation claim was lost during replay")
                return self._created(contract)

            contract = self._build_contract(campaign_id, request)
            created_event = self._event(
                campaign_id,
                "created",
                "eval.campaign.created",
                {"contract": contract.model_dump(mode="json")},
                causation_id=requested.id,
            )
            claim_closed = self.store.finish_work_claims(
                claims=claims,
                owner_id=owner_id,
                state="completed",
                final_event=created_event,
            )
            if not claim_closed:
                raise RuntimeError("campaign creation claim was lost before commit")
            return self._created(contract)
        finally:
            if not claim_closed:
                self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )

    def contract(self, campaign_id: str) -> EvalCampaignContract:
        created = self._events(campaign_id, "eval.campaign.created")
        if len(created) != 1:
            raise KeyError(f"campaign has no unique contract: {campaign_id}")
        return EvalCampaignContract.model_validate(created[0].payload["contract"])

    def report(self, campaign_id: str) -> EvalCampaignReport:
        """只从持久事实组装视图；GET 调用该方法不会推进或评分。"""

        events = self.store.read_all(run_id=campaign_id)
        terminal = [
            event
            for event in events
            if event.type in {"eval.campaign.completed", "eval.campaign.cancelled"}
        ]
        if len(terminal) > 1:
            raise RuntimeError(f"campaign has multiple terminal reports: {campaign_id}")
        if terminal:
            report = EvalCampaignReport.model_validate(terminal[0].payload["report"])
            expected_status = (
                "completed"
                if terminal[0].type == "eval.campaign.completed"
                else "cancelled"
            )
            if report.status != expected_status:
                raise RuntimeError(
                    f"campaign terminal event disagrees with its report: {campaign_id}"
                )
            return report

        contract = self.contract(campaign_id)
        scope_events = [
            event for event in events if event.type == "eval.campaign.scope.bound"
        ]
        if len(scope_events) > 1:
            raise RuntimeError(f"campaign has multiple scope facts: {campaign_id}")
        scope = (
            CampaignScope.model_validate(scope_events[0].payload["scope"])
            if scope_events
            else None
        )
        linked = {
            int(event.payload["trial_index"]): event
            for event in events
            if event.type == "eval.campaign.trial.linked"
        }
        started = {
            int(event.payload["trial_index"]): event
            for event in events
            if event.type == "eval.campaign.trial.started"
        }
        released = {
            int(event.payload["trial_index"]): event
            for event in events
            if event.type == "eval.campaign.trial.released"
        }
        observed = {
            int(event.payload["trial_index"]): event
            for event in events
            if event.type == "eval.campaign.trial.observed"
        }
        creation_failed = {
            int(event.payload["trial_index"]): event
            for event in events
            if event.type == "eval.campaign.trial.creation_failed"
        }
        summaries: list[CampaignTrialSummary] = []
        invalid_reasons: list[str] = []
        invalid_count = 0
        for trial in contract.trials:
            index = trial.trial_index
            if index in creation_failed:
                reasons = tuple(
                    str(item)
                    for item in creation_failed[index].payload.get(
                        "invalid_reasons", ("pair_creation_failed",)
                    )
                )
                invalid_count += 1
                invalid_reasons.extend(f"trial_{index}:{reason}" for reason in reasons)
                summaries.append(
                    CampaignTrialSummary(
                        trial_index=index,
                        eval_id=trial.eval_id,
                        status="creation_failed",
                        evidence_valid=False,
                        invalid_reasons=reasons,
                    )
                )
                continue
            if index in observed:
                event = observed[index]
                valid = bool(event.payload.get("evidence_valid"))
                reasons = tuple(
                    str(item) for item in event.payload.get("invalid_reasons", ())
                )
                sample_payload = event.payload.get("sample")
                sample = (
                    PairedTrialSample.model_validate(sample_payload)
                    if sample_payload is not None
                    else None
                )
                if not valid:
                    invalid_count += 1
                    invalid_reasons.extend(
                        f"trial_{index}:{reason}" for reason in reasons
                    )
                summaries.append(
                    CampaignTrialSummary(
                        trial_index=index,
                        eval_id=trial.eval_id,
                        status="observed",
                        evidence_valid=valid,
                        invalid_reasons=reasons,
                        sample=sample,
                    )
                )
                continue
            status: Literal["planned", "started", "linked", "released"] = "planned"
            if index in released:
                status = "released"
            elif index in linked:
                status = "linked"
            elif index in started:
                status = "started"
            summaries.append(
                CampaignTrialSummary(
                    trial_index=index,
                    eval_id=trial.eval_id,
                    status=status,
                )
            )
        completed_count = len(observed) + len(creation_failed)
        return EvalCampaignReport(
            campaign_id=campaign_id,
            status="running",
            contract=contract,
            scope=scope,
            scope_fingerprint=(scope.fingerprint if scope is not None else None),
            planned_trial_count=contract.planned_trial_count,
            linked_trial_count=len(linked),
            released_trial_count=len(released),
            completed_trial_count=completed_count,
            invalid_trial_count=invalid_count,
            trials=tuple(summaries),
            evidence_valid=invalid_count == 0,
            invalid_reasons=tuple(invalid_reasons),
        )

    def list_reports(self) -> list[EvalCampaignReport]:
        campaign_ids = {
            event.run_id
            for event in self.store.read_all()
            if event.type == "eval.campaign.created"
        }
        return [self.report(campaign_id) for campaign_id in sorted(campaign_ids)]

    def finalize(self, campaign_id: str) -> EvalCampaignReport:
        current = self.report(campaign_id)
        if current.status != "running":
            return current
        if current.completed_trial_count != current.planned_trial_count:
            return current

        owner_id = f"campaign-finalizer:{uuid4().hex}"
        claims = self.store.claim_work(
            claim_keys=(self._terminal_claim_key(campaign_id),),
            owner_id=owner_id,
            ttl_seconds=self._ADVANCE_CLAIM_TTL_SECONDS,
        )
        if claims is None:
            return self.report(campaign_id)
        claim_closed = False
        try:
            current = self.report(campaign_id)
            if (
                current.status != "running"
                or current.completed_trial_count != current.planned_trial_count
            ):
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )
                return current
            samples = tuple(
                trial.sample for trial in current.trials if trial.sample is not None
            )
            observations = self._events(campaign_id, "eval.campaign.trial.observed")
            scope_fingerprints = {
                str(event.payload.get("scope_fingerprint", ""))
                for event in observations
            }
            scope_valid = current.scope is not None and scope_fingerprints == {
                current.scope.fingerprint
            }
            aggregator_version_valid = (
                current.contract.aggregator_version == self.aggregator.VERSION
            )
            aggregate = None
            if (
                current.invalid_trial_count == 0
                and scope_valid
                and aggregator_version_valid
                and len(samples) == current.planned_trial_count
            ):
                aggregate = self.aggregator.aggregate(
                    samples=samples,
                    policy=current.contract.policy,
                    seed_material=(
                        f"{current.contract.request_fingerprint}:"
                        f"{current.scope.fingerprint}"
                    ),
                )
            evidence = CampaignRecommendationEvidence(
                evidence_tier=current.contract.evidence_tier,
                planned_trial_count=current.planned_trial_count,
                completed_trial_count=current.completed_trial_count,
                invalid_trial_count=current.invalid_trial_count,
                scope_valid=scope_valid,
                aggregate=aggregate,
            )
            recommendation = current.contract.policy.decide(evidence)
            invalid_reasons = list(current.invalid_reasons)
            if not scope_valid:
                invalid_reasons.append("campaign_scope_missing_or_mismatched")
            if not aggregator_version_valid:
                invalid_reasons.append(
                    "aggregator_version_mismatch:"
                    f"{current.contract.aggregator_version}!={self.aggregator.VERSION}"
                )
            completed = current.model_copy(
                update={
                    "status": "completed",
                    "evidence_valid": (
                        current.invalid_trial_count == 0
                        and scope_valid
                        and aggregator_version_valid
                    ),
                    "invalid_reasons": tuple(invalid_reasons),
                    "aggregate": aggregate,
                    "recommendation": recommendation,
                }
            )
            final_event = self._event(
                campaign_id,
                "completed",
                "eval.campaign.completed",
                {"report": completed.model_dump(mode="json")},
            )
            claim_closed = self.store.finish_work_claims(
                claims=claims,
                owner_id=owner_id,
                state="completed",
                final_event=final_event,
            )
            if not claim_closed:
                raise RuntimeError("campaign finalization claim was lost before commit")
            return EvalCampaignReport.model_validate(final_event.payload["report"])
        finally:
            if not claim_closed:
                self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )

    def finalize_ready(self) -> int:
        completed = 0
        for report in self.list_reports():
            if report.status != "running":
                continue
            if report.completed_trial_count != report.planned_trial_count:
                continue
            completed += self.finalize(report.campaign_id).status == "completed"
        return completed

    def advance_ready(
        self,
        *,
        create_pair: Callable[[PairedEvalRequest], PairedEvalCreated],
        pair_contract: Callable[[str], PairedEvalContract],
        release_pair: Callable[[str], object],
        pair_report: Callable[[str], PairedEvalReport],
    ) -> int:
        """每个 Campaign 最多推进一个边界，避免单个实验饿死普通任务。"""

        progressed = 0
        for report in self.list_reports():
            if report.status != "running":
                continue
            progressed += self.advance_one(
                report.campaign_id,
                create_pair=create_pair,
                pair_contract=pair_contract,
                release_pair=release_pair,
                pair_report=pair_report,
            )
        return progressed

    def advance_one(
        self,
        campaign_id: str,
        *,
        create_pair: Callable[[PairedEvalRequest], PairedEvalCreated],
        pair_contract: Callable[[str], PairedEvalContract],
        release_pair: Callable[[str], object],
        pair_report: Callable[[str], PairedEvalReport],
    ) -> bool:
        """推进一个持久边界；Link 与 Release 故意拆成两个可恢复步骤。"""

        contract = self.contract(campaign_id)
        owner_id = f"campaign-advancer:{uuid4().hex}"
        claims = self.store.claim_work(
            claim_keys=(self._advance_claim_key(campaign_id),),
            owner_id=owner_id,
            ttl_seconds=self._ADVANCE_CLAIM_TTL_SECONDS,
        )
        if claims is None:
            return False
        claim_closed = False
        try:
            events = self.store.read_all(run_id=campaign_id)
            if any(
                event.type in {"eval.campaign.completed", "eval.campaign.cancelled"}
                for event in events
            ):
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )
                return False

            linked = {
                int(event.payload["trial_index"]): event
                for event in events
                if event.type == "eval.campaign.trial.linked"
            }
            released = {
                int(event.payload["trial_index"])
                for event in events
                if event.type == "eval.campaign.trial.released"
            }
            observed = {
                int(event.payload["trial_index"])
                for event in events
                if event.type == "eval.campaign.trial.observed"
            }
            failed = {
                int(event.payload["trial_index"])
                for event in events
                if event.type == "eval.campaign.trial.creation_failed"
            }
            retryable_failures = {
                trial.trial_index: sum(
                    event.type == "eval.campaign.trial.creation_retryable_failed"
                    and int(event.payload["trial_index"]) == trial.trial_index
                    for event in events
                )
                for trial in contract.trials
            }
            started = {
                int(event.payload["trial_index"]): event
                for event in events
                if event.type == "eval.campaign.trial.started"
            }

            # Link 已是父级授权事实。若崩溃发生在 Link 后、Release 前，
            # 恢复只重放同一个 Pair 的幂等释放，不会派生新 Trial。
            for trial in contract.trials:
                if trial.trial_index not in linked or trial.trial_index in released:
                    continue
                release_pair(trial.eval_id)
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="completed",
                    final_event=self._event(
                        campaign_id,
                        f"trial-released:{trial.trial_index}",
                        "eval.campaign.trial.released",
                        {
                            "trial_index": trial.trial_index,
                            "eval_id": trial.eval_id,
                        },
                        causation_id=linked[trial.trial_index].id,
                    ),
                )
                if not claim_closed:
                    raise RuntimeError("campaign advance claim was lost after release")
                return True

            for trial in contract.trials:
                if trial.trial_index not in linked or trial.trial_index in observed:
                    continue
                report = pair_report(trial.eval_id)
                if report.status != "completed":
                    continue
                observation = self._observation_event(
                    campaign=contract,
                    trial=trial,
                    report=report,
                )
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="completed",
                    final_event=observation,
                )
                if not claim_closed:
                    raise RuntimeError(
                        "campaign advance claim was lost before Pair observation"
                    )
                return True

            trial = next(
                (
                    item
                    for item in contract.trials
                    if item.trial_index in started
                    and item.trial_index not in linked
                    and item.trial_index not in failed
                ),
                None,
            )
            if trial is not None:
                child_request = self._child_request(contract, trial)
                failure_stage = "create_pair"
                try:
                    created = create_pair(child_request)
                    failure_stage = "validate_pair_identity"
                    if created.eval_id != trial.eval_id:
                        raise ValueError(
                            "campaign child Pair returned an unexpected eval_id"
                        )
                    child_contract = pair_contract(created.eval_id)
                    if child_contract.eval_id != trial.eval_id:
                        raise ValueError(
                            "campaign child contract has an unexpected eval_id"
                        )
                    if created.single_run_id != child_contract.single.run_id or (
                        created.team_run_id != child_contract.team.run_id
                    ):
                        raise ValueError(
                            "campaign child Pair identity differs from its contract"
                        )
                    failure_stage = "validate_pair_commit"
                    pair_commits = [
                        event
                        for event in self.store.read_all(run_id=trial.eval_id)
                        if event.type == "eval.pair.committed"
                    ]
                    expected_commit = {
                        "single_run_id": child_contract.single.run_id,
                        "team_run_id": child_contract.team.run_id,
                    }
                    if (
                        len(pair_commits) != 1
                        or pair_commits[0].payload != expected_commit
                    ):
                        raise RuntimeError("campaign child Pair is not committed")
                    failure_stage = "bind_campaign_scope"
                    scope = CampaignScope.from_pair_contract(child_contract)
                    scope_event = self._bind_or_validate_scope(campaign_id, scope)
                except Exception as exc:
                    retryable = self._is_retryable_pair_creation_error(exc)
                    attempt_number = retryable_failures[trial.trial_index] + 1
                    retry_exhausted = (
                        retryable and attempt_number >= self._MAX_PAIR_CREATION_ATTEMPTS
                    )
                    event_type = "eval.campaign.trial.creation_failed"
                    event_key = f"trial-creation-failed:{trial.trial_index}"
                    invalid_reasons = ["pair_creation_failed"]
                    if retryable and not retry_exhausted:
                        event_type = "eval.campaign.trial.creation_retryable_failed"
                        event_key = (
                            f"trial-creation-retryable-failed:"
                            f"{trial.trial_index}:{attempt_number}"
                        )
                        invalid_reasons = []
                    elif retry_exhausted:
                        invalid_reasons = ["pair_creation_retry_exhausted"]
                    claim_closed = self.store.finish_work_claims(
                        claims=claims,
                        owner_id=owner_id,
                        state="failed",
                        final_event=self._event(
                            campaign_id,
                            event_key,
                            event_type,
                            {
                                "trial_index": trial.trial_index,
                                "pair_request_id": trial.pair_request_id,
                                "eval_id": trial.eval_id,
                                "failure_stage": failure_stage,
                                "error_type": type(exc).__name__,
                                "error_message": str(exc)[:2000],
                                "retryable": retryable,
                                "attempt_number": attempt_number,
                                "max_attempts": self._MAX_PAIR_CREATION_ATTEMPTS,
                                "invalid_reasons": invalid_reasons,
                            },
                            causation_id=started[trial.trial_index].id,
                        ),
                    )
                    if not claim_closed:
                        raise RuntimeError(
                            "campaign advance claim was lost before creation failure"
                        ) from exc
                    return True
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="completed",
                    final_event=self._event(
                        campaign_id,
                        f"trial-linked:{trial.trial_index}",
                        "eval.campaign.trial.linked",
                        {
                            "trial_index": trial.trial_index,
                            "pair_request_id": trial.pair_request_id,
                            "eval_id": trial.eval_id,
                            "single_run_id": created.single_run_id,
                            "team_run_id": created.team_run_id,
                            "scope_fingerprint": scope.fingerprint,
                        },
                        causation_id=scope_event.id,
                    ),
                )
                if not claim_closed:
                    raise RuntimeError(
                        "campaign advance claim was lost before Pair link"
                    )
                return True

            active_indices = (set(started) | set(linked)) - observed - failed
            if len(active_indices) >= contract.max_parallel_pairs:
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )
                return False
            trial = next(
                (
                    item
                    for item in contract.trials
                    if item.trial_index not in started
                    and item.trial_index not in linked
                    and item.trial_index not in failed
                ),
                None,
            )
            if trial is None:
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )
                return False
            claim_closed = self.store.finish_work_claims(
                claims=claims,
                owner_id=owner_id,
                state="completed",
                final_event=self._event(
                    campaign_id,
                    f"trial-started:{trial.trial_index}",
                    "eval.campaign.trial.started",
                    {
                        "trial_index": trial.trial_index,
                        "pair_request_id": trial.pair_request_id,
                        "eval_id": trial.eval_id,
                    },
                ),
            )
            if not claim_closed:
                raise RuntimeError("campaign advance claim was lost before Trial start")
            return True
        finally:
            if not claim_closed:
                self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )

    def cancel(
        self,
        campaign_id: str,
        *,
        reason: str = "operator_requested",
        cancel_pair: CancelPair | None = None,
    ) -> EvalCampaignReport:
        """Fence advancement, cancel every started Pair, then commit one terminal fact."""

        current = self.report(campaign_id)
        if current.status != "running":
            return current

        owner_id = f"campaign-canceller:{uuid4().hex}"
        claims = self.store.claim_work(
            claim_keys=(
                self._advance_claim_key(campaign_id),
                self._terminal_claim_key(campaign_id),
            ),
            owner_id=owner_id,
            ttl_seconds=self._ADVANCE_CLAIM_TTL_SECONDS,
        )
        if claims is None:
            raise TimeoutError("eval campaign cancellation is already in progress")
        claim_closed = False
        try:
            current = self.report(campaign_id)
            if current.status != "running":
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )
                return current

            events = self.store.read_all(run_id=campaign_id)
            started_indices = {
                int(event.payload["trial_index"])
                for event in events
                if event.type == "eval.campaign.trial.started"
            }
            started_trials = tuple(
                trial
                for trial in current.contract.trials
                if trial.trial_index in started_indices
            )
            if started_trials and cancel_pair is None:
                raise RuntimeError(
                    "campaign has started child Pairs but no cancellation callback"
                )
            for trial in started_trials:
                assert cancel_pair is not None
                cancel_pair(trial.eval_id)

            invalid_reasons = tuple(
                dict.fromkeys((*current.invalid_reasons, "campaign_cancelled"))
            )
            cancelled = current.model_copy(
                update={
                    "status": "cancelled",
                    "evidence_valid": False,
                    "invalid_reasons": invalid_reasons,
                    "aggregate": None,
                    "recommendation": None,
                }
            )
            final_event = self._event(
                campaign_id,
                "cancelled",
                "eval.campaign.cancelled",
                {
                    "reason": reason,
                    "report": cancelled.model_dump(mode="json"),
                },
            )
            claim_closed = self.store.finish_work_claims(
                claims=claims,
                owner_id=owner_id,
                state="completed",
                final_event=final_event,
            )
            if not claim_closed:
                raise RuntimeError("campaign cancellation claim was lost before commit")
            return EvalCampaignReport.model_validate(final_event.payload["report"])
        finally:
            if not claim_closed:
                self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )

    def _build_contract(
        self, campaign_id: str, request: EvalCampaignRequest
    ) -> EvalCampaignContract:
        trials = tuple(
            CampaignTrialPlan(
                trial_index=index,
                pair_request_id=self.child_request_id(campaign_id, index),
                eval_id=paired_eval_id(self.child_request_id(campaign_id, index)),
            )
            for index in range(1, request.trial_count + 1)
        )
        return EvalCampaignContract(
            campaign_id=campaign_id,
            request_fingerprint=self._request_fingerprint(request),
            title=request.title,
            brief=request.brief,
            task_pack=request.task_pack,
            model_mode=request.model_mode,
            evidence_tier=(
                EvidenceTier.DETERMINISTIC
                if request.model_mode == "scripted"
                else EvidenceTier.LIVE_PAIRED
            ),
            planned_trial_count=request.trial_count,
            trials=trials,
            pair_model_budget=request.model_budget.model_dump(mode="json"),
            budget=request.budget_envelope(),
            max_parallel_pairs=request.max_parallel_pairs,
            policy=self.policy,
        )

    @staticmethod
    def _child_request(
        contract: EvalCampaignContract,
        trial: CampaignTrialPlan,
    ) -> PairedEvalRequest:
        return PairedEvalRequest(
            request_id=trial.pair_request_id,
            title=contract.title,
            brief=contract.brief,
            model_mode=contract.model_mode,
            task_pack=contract.task_pack,
            model_budget=ModelBudgetConfig.model_validate(contract.pair_model_budget),
            release_policy="campaign_linked",
            parent_campaign_id=contract.campaign_id,
            parent_trial_index=trial.trial_index,
        )

    def _bind_or_validate_scope(
        self,
        campaign_id: str,
        scope: CampaignScope,
    ) -> Event:
        payload = {
            "scope": scope.model_dump(mode="json"),
            "scope_fingerprint": scope.fingerprint,
        }
        existing = self._events(campaign_id, "eval.campaign.scope.bound")
        if len(existing) > 1:
            raise RuntimeError(f"campaign has multiple scope facts: {campaign_id}")
        if existing:
            if existing[0].payload != payload:
                raise ValueError("campaign child Pair does not match the bound scope")
            return existing[0]
        return self.store.append(
            self._event(
                campaign_id,
                "scope-bound",
                "eval.campaign.scope.bound",
                payload,
            )
        )

    def _observation_event(
        self,
        *,
        campaign: EvalCampaignContract,
        trial: CampaignTrialPlan,
        report: PairedEvalReport,
    ) -> Event:
        if report.eval_id != trial.eval_id or report.status != "completed":
            raise ValueError("campaign observation requires its completed child Pair")
        pair_events = [
            event
            for event in self.store.read_all(run_id=trial.eval_id)
            if event.type == "eval.pair.completed"
        ]
        if len(pair_events) != 1:
            raise RuntimeError("campaign child Pair has no unique completion fact")
        persisted_report = PairedEvalReport.model_validate(
            pair_events[0].payload["report"]
        )
        if persisted_report != report:
            raise ValueError("campaign child Pair report differs from persisted fact")

        encoded = json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        report_hash = sha256(encoded).hexdigest()
        scope = CampaignScope.from_pair_contract(report.contract)
        scope_events = self._events(campaign.campaign_id, "eval.campaign.scope.bound")
        expected_scope = (
            CampaignScope.model_validate(scope_events[0].payload["scope"])
            if len(scope_events) == 1
            else None
        )
        invalid_reasons = list(report.invalid_reasons)
        if not report.evidence_valid:
            invalid_reasons.append("paired_evidence_invalid")
        if expected_scope is None or scope.fingerprint != expected_scope.fingerprint:
            invalid_reasons.append("scope_mismatch")
        if (
            report.single.score is None
            or report.team.score is None
            or report.single.trace is None
            or report.team.trace is None
        ):
            invalid_reasons.append("missing_score_or_trace")

        sample = None
        if not invalid_reasons:
            single_score = report.single.score
            team_score = report.team.score
            single_trace = report.single.trace
            team_trace = report.team.trace
            assert single_score is not None and team_score is not None
            assert single_trace is not None and team_trace is not None
            single_success = report.single.status == "succeeded" and single_score.passed
            team_success = report.team.status == "succeeded" and team_score.passed
            sample = PairedTrialSample(
                trial_index=trial.trial_index,
                eval_id=trial.eval_id,
                pair_report_sha256=report_hash,
                single_run_id=report.single.run_id,
                team_run_id=report.team.run_id,
                single_terminal_event_id=single_trace.terminal_event_id,
                team_terminal_event_id=team_trace.terminal_event_id,
                single_success=single_success,
                team_success=team_success,
                single_quality_ppm=self._quality_ppm(single_score.score),
                team_quality_ppm=self._quality_ppm(team_score.score),
                single_committed_cost_microusd=(single_trace.committed_cost_microusd),
                team_committed_cost_microusd=team_trace.committed_cost_microusd,
                single_duration_ms=single_trace.duration_ms,
                team_duration_ms=team_trace.duration_ms,
                hard_reliability_regression=(
                    (single_success and not team_success)
                    or team_trace.operation_unknowns > single_trace.operation_unknowns
                    or team_trace.model_unknown_calls > single_trace.model_unknown_calls
                    or team_trace.dead_letters > single_trace.dead_letters
                ),
            )
        return self._event(
            campaign.campaign_id,
            f"trial-observed:{trial.trial_index}",
            "eval.campaign.trial.observed",
            {
                "trial_index": trial.trial_index,
                "eval_id": trial.eval_id,
                "pair_completed_event_id": pair_events[0].id,
                "pair_report_sha256": report_hash,
                "scope_fingerprint": scope.fingerprint,
                "evidence_valid": not invalid_reasons,
                "invalid_reasons": invalid_reasons,
                "sample": sample.model_dump(mode="json") if sample else None,
            },
            causation_id=pair_events[0].id,
        )

    @staticmethod
    def _quality_ppm(value: float) -> int:
        scaled = Decimal(str(value)) * Decimal(1_000_000)
        return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _is_retryable_pair_creation_error(exc: Exception) -> bool:
        """只重试明确的暂时冲突；契约、身份和 Scope 错误继续 fail closed。"""

        if isinstance(
            exc,
            (TimeoutError, ConnectionError, BlockingIOError, InterruptedError),
        ):
            return True
        if isinstance(exc, sqlite3.OperationalError):
            message = str(exc).casefold()
            return any(
                marker in message
                for marker in (
                    "database is locked",
                    "database table is locked",
                    "database is busy",
                    "temporarily unavailable",
                )
            )
        if isinstance(exc, OSError):
            return exc.errno in {
                errno.EAGAIN,
                errno.EBUSY,
                errno.EINTR,
                errno.ETIMEDOUT,
                errno.ECONNABORTED,
                errno.ECONNREFUSED,
                errno.ECONNRESET,
            }
        return False

    def _request_event(self, campaign_id: str, request: EvalCampaignRequest) -> Event:
        payload = {"request": request.model_dump(mode="json")}
        existing = self._events(campaign_id, "eval.campaign.requested")
        if len(existing) > 1:
            raise RuntimeError(f"campaign has multiple request events: {campaign_id}")
        if existing:
            if existing[0].payload != payload:
                raise EvalCampaignIdempotencyConflict(
                    "campaign idempotency key was reused with different input"
                )
            return existing[0]
        return self.store.append(
            self._event(
                campaign_id,
                "requested",
                "eval.campaign.requested",
                payload,
            )
        )

    def _events(self, campaign_id: str, event_type: str) -> list[Event]:
        return [
            event
            for event in self.store.read_all(run_id=campaign_id)
            if event.type == event_type
        ]

    @staticmethod
    def _created(contract: EvalCampaignContract) -> EvalCampaignCreated:
        return EvalCampaignCreated(
            campaign_id=contract.campaign_id,
            planned_trial_count=contract.planned_trial_count,
            budget=contract.budget,
        )

    @staticmethod
    def campaign_id(request_id: str) -> str:
        value = uuid5(NAMESPACE_URL, f"crazy:campaign-request:{request_id}")
        return f"campaign_{value.hex[:12]}"

    @staticmethod
    def child_request_id(campaign_id: str, trial_index: int) -> str:
        value = uuid5(
            NAMESPACE_URL,
            f"crazy:campaign:{campaign_id}:trial:{trial_index}",
        )
        return f"campaign-trial:{value.hex}"

    @staticmethod
    def _advance_claim_key(campaign_id: str) -> str:
        return f"eval-campaign-advance:{campaign_id}"

    @staticmethod
    def _terminal_claim_key(campaign_id: str) -> str:
        return f"eval-campaign-terminal:{campaign_id}"

    @staticmethod
    def _request_fingerprint(request: EvalCampaignRequest) -> str:
        encoded = json.dumps(
            request.model_dump(mode="json", exclude={"request_id"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _event(
        campaign_id: str,
        key: str,
        event_type: str,
        payload: dict[str, object],
        *,
        causation_id: str | None = None,
    ) -> Event:
        return Event(
            id=str(uuid5(NAMESPACE_URL, f"crazy:campaign:{campaign_id}:{key}")),
            run_id=campaign_id,
            task_id=campaign_id,
            type=event_type,
            source="runtime.eval.campaign",
            payload=payload,
            causation_id=causation_id,
        )
