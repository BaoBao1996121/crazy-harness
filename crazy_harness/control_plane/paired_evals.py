from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from crazy_harness.control_plane.model_governance import ModelBudgetConfig
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.evals import (
    EvidenceTier,
    PairedEvalArm,
    PairedEvalContract,
    RecommendationOutcome,
    RunTraceAggregator,
    RunTraceMetrics,
    TeamRecommendationDecision,
    TeamRecommendationEvidence,
    TeamRecommendationPolicy,
)
from crazy_harness.core.events import Event
from crazy_harness.taskpacks import (
    PreparedRepoWorkspace,
    RepoMaintainerScore,
    RepoMaintainerScorer,
)


class PairedEvalCreationRejected(ValueError):
    """The persisted request cannot be resumed and needs a fresh request ID."""

    code = "paired_eval_creation_rejected"


class PairedEvalRequest(BaseModel):
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
    model_budget: ModelBudgetConfig = Field(default_factory=ModelBudgetConfig)


class EvalRunIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    task_id: str


class PairedEvalCreated(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_id: str
    single_run_id: str
    team_run_id: str
    status: Literal["queued"] = "queued"


class PairedEvalArmReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_mode: Literal["single", "team"]
    run_id: str
    status: str
    score: RepoMaintainerScore | None = None
    trace: RunTraceMetrics | None = None


class PairedEvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_id: str
    status: Literal["running", "completed"]
    contract: PairedEvalContract
    single: PairedEvalArmReport
    team: PairedEvalArmReport
    evidence_valid: bool = True
    invalid_reasons: tuple[str, ...] = ()
    recommendation: TeamRecommendationDecision | None = None

    @model_validator(mode="after")
    def invalid_evidence_requires_a_reason(self) -> PairedEvalReport:
        if self.evidence_valid and self.invalid_reasons:
            raise ValueError("valid paired evidence cannot have invalid reasons")
        if not self.evidence_valid and not self.invalid_reasons:
            raise ValueError("invalid paired evidence requires at least one reason")
        return self


PrepareArm = Callable[
    [Literal["single", "team"], EvalRunIdentity],
    EvalRunIdentity,
]
ReleaseArm = Callable[[Literal["single", "team"], EvalRunIdentity], object]
CancelArm = Callable[[str], object]
FaultInjector = Callable[[str], None]
ResumeEval = Callable[[PairedEvalRequest], object]


def paired_input_hash(request: PairedEvalRequest, fixture_hash: str) -> str:
    payload = {
        "task_pack": request.task_pack,
        "title": request.title,
        "brief": request.brief,
        "fixture_hash": fixture_hash,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class PairedEvalService:
    """Persist, score, and replay one fair Single-vs-Team experiment."""

    _TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
    _CREATE_CLAIM_TTL_SECONDS = 15
    _CREATE_CLAIM_WAIT_SECONDS = 16.0
    _CREATE_CLAIM_POLL_SECONDS = 0.02

    def __init__(
        self,
        store: SQLiteEventStore,
        *,
        scorer: RepoMaintainerScorer | None = None,
        recommendation_policy: TeamRecommendationPolicy | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.store = store
        self.scorer = scorer or RepoMaintainerScorer()
        self.recommendation_policy = recommendation_policy or TeamRecommendationPolicy()
        self.trace_aggregator = RunTraceAggregator()
        self.fault_injector = fault_injector or (lambda _point: None)

    def create(
        self,
        request: PairedEvalRequest,
        *,
        prepare_arm: PrepareArm,
        release_arm: ReleaseArm,
        cancel_arm: CancelArm | None = None,
        fail_precommit: bool = True,
    ) -> PairedEvalCreated:
        eval_id = self._eval_id(request.request_id)
        claim_owner, claims = self._acquire_create_claim(eval_id)
        claim_closed = False
        identities: dict[str, EvalRunIdentity] = {}
        committed = False
        requested: Event | None = None
        try:
            requested = self._request_event(eval_id, request)
            if self._events(eval_id, "eval.pair.failed"):
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=claim_owner,
                    state="failed",
                )
                if not claim_closed:
                    raise RuntimeError(
                        "paired eval creation claim was lost before failure replay"
                    )
                raise PairedEvalCreationRejected(
                    "paired eval idempotency key belongs to a failed creation"
                )
            persisted = self._events(eval_id, "eval.pair.created")
            if len(persisted) > 1:
                raise RuntimeError(f"paired eval has multiple contracts: {eval_id}")
            if persisted:
                contract = PairedEvalContract.model_validate(
                    persisted[0].payload["contract"]
                )
                self._commit_contract(
                    contract,
                    claim_owner=claim_owner,
                    claims=claims,
                )
                claim_closed = True
                committed = True
                self._release_contract(contract, release_arm=release_arm)
                return self._created(contract)

            for mode in ("single", "team"):
                identity = self._arm_identity(eval_id, mode)
                identities[mode] = identity
                self._append(
                    eval_id,
                    f"arm-planned:{mode}",
                    "eval.arm.planned",
                    {
                        "execution_mode": mode,
                        **identity.model_dump(mode="json"),
                    },
                    causation_id=requested.id,
                )
                prepared = self._arm_events(eval_id, "eval.arm.created", mode)
                if not prepared:
                    actual = prepare_arm(mode, identity)
                    if actual != identity:
                        raise ValueError(
                            f"paired {mode} prepare returned a different identity"
                        )
                    self._record_arm(requested, identity, mode)
                    self.fault_injector(f"after_eval_arm_prepared:{mode}")

            single = identities["single"]
            team = identities["team"]
            contract = self._build_contract(
                eval_id=eval_id,
                request=request,
                single=single,
                team=team,
            )
            self._append(
                eval_id,
                "created",
                "eval.pair.created",
                {"contract": contract.model_dump(mode="json")},
                causation_id=requested.id,
            )
            self._commit_contract(
                contract,
                claim_owner=claim_owner,
                claims=claims,
            )
            claim_closed = True
            committed = True
            self.fault_injector("after_eval_pair_committed")
            self._release_contract(contract, release_arm=release_arm)
            return self._created(contract)
        except PairedEvalCreationRejected:
            raise
        except Exception as exc:
            if requested is None:
                raise
            if not committed and fail_precommit:
                failure = self._event(
                    eval_id,
                    "failed",
                    "eval.pair.failed",
                    {"error_type": type(exc).__name__, "reason": str(exc)},
                    causation_id=requested.id,
                )
                claim_closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=claim_owner,
                    state="failed",
                    final_event=failure,
                )
                if not claim_closed:
                    raise RuntimeError(
                        "paired eval creation claim was lost before failure commit"
                    ) from exc
                if cancel_arm is not None:
                    for identity in identities.values():
                        try:
                            cancel_arm(identity.run_id)
                        except Exception as cleanup_error:
                            self._append(
                                eval_id,
                                f"cleanup-failed:{identity.run_id}",
                                "eval.arm.cleanup.failed",
                                {
                                    "run_id": identity.run_id,
                                    "error_type": type(cleanup_error).__name__,
                                    "reason": str(cleanup_error),
                                },
                                causation_id=requested.id,
                            )
                raise PairedEvalCreationRejected(str(exc)) from exc
            raise
        finally:
            if not claim_closed:
                self.store.finish_work_claims(
                    claims=claims,
                    owner_id=claim_owner,
                    state="released",
                )

    def recover_pending(
        self,
        *,
        resume: ResumeEval,
    ) -> int:
        requests = {
            event.run_id: PairedEvalRequest.model_validate(event.payload["request"])
            for event in self.store.read_all()
            if event.type == "eval.pair.requested"
        }
        recovered = 0
        for eval_id, request in requests.items():
            if self._events(eval_id, "eval.pair.failed"):
                continue
            created = self._events(eval_id, "eval.pair.created")
            if created:
                PairedEvalContract.model_validate(
                    created[0].payload["contract"]
                )
                released = {
                    event.payload.get("execution_mode")
                    for event in self._events(eval_id, "eval.arm.released")
                }
                if released == {"single", "team"}:
                    continue
            try:
                resume(request)
            except Exception as exc:
                attempt = len(
                    self._events(eval_id, "eval.pair.recovery.failed")
                ) + 1
                self._append(
                    eval_id,
                    f"recovery-failed:{attempt}",
                    "eval.pair.recovery.failed",
                    {
                        "attempt": attempt,
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                    },
                )
                continue
            recovered += 1
        return recovered

    def contract(self, eval_id: str) -> PairedEvalContract:
        created = [
            event
            for event in self.store.read_all(run_id=eval_id)
            if event.type == "eval.pair.created"
        ]
        if len(created) != 1:
            raise KeyError(f"paired eval has no unique contract: {eval_id}")
        return PairedEvalContract.model_validate(created[0].payload["contract"])

    def report(self, eval_id: str) -> PairedEvalReport:
        persisted = self._events(eval_id, "eval.pair.completed")
        if len(persisted) > 1:
            raise RuntimeError(f"paired eval has multiple completed reports: {eval_id}")
        if persisted:
            return PairedEvalReport.model_validate(persisted[0].payload["report"])

        contract = self.contract(eval_id)
        single_status = self._arm_status(contract.single)
        team_status = self._arm_status(contract.team)
        return PairedEvalReport(
            eval_id=eval_id,
            status="running",
            contract=contract,
            single=PairedEvalArmReport(
                execution_mode="single",
                run_id=contract.single.run_id,
                status=single_status,
            ),
            team=PairedEvalArmReport(
                execution_mode="team",
                run_id=contract.team.run_id,
                status=team_status,
            ),
        )

    def finalize(self, eval_id: str) -> PairedEvalReport:
        current = self.report(eval_id)
        if current.status == "completed":
            return current
        contract = current.contract
        if self.scorer.scorer_version != contract.scorer_version:
            raise RuntimeError(
                "paired eval scorer version does not match its persisted contract"
            )
        if (
            current.single.status not in self._TERMINAL
            or current.team.status not in self._TERMINAL
        ):
            return current

        owner_id = f"eval-scorer:{uuid4().hex}"
        claim_key = f"eval-score:{eval_id}"
        claims = self.store.claim_work(
            claim_keys=(claim_key,),
            owner_id=owner_id,
            ttl_seconds=300,
        )
        if claims is None:
            return self.report(eval_id)
        try:
            single = self._completed_arm_report(
                contract,
                contract.single,
                current.single.status,
            )
            team = self._completed_arm_report(
                contract,
                contract.team,
                current.team.status,
            )
            invalid_reasons = self._live_model_attestation_errors(contract)
            completed = PairedEvalReport(
                eval_id=eval_id,
                status="completed",
                contract=contract,
                single=single,
                team=team,
                evidence_valid=not invalid_reasons,
                invalid_reasons=invalid_reasons,
                recommendation=(
                    TeamRecommendationDecision(
                        outcome=RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE,
                        reason="paired evidence is invalid and cannot change routing",
                        failed_thresholds=("invalid_evidence",),
                    )
                    if invalid_reasons
                    else self._recommend(contract, single, team)
                ),
            )
            final_event = self._event(
                eval_id,
                "completed",
                "eval.pair.completed",
                {"report": completed.model_dump(mode="json")},
            )
            committed = self.store.finish_work_claims(
                claims=claims,
                owner_id=owner_id,
                state="completed",
                final_event=final_event,
            )
            if not committed:
                raise RuntimeError("paired eval scoring claim was lost before commit")
            return PairedEvalReport.model_validate(final_event.payload["report"])
        except Exception:
            self.store.finish_work_claims(
                claims=claims,
                owner_id=owner_id,
                state="failed",
            )
            raise

    def list_reports(self) -> list[PairedEvalReport]:
        eval_ids = {
            event.run_id
            for event in self.store.read_all()
            if event.type == "eval.pair.created"
        }
        return [self.report(eval_id) for eval_id in sorted(eval_ids)]

    def finalize_ready(self) -> int:
        """Finalize terminal pairs without making GET/list endpoints mutate state."""

        completed = 0
        eval_ids = {
            event.run_id
            for event in self.store.read_all()
            if event.type == "eval.pair.created"
        }
        for eval_id in sorted(eval_ids):
            prior_failure = any(
                event.payload.get("active_scorer_version")
                == self.scorer.scorer_version
                for event in self._events(
                    eval_id,
                    "eval.pair.finalization.failed",
                )
            )
            if prior_failure:
                continue
            try:
                finalized = self.finalize(eval_id)
            except Exception as exc:
                self._append(
                    eval_id,
                    f"finalization-failed:{self.scorer.scorer_version}",
                    "eval.pair.finalization.failed",
                    {
                        "active_scorer_version": self.scorer.scorer_version,
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                        "automatic_retry": False,
                    },
                )
                continue
            completed += finalized.status == "completed"
        return completed

    def _build_contract(
        self,
        *,
        eval_id: str,
        request: PairedEvalRequest,
        single: EvalRunIdentity,
        team: EvalRunIdentity,
    ) -> PairedEvalContract:
        single_event = self._run_created(single.run_id)
        team_event = self._run_created(team.run_id)
        shared_fields = (
            "case_id",
            "fixture_hash",
            "input_hash",
            "scorer_version",
            "title",
            "brief",
        )
        for name in shared_fields:
            if single_event.payload.get(name) != team_event.payload.get(name):
                raise ValueError(f"paired run metadata differs for {name}")
        budget = request.model_budget.model_dump(mode="json")
        for event, mode in ((single_event, "single"), (team_event, "team")):
            if event.payload.get("execution_mode") != mode:
                raise ValueError(f"paired {mode} run has the wrong execution_mode")
            if event.payload.get("task_pack") != request.task_pack:
                raise ValueError(f"paired {mode} run has the wrong task_pack")
            if event.payload.get("model_mode") != request.model_mode:
                raise ValueError(f"paired {mode} run has the wrong model_mode")
            if event.payload.get("model_budget") != budget:
                raise ValueError(f"paired {mode} run has a different model_budget")
        profile = dict(single_event.payload.get("model_profile") or {})
        if not profile or profile != team_event.payload.get("model_profile"):
            raise ValueError("paired runs have different persisted model_profile values")
        if single_event.payload.get("scorer_version") != self.scorer.scorer_version:
            raise ValueError("paired runs require the active scorer version")
        fixture_hash = str(single_event.payload["fixture_hash"])
        expected_input_hash = paired_input_hash(request, fixture_hash)
        if single_event.payload.get("input_hash") != expected_input_hash:
            raise ValueError("paired runs do not match the requested task input")
        return PairedEvalContract(
            eval_id=eval_id,
            case_id=str(single_event.payload["case_id"]),
            task_pack=request.task_pack,
            fixture_hash=fixture_hash,
            scorer_version=str(single_event.payload["scorer_version"]),
            evidence_tier=(
                EvidenceTier.DETERMINISTIC
                if request.model_mode == "scripted"
                else EvidenceTier.LIVE_PAIRED
            ),
            single=PairedEvalArm(
                execution_mode="single",
                run_id=single.run_id,
                workspace=Path(str(single_event.payload["workspace_path"])),
                input_hash=str(single_event.payload["input_hash"]),
                model_profile=profile,
                model_budget=budget,
            ),
            team=PairedEvalArm(
                execution_mode="team",
                run_id=team.run_id,
                workspace=Path(str(team_event.payload["workspace_path"])),
                input_hash=str(team_event.payload["input_hash"]),
                model_profile=profile,
                model_budget=budget,
            ),
        )

    def _arm_status(self, arm: PairedEvalArm) -> str:
        run = self.store.projection("run", arm.run_id)
        if run is None:
            raise RuntimeError(f"paired eval references a missing run: {arm.run_id}")
        return str(run.get("status", "unknown"))

    def _completed_arm_report(
        self,
        contract: PairedEvalContract,
        arm: PairedEvalArm,
        status: str,
    ) -> PairedEvalArmReport:
        if status not in self._TERMINAL:
            raise ValueError("completed arm report requires a trusted terminal status")
        created = self._run_created(arm.run_id)
        prepared = PreparedRepoWorkspace(
            workspace=arm.workspace,
            baseline=Path(str(created.payload["baseline_path"])),
        )
        return PairedEvalArmReport(
            execution_mode=arm.execution_mode,
            run_id=arm.run_id,
            status=status,
            score=self.scorer.score(
                prepared,
                expected_input_hash=contract.fixture_hash,
            ),
            trace=self.trace_aggregator.aggregate(
                events=self.store.read_all(run_id=arm.run_id),
                model_budget_status=self.store.model_budget_status(arm.run_id),
            ),
        )

    def _recommend(
        self,
        contract: PairedEvalContract,
        single: PairedEvalArmReport,
        team: PairedEvalArmReport,
    ) -> TeamRecommendationDecision:
        if single.score is None or team.score is None:
            raise RuntimeError("terminal paired eval is missing a machine score")
        if single.trace is None or team.trace is None:
            raise RuntimeError("terminal paired eval is missing trace metrics")
        evidence = TeamRecommendationEvidence(
            evidence_tier=contract.evidence_tier,
            paired_live_trials=(
                1 if contract.evidence_tier is EvidenceTier.LIVE_PAIRED else 0
            ),
            success_rate_delta=float(team.score.passed) - float(single.score.passed),
            quality_delta=team.score.score - single.score.score,
            cost_ratio=self._ratio(
                team.trace.committed_cost_microusd,
                single.trace.committed_cost_microusd,
            ),
            duration_ratio=self._ratio(
                team.trace.duration_ms,
                single.trace.duration_ms,
            ),
            hard_reliability_regression=(
                (single.status == "succeeded" and team.status != "succeeded")
                or team.trace.operation_unknowns > single.trace.operation_unknowns
                or team.trace.model_unknown_calls > single.trace.model_unknown_calls
                or team.trace.dead_letters > single.trace.dead_letters
            ),
        )
        return self.recommendation_policy.decide(evidence)

    def _live_model_attestation_errors(
        self,
        contract: PairedEvalContract,
    ) -> tuple[str, ...]:
        if contract.evidence_tier is not EvidenceTier.LIVE_PAIRED:
            return ()
        expected_profile = dict(contract.single.model_profile)
        expected_model = str(expected_profile.get("model", ""))
        expected_provider = str(expected_profile.get("provider", ""))
        if not expected_model or not expected_provider:
            return ("live pair has no persisted model attestation target",)
        errors: list[str] = []
        for arm in (contract.single, contract.team):
            calls = self.store.list_model_calls(run_id=arm.run_id)
            if not calls:
                errors.append(
                    f"{arm.execution_mode} arm has no persisted model call attestation"
                )
                continue
            call_ids = {str(call.get("call_id", "")) for call in calls}
            models = {str(call.get("model", "")) for call in calls}
            providers = {str(call.get("provider", "")) for call in calls}
            reservations = [
                event
                for event in self.store.read_all(run_id=arm.run_id)
                if event.type == "model.call.reserved"
            ]
            reservation_ids = {
                str(event.payload.get("call_id", "")) for event in reservations
            }
            profiles = [
                dict(event.payload.get("provider_profile") or {})
                for event in reservations
            ]
            if (
                models != {expected_model}
                or providers != {expected_provider}
                or len(reservations) != len(calls)
                or reservation_ids != call_ids
                or any(profile != expected_profile for profile in profiles)
            ):
                errors.append(
                    f"{arm.execution_mode} arm model call attestation profile does not match contract"
                )
        return tuple(errors)

    def _validate_live_model_attestation(self, contract: PairedEvalContract) -> None:
        errors = self._live_model_attestation_errors(contract)
        if errors:
            raise ValueError("; ".join(errors))

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        if denominator == 0:
            return 1.0 if numerator == 0 else 1_000_000.0
        return numerator / denominator

    @staticmethod
    def _eval_id(request_id: str) -> str:
        value = uuid5(NAMESPACE_URL, f"crazy:eval-request:{request_id}")
        return f"eval_{value.hex[:12]}"

    @staticmethod
    def _arm_identity(
        eval_id: str,
        mode: Literal["single", "team"],
    ) -> EvalRunIdentity:
        run = uuid5(NAMESPACE_URL, f"crazy:eval:{eval_id}:{mode}:run")
        task = uuid5(NAMESPACE_URL, f"crazy:eval:{eval_id}:{mode}:task")
        return EvalRunIdentity(
            run_id=f"run_{run.hex[:12]}",
            task_id=f"task_{task.hex[:12]}",
        )

    def _request_event(
        self,
        eval_id: str,
        request: PairedEvalRequest,
    ) -> Event:
        payload = {"request": request.model_dump(mode="json")}
        existing = self._events(eval_id, "eval.pair.requested")
        if len(existing) > 1:
            raise RuntimeError(f"paired eval has multiple request events: {eval_id}")
        if existing:
            if existing[0].payload != payload:
                raise ValueError(
                    "paired eval idempotency key was reused with different input"
                )
            return existing[0]
        return self._append(
            eval_id,
            "requested",
            "eval.pair.requested",
            payload,
        )

    def _commit_contract(
        self,
        contract: PairedEvalContract,
        *,
        claim_owner: str,
        claims: dict[str, int],
    ) -> None:
        self._link_arm(
            contract.single,
            contract.eval_id,
            contract.team.run_id,
        )
        self._link_arm(
            contract.team,
            contract.eval_id,
            contract.single.run_id,
        )
        committed = self.store.finish_work_claims(
            claims=claims,
            owner_id=claim_owner,
            state="completed",
            final_event=self._event(
                contract.eval_id,
                "committed",
                "eval.pair.committed",
                {
                    "single_run_id": contract.single.run_id,
                    "team_run_id": contract.team.run_id,
                },
            ),
        )
        if not committed:
            raise RuntimeError("paired eval creation claim was lost before commit")

    def _acquire_create_claim(self, eval_id: str) -> tuple[str, dict[str, int]]:
        owner_id = f"eval-creator:{uuid4().hex}"
        claim_key = f"eval-create:{eval_id}"
        deadline = monotonic() + self._CREATE_CLAIM_WAIT_SECONDS
        while True:
            claims = self.store.claim_work(
                claim_keys=(claim_key,),
                owner_id=owner_id,
                ttl_seconds=self._CREATE_CLAIM_TTL_SECONDS,
            )
            if claims is not None:
                return owner_id, claims
            if monotonic() >= deadline:
                raise TimeoutError("paired eval creation is already in progress")
            sleep(self._CREATE_CLAIM_POLL_SECONDS)

    def _release_contract(
        self,
        contract: PairedEvalContract,
        *,
        release_arm: ReleaseArm,
    ) -> None:
        for arm in (contract.single, contract.team):
            if self._arm_events(
                contract.eval_id,
                "eval.arm.released",
                arm.execution_mode,
            ):
                continue
            identity = EvalRunIdentity(
                run_id=arm.run_id,
                task_id=self._run_created(arm.run_id).task_id,
            )
            release_arm(arm.execution_mode, identity)
            self._append(
                contract.eval_id,
                f"arm-released:{arm.execution_mode}",
                "eval.arm.released",
                {
                    "execution_mode": arm.execution_mode,
                    **identity.model_dump(mode="json"),
                },
            )

    @staticmethod
    def _created(contract: PairedEvalContract) -> PairedEvalCreated:
        return PairedEvalCreated(
            eval_id=contract.eval_id,
            single_run_id=contract.single.run_id,
            team_run_id=contract.team.run_id,
        )

    def _events(self, eval_id: str, event_type: str) -> list[Event]:
        return [
            event
            for event in self.store.read_all(run_id=eval_id)
            if event.type == event_type
        ]

    def _arm_events(
        self,
        eval_id: str,
        event_type: str,
        mode: str,
    ) -> list[Event]:
        return [
            event
            for event in self._events(eval_id, event_type)
            if event.payload.get("execution_mode") == mode
        ]

    def _record_arm(
        self,
        requested: Event,
        identity: EvalRunIdentity,
        execution_mode: Literal["single", "team"],
    ) -> Event:
        return self._append(
            requested.run_id,
            f"arm:{execution_mode}",
            "eval.arm.created",
            {
                "execution_mode": execution_mode,
                "run_id": identity.run_id,
                "task_id": identity.task_id,
            },
            causation_id=requested.id,
        )

    def _link_arm(
        self, arm: PairedEvalArm, eval_id: str, peer_run_id: str
    ) -> Event:
        created = self._run_created(arm.run_id)
        return self.store.append(
            Event(
                id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"crazy:eval:{eval_id}:link:{arm.execution_mode}",
                    )
                ),
                run_id=arm.run_id,
                task_id=created.task_id,
                type="eval.arm.linked",
                source="runtime.eval",
                payload={
                    "eval_id": eval_id,
                    "execution_mode": arm.execution_mode,
                    "peer_run_id": peer_run_id,
                },
                causation_id=created.id,
            )
        )

    def _run_created(self, run_id: str) -> Event:
        created = [
            event
            for event in self.store.read_all(run_id=run_id)
            if event.type == "run.created"
        ]
        if len(created) != 1:
            raise RuntimeError(f"run has no unique run.created event: {run_id}")
        return created[0]

    def _append(
        self,
        eval_id: str,
        key: str,
        event_type: str,
        payload: dict[str, object],
        *,
        causation_id: str | None = None,
    ) -> Event:
        return self.store.append(
            self._event(
                eval_id,
                key,
                event_type,
                payload,
                causation_id=causation_id,
            )
        )

    @staticmethod
    def _event(
        eval_id: str,
        key: str,
        event_type: str,
        payload: dict[str, object],
        *,
        causation_id: str | None = None,
    ) -> Event:
        return Event(
            id=str(uuid5(NAMESPACE_URL, f"crazy:eval:{eval_id}:{key}")),
            run_id=eval_id,
            task_id=eval_id,
            type=event_type,
            source="runtime.eval",
            payload=payload,
            causation_id=causation_id,
        )
