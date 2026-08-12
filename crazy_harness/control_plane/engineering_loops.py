from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from crazy_harness.control_plane.model_governance import ModelBudgetConfig
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.engineering_loops import (
    CandidateProposal,
    DeterministicLoopPolicy,
    EngineeringIterationIdentity,
    EngineeringLoopBudget,
    EngineeringLoopContract,
    IterationEvaluation,
    LoopDecision,
    LoopDecisionKind,
    MetricContract,
    PromotionMode,
    WorkerProfile,
    engineering_iteration_identity,
)
from crazy_harness.core.events import Event


class EngineeringLoopIdempotencyConflict(ValueError):
    code = "engineering_loop_idempotency_conflict"


class EngineeringLoopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=4000)
    exit_criteria: tuple[str, ...] = Field(min_length=1)
    loop_pack: str = Field(min_length=1)
    worker: WorkerProfile
    metric: MetricContract
    budget: EngineeringLoopBudget = Field(default_factory=EngineeringLoopBudget)
    promotion_mode: PromotionMode = PromotionMode.AUTO_DISPOSABLE
    permissions: tuple[str, ...] = ()
    initial_state_ref: str = Field(min_length=1)
    input_payload: dict[str, JsonValue] = Field(default_factory=dict)


class EngineeringLoopPublicBudget(BaseModel):
    """Only parent limits that the v1 runtime actually enforces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(default=3, ge=1, le=100)
    max_no_progress_iterations: int = Field(default=2, ge=0, le=100)


class EngineeringLoopCreateRequest(BaseModel):
    """Untrusted public input; Harness compiles the authority-bearing fields."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=4000)
    exit_criteria: tuple[str, ...] = Field(min_length=1)
    loop_pack: str = Field(min_length=1)
    model_mode: Literal["scripted", "deepseek"] = "scripted"
    model_budget: ModelBudgetConfig = Field(default_factory=ModelBudgetConfig)
    budget: EngineeringLoopPublicBudget = Field(
        default_factory=EngineeringLoopPublicBudget
    )


class EngineeringLoopCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    reason: str = Field(min_length=1, max_length=1000)


class EngineeringLoopPauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    reason: str = Field(min_length=1, max_length=1000)


class EngineeringLoopResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    reason: str = Field(min_length=1, max_length=1000)


class EngineeringLoopCreated(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    loop_id: str = Field(min_length=1)
    status: Literal["running"] = "running"
    contract: EngineeringLoopContract


class ChildRunOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: Literal["succeeded", "failed", "cancelled"]
    terminal_event_id: str = Field(min_length=1)
    candidate_state_ref: str | None = Field(default=None, min_length=1)
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_candidate_snapshot_for_success(self) -> ChildRunOutcome:
        if self.status == "succeeded" and self.candidate_state_ref is None:
            raise ValueError("succeeded child outcome requires candidate_state_ref")
        return self


class EngineeringIterationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: EngineeringIterationIdentity
    status: Literal[
        "planned",
        "candidate_proposed",
        "candidate_validated",
        "candidate_rejected",
        "running",
        "completed",
        "failed",
        "evaluated",
        "decided",
    ]
    base_state_ref: str = Field(min_length=1)
    candidate: CandidateProposal | None = None
    outcome: ChildRunOutcome | None = None
    evaluation: IterationEvaluation | None = None
    decision: LoopDecision | None = None
    failure_reason: str | None = None


class EngineeringLoopReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    loop_id: str = Field(min_length=1)
    status: Literal[
        "running",
        "pausing",
        "paused",
        "resuming",
        "awaiting_approval",
        "completed",
        "blocked",
        "cancelled",
    ]
    contract: EngineeringLoopContract
    active_state_ref: str = Field(min_length=1)
    active_score: Decimal | None = None
    pending_state_ref: str | None = None
    no_progress_count: int = Field(ge=0)
    iterations: tuple[EngineeringIterationReport, ...] = ()
    terminal_reason: str | None = None


class EngineeringLoopAdvanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    loop_id: str = Field(min_length=1)
    advanced: bool
    report: EngineeringLoopReport


class EngineeringLoopDrainResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    loop_id: str = Field(min_length=1)
    steps: int = Field(ge=0)
    report: EngineeringLoopReport


ProposeCandidate = Callable[
    [EngineeringLoopContract, EngineeringIterationIdentity, str],
    CandidateProposal,
]
LaunchChild = Callable[
    [EngineeringLoopContract, EngineeringIterationIdentity, CandidateProposal],
    None,
]
ReadChildOutcome = Callable[
    [EngineeringIterationIdentity],
    ChildRunOutcome | None,
]
EvaluateIteration = Callable[
    [EngineeringLoopContract, CandidateProposal, ChildRunOutcome],
    IterationEvaluation,
]
CancelChild = Callable[[str, str], None]
FaultInjector = Callable[[str], None]


class EngineeringLoopService:
    """Durable parent state machine for adaptive, independently evaluated iterations."""

    _CREATE_CLAIM_TTL_SECONDS = 15
    _ADVANCE_CLAIM_TTL_SECONDS = 300

    def __init__(
        self,
        store: SQLiteEventStore,
        *,
        policy: DeterministicLoopPolicy | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or DeterministicLoopPolicy()
        self.fault_injector = fault_injector or (lambda _phase: None)

    def create(self, request: EngineeringLoopRequest) -> EngineeringLoopCreated:
        loop_id = self.loop_id(request.request_id)
        owner_id = f"engineering-loop-creator:{uuid4().hex}"
        claims = self.store.claim_work(
            claim_keys=(f"engineering-loop-create:{loop_id}",),
            owner_id=owner_id,
            ttl_seconds=self._CREATE_CLAIM_TTL_SECONDS,
        )
        if claims is None:
            raise TimeoutError("engineering loop creation is already in progress")
        closed = False
        try:
            requested = self._request_event(loop_id, request)
            existing = self._events(loop_id, "engineering.loop.created")
            if len(existing) > 1:
                raise RuntimeError(f"engineering loop has multiple contracts: {loop_id}")
            if existing:
                contract = EngineeringLoopContract.model_validate(
                    existing[0].payload["contract"]
                )
                closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="completed",
                )
                if not closed:
                    raise RuntimeError("engineering loop creation claim was lost")
                return EngineeringLoopCreated(loop_id=loop_id, contract=contract)

            contract = self._build_contract(loop_id, request)
            event = self._event(
                loop_id,
                "created",
                "engineering.loop.created",
                {"contract": contract.model_dump(mode="json")},
                causation_id=requested.id,
            )
            closed = self.store.finish_work_claims(
                claims=claims,
                owner_id=owner_id,
                state="completed",
                final_event=event,
            )
            if not closed:
                raise RuntimeError("engineering loop creation claim was lost before commit")
            return EngineeringLoopCreated(loop_id=loop_id, contract=contract)
        finally:
            if not closed:
                self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )

    def contract(self, loop_id: str) -> EngineeringLoopContract:
        created = self._events(loop_id, "engineering.loop.created")
        if len(created) != 1:
            raise KeyError(f"engineering loop has no unique contract: {loop_id}")
        return EngineeringLoopContract.model_validate(created[0].payload["contract"])

    def report(self, loop_id: str) -> EngineeringLoopReport:
        contract = self.contract(loop_id)
        events = self.store.read_all(run_id=loop_id)
        grouped: dict[int, dict[str, Event]] = {}
        for event in events:
            raw_iteration = event.payload.get("iteration")
            if not isinstance(raw_iteration, int):
                continue
            phases = grouped.setdefault(raw_iteration, {})
            if event.type in phases:
                raise RuntimeError(
                    f"iteration {raw_iteration} has duplicate phase {event.type}"
                )
            phases[event.type] = event

        active_state_ref = contract.initial_state_ref
        active_score: Decimal | None = None
        pending_state_ref: str | None = None
        no_progress_count = 0
        iterations: list[EngineeringIterationReport] = []
        ordered_iterations = sorted(grouped)
        if ordered_iterations != list(range(1, len(ordered_iterations) + 1)):
            raise RuntimeError("engineering loop iterations are not contiguous")
        for iteration in ordered_iterations:
            phases = grouped[iteration]
            planned = phases.get("engineering.iteration.planned")
            if planned is None:
                raise RuntimeError(f"iteration {iteration} has no plan fact")
            identity = EngineeringIterationIdentity.model_validate(
                planned.payload["identity"]
            )
            if identity != engineering_iteration_identity(loop_id, iteration):
                raise RuntimeError(
                    f"iteration {iteration} does not use its deterministic identity"
                )
            base_state_ref = str(planned.payload["base_state_ref"])
            if base_state_ref != active_state_ref:
                raise RuntimeError(
                    f"iteration {iteration} breaks the active state lineage"
                )
            candidate_event = phases.get("engineering.candidate.proposed")
            candidate = (
                CandidateProposal.model_validate(candidate_event.payload["candidate"])
                if candidate_event is not None
                else None
            )
            outcome_event = phases.get("engineering.iteration.completed")
            outcome = (
                ChildRunOutcome.model_validate(outcome_event.payload["outcome"])
                if outcome_event is not None
                else None
            )
            evaluation_event = phases.get("engineering.evaluation.completed")
            evaluation = (
                IterationEvaluation.model_validate(
                    evaluation_event.payload["evaluation"]
                )
                if evaluation_event is not None
                else None
            )
            decision_event = phases.get("engineering.decision.recorded")
            decision = (
                LoopDecision.model_validate(decision_event.payload["decision"])
                if decision_event is not None
                else None
            )
            status = self._iteration_status(phases)
            failure = phases.get("engineering.iteration.failed")
            failure_reason = str(failure.payload["reason"]) if failure else None
            if outcome_event is not None and outcome is not None:
                legacy_mismatch = self._child_outcome_mismatch(identity, outcome)
                if legacy_mismatch is not None:
                    status = "failed"
                    failure_reason = f"legacy completion rejected: {legacy_mismatch}"
                    evaluation = None
                    decision = None
            report = EngineeringIterationReport(
                identity=identity,
                status=status,
                base_state_ref=base_state_ref,
                candidate=candidate,
                outcome=outcome,
                evaluation=evaluation,
                decision=decision,
                failure_reason=failure_reason,
            )
            iterations.append(report)
            if decision is not None:
                no_progress_count = decision.next_no_progress_count
                if decision.accepted:
                    if decision.active_state_ref is None or decision.score is None:
                        raise RuntimeError("accepted decision has no active state and score")
                    active_state_ref = decision.active_state_ref
                    active_score = decision.score
                if decision.kind is LoopDecisionKind.AWAITING_APPROVAL:
                    pending_state_ref = decision.pending_state_ref

        terminal_events = [
            event
            for event in events
            if event.type
            in {
                "engineering.loop.completed",
                "engineering.loop.blocked",
                "engineering.loop.cancelled",
            }
        ]
        if len(terminal_events) > 1:
            raise RuntimeError(f"engineering loop has multiple terminal facts: {loop_id}")
        status: Literal[
            "running",
            "pausing",
            "paused",
            "resuming",
            "awaiting_approval",
            "completed",
            "blocked",
            "cancelled",
        ] = "running"
        terminal_reason = None
        if terminal_events:
            terminal = terminal_events[0]
            status = {
                "engineering.loop.completed": "completed",
                "engineering.loop.blocked": "blocked",
                "engineering.loop.cancelled": "cancelled",
            }[terminal.type]
            terminal_reason = str(terminal.payload.get("reason", "")) or None
        elif iterations and iterations[-1].decision is not None:
            if (
                iterations[-1].decision.kind
                is LoopDecisionKind.AWAITING_APPROVAL
            ):
                status = "awaiting_approval"
        if not terminal_events and status != "awaiting_approval":
            control_status = self._control_status(events)
            if control_status is not None:
                status = control_status
        return EngineeringLoopReport(
            loop_id=loop_id,
            status=status,
            contract=contract,
            active_state_ref=active_state_ref,
            active_score=active_score,
            pending_state_ref=pending_state_ref,
            no_progress_count=no_progress_count,
            iterations=tuple(iterations),
            terminal_reason=terminal_reason,
        )

    def list_reports(self) -> list[EngineeringLoopReport]:
        loop_ids = {
            event.run_id
            for event in self.store.read_all(event_type="engineering.loop.created")
        }
        return [self.report(loop_id) for loop_id in sorted(loop_ids)]

    def cancel(
        self,
        loop_id: str,
        request: EngineeringLoopCancelRequest,
        *,
        cancel_child: CancelChild,
    ) -> EngineeringLoopReport:
        """Persist cancellation intent before touching a possibly active child Run."""

        owner_id = f"engineering-loop-cancel:{uuid4().hex}"
        claims = self.store.claim_work(
            claim_keys=(f"engineering-loop-advance:{loop_id}",),
            owner_id=owner_id,
            ttl_seconds=self._ADVANCE_CLAIM_TTL_SECONDS,
        )
        if claims is None:
            raise TimeoutError("engineering loop control is already in progress")
        closed = False
        try:
            current = self.report(loop_id)
            payload = {
                "request_id": request.request_id,
                "reason": request.reason,
            }
            requested = self._events(loop_id, "engineering.loop.cancellation.requested")
            if len(requested) > 1:
                raise RuntimeError("engineering loop has multiple cancellation intents")
            if requested and requested[0].payload != payload:
                raise EngineeringLoopIdempotencyConflict(
                    "engineering loop cancellation key was reused with different input"
                )
            if current.status == "cancelled":
                closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="completed",
                )
                return current
            if current.status in {"completed", "blocked"}:
                raise ValueError(
                    f"terminal engineering loop cannot be cancelled: {current.status}"
                )
            request_event = requested[0] if requested else self.store.append(
                self._event(
                    loop_id,
                    f"cancellation-request:{request.request_id}",
                    "engineering.loop.cancellation.requested",
                    payload,
                )
            )
            active_child = self._active_child_run_id(current)
            if active_child is not None:
                cancel_child(active_child, request.reason)
            event = self._terminal_event(
                loop_id,
                "cancelled",
                "engineering.loop.cancelled",
                {
                    **payload,
                    "request_event_id": request_event.id,
                    "active_child_run_id": active_child,
                },
            )
            closed = self.store.finish_work_claims(
                claims=claims,
                owner_id=owner_id,
                state="completed",
                final_event=event,
            )
            if not closed:
                raise RuntimeError("engineering loop cancellation claim was lost")
            return self.report(loop_id)
        finally:
            if not closed:
                self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )

    def pause(
        self,
        loop_id: str,
        request: EngineeringLoopPauseRequest,
    ) -> EngineeringLoopReport:
        current = self.report(loop_id)
        if current.status in {"completed", "blocked", "cancelled"}:
            raise ValueError(
                f"terminal engineering loop cannot be paused: {current.status}"
            )
        self._append_control_request(
            loop_id,
            kind="pause",
            request_id=request.request_id,
            reason=request.reason,
        )
        self.reconcile_control(loop_id)
        return self.report(loop_id)

    def resume(
        self,
        loop_id: str,
        request: EngineeringLoopResumeRequest,
    ) -> EngineeringLoopReport:
        current = self.report(loop_id)
        if current.status in {"completed", "blocked", "cancelled"}:
            raise ValueError(
                f"terminal engineering loop cannot be resumed: {current.status}"
            )
        if current.status == "running" and not self._events(
            loop_id, "engineering.loop.resume.requested"
        ):
            raise ValueError("running engineering loop cannot be resumed")
        self._append_control_request(
            loop_id,
            kind="resume",
            request_id=request.request_id,
            reason=request.reason,
        )
        self.reconcile_control(loop_id)
        return self.report(loop_id)

    def reconcile_control(self, loop_id: str) -> bool:
        """Settle the latest durable pause/resume intent under the advance barrier."""

        current = self.report(loop_id)
        event_type = {
            "pausing": "engineering.loop.paused",
            "resuming": "engineering.loop.resumed",
        }.get(current.status)
        if event_type is None:
            return False
        request_event = self._latest_control_request(loop_id)
        owner_id = f"engineering-loop-control:{uuid4().hex}"
        claims = self.store.claim_work(
            claim_keys=(f"engineering-loop-advance:{loop_id}",),
            owner_id=owner_id,
            ttl_seconds=self._ADVANCE_CLAIM_TTL_SECONDS,
        )
        if claims is None:
            return False
        closed = False
        try:
            if self.report(loop_id).status != current.status:
                closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )
                return False
            event = self._event(
                loop_id,
                f"control-applied:{request_event.id}",
                event_type,
                {
                    "request_id": request_event.payload["request_id"],
                    "reason": request_event.payload["reason"],
                    "request_event_id": request_event.id,
                },
                causation_id=request_event.id,
            )
            closed = self.store.finish_work_claims(
                claims=claims,
                owner_id=owner_id,
                state="completed",
                final_event=event,
            )
            return closed
        finally:
            if not closed:
                self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )

    def advance_one(
        self,
        loop_id: str,
        *,
        propose: ProposeCandidate,
        launch_child: LaunchChild,
        child_outcome: ReadChildOutcome,
        evaluate: EvaluateIteration,
    ) -> bool:
        owner_id = f"engineering-loop-advance:{uuid4().hex}"
        claims = self.store.claim_work(
            claim_keys=(f"engineering-loop-advance:{loop_id}",),
            owner_id=owner_id,
            ttl_seconds=self._ADVANCE_CLAIM_TTL_SECONDS,
        )
        if claims is None:
            return False
        closed = False
        try:
            current = self.report(loop_id)
            if current.status != "running":
                closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )
                return False

            event, fault_phase = self._next_event(
                current,
                propose=propose,
                launch_child=launch_child,
                child_outcome=child_outcome,
                evaluate=evaluate,
            )
            if event is None:
                closed = self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )
                return False
            closed = self.store.finish_work_claims(
                claims=claims,
                owner_id=owner_id,
                state="completed",
                final_event=event,
            )
            if not closed:
                return False
            if fault_phase is not None:
                self.fault_injector(fault_phase)
            return True
        finally:
            if not closed:
                self.store.finish_work_claims(
                    claims=claims,
                    owner_id=owner_id,
                    state="released",
                )

    def _next_event(
        self,
        report: EngineeringLoopReport,
        *,
        propose: ProposeCandidate,
        launch_child: LaunchChild,
        child_outcome: ReadChildOutcome,
        evaluate: EvaluateIteration,
    ) -> tuple[Event | None, str | None]:
        contract = report.contract
        if not report.iterations or (
            report.iterations[-1].decision is not None
            and report.iterations[-1].decision.kind
            in {
                LoopDecisionKind.ACCEPT_CONTINUE,
                LoopDecisionKind.REJECT_CONTINUE,
            }
        ):
            iteration = len(report.iterations) + 1
            identity = engineering_iteration_identity(report.loop_id, iteration)
            return (
                self._iteration_event(
                    report.loop_id,
                    identity,
                    "planned",
                    "engineering.iteration.planned",
                    {
                        "identity": identity.model_dump(mode="json"),
                        "base_state_ref": report.active_state_ref,
                    },
                ),
                "after_iteration_planned",
            )

        current = report.iterations[-1]
        identity = current.identity
        if current.status == "planned":
            candidate = propose(contract, identity, current.base_state_ref)
            return (
                self._iteration_event(
                    report.loop_id,
                    identity,
                    "candidate-proposed",
                    "engineering.candidate.proposed",
                    {"candidate": candidate.model_dump(mode="json")},
                ),
                "after_candidate_persisted",
            )
        if current.status == "candidate_proposed":
            assert current.candidate is not None
            reason = self._candidate_rejection_reason(current)
            event_type = (
                "engineering.candidate.rejected"
                if reason is not None
                else "engineering.candidate.validated"
            )
            return (
                self._iteration_event(
                    report.loop_id,
                    identity,
                    "candidate-rejected" if reason else "candidate-validated",
                    event_type,
                    {
                        "candidate_id": current.candidate.candidate_id,
                        **({"reason": reason} if reason else {}),
                    },
                ),
                "after_candidate_validated" if reason is None else None,
            )
        if current.status == "candidate_rejected":
            rejected = self._phase_event(
                report.loop_id,
                identity.iteration,
                "engineering.candidate.rejected",
            )
            return (
                self._terminal_event(
                    report.loop_id,
                    "blocked-candidate",
                    "engineering.loop.blocked",
                    {
                        "reason": str(rejected.payload["reason"]),
                        "iteration": identity.iteration,
                    },
                ),
                None,
            )
        if current.status == "candidate_validated":
            assert current.candidate is not None
            return (
                self._iteration_event(
                    report.loop_id,
                    identity,
                    "started",
                    "engineering.iteration.started",
                    {
                        "candidate_id": current.candidate.candidate_id,
                        "child_run_id": identity.child_run_id,
                        "child_task_id": identity.child_task_id,
                    },
                ),
                "after_child_linked",
            )
        if current.status == "running":
            assert current.candidate is not None
            # 父关联已经持久化；子 Prepare/Release 因而可以安全重放。
            launch_child(contract, identity, current.candidate)
            outcome = child_outcome(identity)
            if outcome is None:
                return None, None
            mismatch = self._child_outcome_mismatch(identity, outcome)
            if mismatch is not None:
                return (
                    self._iteration_event(
                        report.loop_id,
                        identity,
                        "failed",
                        "engineering.iteration.failed",
                        {"reason": mismatch},
                    ),
                    None,
                )
            return (
                self._iteration_event(
                    report.loop_id,
                    identity,
                    "completed",
                    "engineering.iteration.completed",
                    {"outcome": outcome.model_dump(mode="json")},
                ),
                "after_child_observed",
            )
        if current.status == "failed":
            return (
                self._terminal_event(
                    report.loop_id,
                    "blocked-child",
                    "engineering.loop.blocked",
                    {
                        "reason": current.failure_reason or "child iteration failed",
                        "iteration": identity.iteration,
                    },
                ),
                None,
            )
        if current.status == "completed":
            assert current.candidate is not None and current.outcome is not None
            assert current.outcome.candidate_state_ref is not None
            evaluation = evaluate(contract, current.candidate, current.outcome)
            evaluation = self._normalize_evaluation(current, evaluation)
            return (
                self._iteration_event(
                    report.loop_id,
                    identity,
                    "evaluated",
                    "engineering.evaluation.completed",
                    {"evaluation": evaluation.model_dump(mode="json")},
                ),
                "after_evaluation_persisted",
            )
        if current.status == "evaluated":
            assert current.evaluation is not None
            decision = self.policy.decide(
                contract,
                iteration=identity.iteration,
                evaluation=current.evaluation,
                active_score=report.active_score,
                no_progress_count=report.no_progress_count,
            )
            return (
                self._iteration_event(
                    report.loop_id,
                    identity,
                    "decision",
                    "engineering.decision.recorded",
                    {"decision": decision.model_dump(mode="json")},
                ),
                "after_decision_persisted",
            )
        if current.status == "decided":
            assert current.decision is not None
            if current.decision.kind is LoopDecisionKind.COMPLETE:
                return (
                    self._terminal_event(
                        report.loop_id,
                        "completed",
                        "engineering.loop.completed",
                        {
                            "reason": current.decision.reason,
                            "iteration": identity.iteration,
                        },
                    ),
                    "after_loop_terminal",
                )
            if current.decision.kind is LoopDecisionKind.BLOCKED:
                return (
                    self._terminal_event(
                        report.loop_id,
                        "blocked-policy",
                        "engineering.loop.blocked",
                        {
                            "reason": current.decision.reason,
                            "iteration": identity.iteration,
                        },
                    ),
                    "after_loop_terminal",
                )
        return None, None

    @staticmethod
    def _active_child_run_id(report: EngineeringLoopReport) -> str | None:
        if not report.iterations:
            return None
        current = report.iterations[-1]
        if current.status != "running":
            return None
        return current.identity.child_run_id

    def _append_control_request(
        self,
        loop_id: str,
        *,
        kind: Literal["pause", "resume"],
        request_id: str,
        reason: str,
    ) -> Event:
        event = self._event(
            loop_id,
            f"control-request:{kind}:{request_id}",
            f"engineering.loop.{kind}.requested",
            {"request_id": request_id, "reason": reason},
        )
        try:
            return self.store.append(event)
        except ValueError as exc:
            raise EngineeringLoopIdempotencyConflict(
                "engineering loop control request key was reused with different input"
            ) from exc

    def _latest_control_request(self, loop_id: str) -> Event:
        requests = [
            event
            for event in self.store.read_all(run_id=loop_id)
            if event.type
            in {
                "engineering.loop.pause.requested",
                "engineering.loop.resume.requested",
            }
        ]
        if not requests:
            raise RuntimeError("engineering loop has no pending control request")
        return requests[-1]

    @staticmethod
    def _control_status(
        events: list[Event],
    ) -> Literal["running", "pausing", "paused", "resuming"] | None:
        mapping = {
            "engineering.loop.pause.requested": "pausing",
            "engineering.loop.paused": "paused",
            "engineering.loop.resume.requested": "resuming",
            "engineering.loop.resumed": "running",
        }
        controls = [event for event in events if event.type in mapping]
        if not controls:
            return None
        return mapping[controls[-1].type]  # type: ignore[return-value]

    @staticmethod
    def _candidate_rejection_reason(
        iteration: EngineeringIterationReport,
    ) -> str | None:
        candidate = iteration.candidate
        assert candidate is not None
        if candidate.candidate_id != iteration.identity.candidate_id:
            return "candidate identity does not match the planned iteration"
        if candidate.iteration != iteration.identity.iteration:
            return "candidate iteration does not match the planned iteration"
        try:
            candidate.validate_base(iteration.base_state_ref)
        except ValueError as exc:
            return str(exc)
        return None

    @staticmethod
    def _child_outcome_mismatch(
        identity: EngineeringIterationIdentity,
        outcome: ChildRunOutcome,
    ) -> str | None:
        if outcome.run_id != identity.child_run_id:
            return "child outcome run identity mismatch"
        if outcome.task_id != identity.child_task_id:
            return "child outcome task identity mismatch"
        if outcome.status != "succeeded":
            snapshot_state = (
                "candidate snapshot absent"
                if outcome.candidate_state_ref is None
                else "candidate snapshot present but ineligible"
            )
            return (
                f"child run {outcome.status}; "
                f"terminal_event_id={outcome.terminal_event_id}; "
                f"{snapshot_state}; evaluation skipped"
            )
        return None

    @staticmethod
    def _normalize_evaluation(
        iteration: EngineeringIterationReport,
        evaluation: IterationEvaluation,
    ) -> IterationEvaluation:
        assert iteration.candidate is not None and iteration.outcome is not None
        reasons: list[str] = list(evaluation.invalid_reasons)
        if evaluation.iteration != iteration.identity.iteration:
            reasons.append("evaluation iteration mismatch")
        if evaluation.candidate_id != iteration.candidate.candidate_id:
            reasons.append("evaluation candidate mismatch")
        if evaluation.candidate_state_ref != iteration.outcome.candidate_state_ref:
            reasons.append("evaluation state mismatch")
        if reasons:
            return evaluation.model_copy(
                update={"valid": False, "invalid_reasons": tuple(dict.fromkeys(reasons))}
            )
        return evaluation

    def _request_event(
        self,
        loop_id: str,
        request: EngineeringLoopRequest,
    ) -> Event:
        payload = {"request": request.model_dump(mode="json")}
        existing = self._events(loop_id, "engineering.loop.requested")
        if len(existing) > 1:
            raise RuntimeError(f"engineering loop has multiple requests: {loop_id}")
        if existing:
            if existing[0].payload != payload:
                raise EngineeringLoopIdempotencyConflict(
                    "engineering loop request key was reused with different input"
                )
            return existing[0]
        return self.store.append(
            self._event(
                loop_id,
                "requested",
                "engineering.loop.requested",
                payload,
            )
        )

    @staticmethod
    def _build_contract(
        loop_id: str,
        request: EngineeringLoopRequest,
    ) -> EngineeringLoopContract:
        return EngineeringLoopContract(
            loop_id=loop_id,
            request_fingerprint=EngineeringLoopService._request_fingerprint(request),
            title=request.title,
            objective=request.objective,
            exit_criteria=request.exit_criteria,
            loop_pack=request.loop_pack,
            worker=request.worker,
            metric=request.metric,
            budget=request.budget,
            promotion_mode=request.promotion_mode,
            permissions=request.permissions,
            initial_state_ref=request.initial_state_ref,
            input_payload=request.input_payload,
        )

    @staticmethod
    def _iteration_status(
        phases: dict[str, Event],
    ) -> Literal[
        "planned",
        "candidate_proposed",
        "candidate_validated",
        "candidate_rejected",
        "running",
        "completed",
        "failed",
        "evaluated",
        "decided",
    ]:
        order = (
            ("engineering.decision.recorded", "decided"),
            ("engineering.evaluation.completed", "evaluated"),
            ("engineering.iteration.failed", "failed"),
            ("engineering.iteration.completed", "completed"),
            ("engineering.iteration.started", "running"),
            ("engineering.candidate.rejected", "candidate_rejected"),
            ("engineering.candidate.validated", "candidate_validated"),
            ("engineering.candidate.proposed", "candidate_proposed"),
            ("engineering.iteration.planned", "planned"),
        )
        for event_type, status in order:
            if event_type in phases:
                return status  # type: ignore[return-value]
        raise RuntimeError("iteration has no known phase")

    def _phase_event(
        self,
        loop_id: str,
        iteration: int,
        event_type: str,
    ) -> Event:
        matches = [
            event
            for event in self.store.read_all(run_id=loop_id)
            if event.type == event_type and event.payload.get("iteration") == iteration
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"iteration {iteration} has no unique {event_type} fact"
            )
        return matches[0]

    @staticmethod
    def loop_id(request_id: str) -> str:
        value = uuid5(NAMESPACE_URL, f"crazy:engineering-loop-request:{request_id}")
        return f"loop_{value.hex[:12]}"

    @staticmethod
    def _request_fingerprint(request: EngineeringLoopRequest) -> str:
        encoded = json.dumps(
            request.model_dump(mode="json", exclude={"request_id"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _event(
        loop_id: str,
        key: str,
        event_type: str,
        payload: dict[str, object],
        *,
        causation_id: str | None = None,
    ) -> Event:
        return Event(
            id=str(uuid5(NAMESPACE_URL, f"crazy:engineering-loop:{loop_id}:{key}")),
            run_id=loop_id,
            task_id=loop_id,
            type=event_type,
            source="runtime.engineering-loop",
            payload=payload,
            causation_id=causation_id,
        )

    @classmethod
    def _iteration_event(
        cls,
        loop_id: str,
        identity: EngineeringIterationIdentity,
        phase: str,
        event_type: str,
        payload: dict[str, object],
    ) -> Event:
        return cls._event(
            loop_id,
            f"iteration:{identity.iteration}:{phase}",
            event_type,
            {"iteration": identity.iteration, **payload},
        )

    @classmethod
    def _terminal_event(
        cls,
        loop_id: str,
        phase: str,
        event_type: str,
        payload: dict[str, object],
    ) -> Event:
        return cls._event(loop_id, f"terminal:{phase}", event_type, payload)

    def _events(self, loop_id: str, event_type: str) -> list[Event]:
        return [
            event
            for event in self.store.read_all(run_id=loop_id)
            if event.type == event_type
        ]


__all__ = [
    "ChildRunOutcome",
    "EngineeringLoopAdvanceResult",
    "EngineeringLoopCancelRequest",
    "EngineeringLoopCreateRequest",
    "EngineeringIterationReport",
    "EngineeringLoopCreated",
    "EngineeringLoopDrainResult",
    "EngineeringLoopIdempotencyConflict",
    "EngineeringLoopPauseRequest",
    "EngineeringLoopPublicBudget",
    "EngineeringLoopReport",
    "EngineeringLoopRequest",
    "EngineeringLoopResumeRequest",
    "EngineeringLoopService",
]
