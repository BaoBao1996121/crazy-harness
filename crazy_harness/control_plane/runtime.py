from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from inspect import getsource
from pathlib import Path
from time import monotonic, sleep
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.control_plane.checkpoints import (
    CheckpointCreateRequest,
    CheckpointRestored,
    CheckpointRestoreRequest,
    CheckpointService,
)
from crazy_harness.control_plane.context import PersistentContextCompiler
from crazy_harness.control_plane.eval_campaigns import (
    EvalCampaignCreated,
    EvalCampaignReport,
    EvalCampaignRequest,
    EvalCampaignService,
)
from crazy_harness.control_plane.engineering_loops import (
    ChildRunOutcome,
    EngineeringLoopCreated,
    EngineeringLoopReport,
    EngineeringLoopRequest,
    EngineeringLoopService,
)
from crazy_harness.control_plane.kernel import (
    CommandCandidate,
    CommandKind,
    ControlKernel,
    FaultController,
    InjectedKernelCrash,
    KernelDecision,
)
from crazy_harness.control_plane.model_governance import (
    ModelBudgetConfig,
    PersistentModelCallAuthority,
)
from crazy_harness.control_plane.run_controls import (
    AgentRunBranchView,
    RunControlResult,
    RunForkRequest,
    RunNudgeRequest,
    RunNudgeResult,
    RunPauseRequest,
    RunResumeRequest,
)
from crazy_harness.control_plane.paired_evals import (
    EvalRunIdentity,
    PairedEvalCreated,
    PairedEvalReport,
    PairedEvalRequest,
    PairedEvalService,
    paired_eval_id,
    paired_input_hash,
)
from crazy_harness.control_plane.store import (
    SQLiteEventStore,
    UnfencedAckError,
    WorkClaimLost,
)
from crazy_harness.control_plane.team_workers import TeamModelFactory, TeamWorkerEngine
from crazy_harness.core.a2a.coordinator import AgentStatus
from crazy_harness.core.a2a.messages import AgentCard
from crazy_harness.core.a2a.orchestration import (
    CapabilitySupervisorPolicy,
    SupervisorContext,
    SupervisorPolicy,
    TeamContract,
)
from crazy_harness.core.agents import (
    AgentRunKind,
    AgentRunSession,
    AgentRunSessionIdentity,
    AgentRunSessionView,
    AssignmentContract,
)
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.checkpoints import CheckpointContract, WorkspaceSnapshotStore
from crazy_harness.core.events import Event
from crazy_harness.core.engineering_loops import (
    CandidateProposal,
    EngineeringIterationIdentity,
    EngineeringLoopContract,
)
from crazy_harness.core.models import (
    DeepSeekOpenAIProvider,
    FakeModelProvider,
    ModelProvider,
)
from crazy_harness.core.runtime import DurableMailbox
from crazy_harness.core.dispatch import (
    DispatchCancelled,
    DispatchContext,
    activate_dispatch_context,
)
from crazy_harness.core.runtime.mailbox import Delivery
from crazy_harness.loop_packs import LoopPack, RepoQualityLoopPack
from crazy_harness.taskpacks import (
    EvidenceResearchTaskPack,
    RepoMaintainerTaskPack,
    RepoMaintainerTeamTaskPack,
    RepoQualityTaskPack,
    ResidentDemoTeamTaskPack,
    TaskPack,
)


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(min_length=1, max_length=4000)
    model_mode: Literal["scripted", "deepseek"] = "scripted"
    execution_mode: Literal["team", "single"] = "team"
    task_pack: (
        Literal[
            "resident-demo",
            "repo-maintainer",
            "evidence-research",
            "repo-quality",
        ]
        | None
    ) = None
    model_budget: ModelBudgetConfig = Field(default_factory=ModelBudgetConfig)


class RunCreated(BaseModel):
    run_id: str
    task_id: str
    status: str = "queued"


Handler = Callable[[Delivery], None]
ModelFactory = Callable[[str], ModelProvider]


class ResidentScheduler:
    """A tiny always-on dispatcher; durable mailboxes remain the source of pending work."""

    MAX_DELIVERY_FAILURES = 3
    DEFAULT_WORK_CLAIM_SECONDS = 180

    def __init__(
        self,
        store: SQLiteEventStore,
        fault_controller: FaultController,
        *,
        max_workers: int = 2,
        work_claim_seconds: int = DEFAULT_WORK_CLAIM_SECONDS,
    ) -> None:
        if max_workers < 1:
            raise ValueError("scheduler max_workers must be positive")
        if work_claim_seconds < 1:
            raise ValueError("scheduler work_claim_seconds must be positive")
        self.store = store
        self.fault_controller = fault_controller
        self._workers: dict[str, tuple[DurableMailbox, Handler, int]] = {}
        self._max_workers = max_workers
        self._work_claim_seconds = work_claim_seconds
        self._owner_id = f"scheduler_{uuid4().hex}"
        self._condition = threading.Condition()
        self._wake_generation = 0
        self._consumed_generation = 0
        self._selection_cursor = 0
        self._in_flight: dict[
            tuple[str, str], tuple[Delivery, dict[str, int], DispatchContext]
        ] = {}
        self._claim_deadlines: dict[tuple[str, str], datetime] = {}
        self._completed_steps = 0
        self._executor: ThreadPoolExecutor | None = None
        self._accepting = True
        self._backpressure_signature: tuple[object, ...] | None = None
        self._cancelled_runs: set[str] = set()
        self._finalizing: set[tuple[str, str]] = set()
        self._lost_reservations: set[tuple[str, str]] = set()
        self._renewer_stop = threading.Event()
        self._renewer_wake = threading.Event()
        self._renewer_thread: threading.Thread | None = None

    def register(
        self,
        worker_id: str,
        mailbox: DurableMailbox,
        handler: Handler,
        *,
        max_concurrency: int = 1,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("worker max_concurrency must be positive")
        if worker_id in self._workers:
            raise ValueError(f"worker already registered: {worker_id}")
        self._workers[worker_id] = (mailbox, handler, max_concurrency)

    @property
    def in_flight_count(self) -> int:
        with self._condition:
            return len(self._in_flight)

    @property
    def completed_steps(self) -> int:
        with self._condition:
            return self._completed_steps

    @property
    def pending_count(self) -> int:
        with self._condition:
            return self._pending_count_locked()

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            claimed_deliveries = self._active_delivery_claim_keys()
            workers = []
            for worker_id, (mailbox, _, capacity) in self._workers.items():
                active = self._worker_in_flight_locked(worker_id)
                queued = sum(
                    (worker_id, delivery.delivery_id) not in self._in_flight
                    and f"delivery:{mailbox.mailbox_id}:{delivery.delivery_id}"
                    not in claimed_deliveries
                    for delivery in mailbox.pending()
                )
                workers.append(
                    {
                        "worker_id": worker_id,
                        "active": active,
                        "capacity": capacity,
                        "queued": queued,
                    }
                )
            return {
                "instance_id": self._owner_id,
                "state": "accepting" if self._accepting else "paused",
                "policy": "round_robin",
                "fairness_scope": "process_workers",
                "active": len(self._in_flight),
                "capacity": self._max_workers,
                "queued": sum(int(worker["queued"]) for worker in workers),
                "workers": workers,
            }

    def claim_keys_for(
        self, worker_id: str, mailbox: DurableMailbox, delivery: Delivery
    ) -> tuple[str, str]:
        return self._claim_keys(worker_id, mailbox, delivery)

    def resume(self) -> None:
        with self._condition:
            self._accepting = True
            self._condition.notify_all()

    def pause(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def cancel_run(
        self,
        run_id: str,
        *,
        reason: str,
        record_events: bool = True,
    ) -> int:
        with self._condition:
            self._cancelled_runs.add(run_id)
            targets = [
                (worker_id, delivery, claims, context)
                for (worker_id, _), (
                    delivery,
                    claims,
                    context,
                ) in self._in_flight.items()
                if delivery.event.run_id == run_id
            ]
            for _, _, _, context in targets:
                context.cancellation.cancel(reason)
            self._wake_generation += 1
            self._condition.notify_all()
        if record_events:
            for worker_id, delivery, claims, _ in targets:
                self._append(
                    delivery.event,
                    "runtime.delivery.cancellation.requested",
                    {
                        "agent_id": worker_id,
                        "delivery_id": delivery.delivery_id,
                        "reason": reason,
                        "claim_tokens": claims,
                    },
                    key=f"cancel-requested:{delivery.delivery_id}:{reason}",
                )
        return len(targets)

    def in_flight_for_run(self, run_id: str) -> int:
        with self._condition:
            return sum(
                delivery.event.run_id == run_id
                for delivery, _, _ in self._in_flight.values()
            )

    def shutdown(self, *, wait: bool = True) -> None:
        with self._condition:
            self._accepting = False
            executor = self._executor
            self._executor = None
            self._condition.notify_all()
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=False)
        if wait:
            with self._condition:
                self._condition.wait_for(lambda: not self._in_flight)
            self._stop_renewer()
        elif self.in_flight_count == 0:
            self._stop_renewer()

    def signal(self) -> None:
        with self._condition:
            self._wake_generation += 1
            self._condition.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        with self._condition:
            if self._consumed_generation == self._wake_generation:
                self._condition.wait_for(
                    lambda: self._consumed_generation != self._wake_generation,
                    timeout,
                )
            signaled = self._consumed_generation != self._wake_generation
            self._consumed_generation = self._wake_generation
            return signaled

    def has_pending(self) -> bool:
        return self.pending_count > 0 or self.in_flight_count > 0

    def wait_until_idle(self, *, timeout: float) -> bool:
        deadline = monotonic() + timeout
        while True:
            if self.in_flight_count == 0 and self.pending_count == 0:
                return True
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            with self._condition:
                self._condition.wait(min(remaining, 0.05))

    def wait_for_progress(self, *, completed_steps: int, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._completed_steps != completed_steps,
                timeout,
            )

    def run_once(
        self,
        *,
        allowed_run_ids: frozenset[str] | None = None,
    ) -> bool:
        """Execute one reserved Delivery synchronously for deterministic stepping."""

        with self._condition:
            selected = self._reserve_next_locked(allowed_run_ids=allowed_run_ids)
        if selected is None:
            return False
        self._execute_reserved(*selected)
        return True

    def dispatch_available(self) -> int:
        """Fill the bounded pool without moving authority out of the Scheduler."""

        dispatched = 0
        with self._condition:
            if not self._accepting:
                return 0
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_workers,
                    thread_name_prefix="crazy-agent-worker",
                )
            while (selected := self._reserve_next_locked()) is not None:
                try:
                    self._executor.submit(self._execute_reserved, *selected)
                except Exception:
                    worker_id, _, _, delivery, claims, _ = selected
                    self.store.finish_work_claims(
                        claims=claims,
                        owner_id=self._owner_id,
                        state="released",
                    )
                    self._release_reservation_locked(worker_id, delivery)
                    raise
                dispatched += 1
            backpressure = self._backpressure_locked()
        if backpressure is not None:
            identity, key, payload = backpressure
            self._append(
                identity,
                "runtime.scheduler.backpressure",
                payload,
                key=key,
            )
        return dispatched

    def _execute_reserved(
        self,
        worker_id: str,
        mailbox: DurableMailbox,
        handler: Handler,
        delivery: Delivery,
        claims: dict[str, int],
        dispatch_context: DispatchContext,
    ) -> None:
        claim_state = "released"
        claims_finished = False
        try:
            attempt = 1 + sum(
                event.type == "runtime.delivery.dispatched"
                and event.payload.get("delivery_id") == delivery.delivery_id
                for event in self.store.read_all()
            )
            with self._condition:
                worker_in_flight = self._worker_in_flight_locked(worker_id)
                worker_capacity = self._workers[worker_id][2]
                active = len(self._in_flight)
            self._record_claim_state(
                worker_id=worker_id,
                delivery=delivery,
                claims=claims,
                event_type="runtime.delivery.claim.acquired",
                state="active",
            )
            self._append(
                delivery.event,
                "runtime.delivery.dispatched",
                {
                    "agent_id": worker_id,
                    "delivery_id": delivery.delivery_id,
                    "attempt": attempt,
                    "active": active,
                    "capacity": self._max_workers,
                    "worker_in_flight": worker_in_flight,
                    "worker_capacity": worker_capacity,
                    "claim_tokens": claims,
                },
                key=f"dispatch:{worker_id}:{delivery.delivery_id}:{attempt}",
            )
            dead_letter = self._dead_letter(delivery.delivery_id)
            if dead_letter is not None:
                self._fail_running_run_for_dead_letter(delivery, dead_letter)
                claims_finished = self._finish_claims_with_ack(
                    worker_id=worker_id,
                    mailbox=mailbox,
                    delivery=delivery,
                    claims=claims,
                )
                return
            self._append(
                delivery.event,
                "runtime.agent.busy",
                {
                    "agent_id": worker_id,
                    "delivery_id": delivery.delivery_id,
                    "in_flight": worker_in_flight,
                    "max_concurrency": worker_capacity,
                },
            )
            try:
                with activate_dispatch_context(dispatch_context):
                    dispatch_context.cancellation.raise_if_cancelled()
                    handler(delivery)
                    dispatch_context.cancellation.raise_if_cancelled()
                self.fault_controller.trip("before_mailbox_ack")
            except DispatchCancelled as exc:
                if not str(exc).startswith("work claim renewal"):
                    claims_finished = self._finish_claims_with_ack(
                        worker_id=worker_id,
                        mailbox=mailbox,
                        delivery=delivery,
                        claims=claims,
                        allow_cancelled_run=True,
                    )
                    if claims_finished:
                        self._append(
                            delivery.event,
                            "runtime.delivery.cancelled",
                            {
                                "agent_id": worker_id,
                                "delivery_id": delivery.delivery_id,
                                "reason": str(exc),
                                "mode": "cooperative",
                            },
                            key=f"delivery-cancelled:{delivery.delivery_id}",
                        )
                return
            except WorkClaimLost:
                dispatch_context.cancellation.cancel(
                    "work claim fencing token was lost"
                )
                return
            except InjectedKernelCrash as exc:
                # 不 ack：同一 Delivery 会再次出现，业务命令依靠幂等键恢复。
                try:
                    with activate_dispatch_context(dispatch_context):
                        self._append(
                            delivery.event,
                            "runtime.agent.crashed",
                            {
                                "agent_id": worker_id,
                                "delivery_id": delivery.delivery_id,
                                "reason": str(exc),
                                "redelivery": True,
                            },
                        )
                except WorkClaimLost:
                    dispatch_context.cancellation.cancel(
                        "work claim fencing token was lost"
                    )
                return
            except Exception as exc:
                try:
                    with activate_dispatch_context(dispatch_context):
                        should_ack = self._handle_unexpected_failure(
                            worker_id=worker_id,
                            delivery=delivery,
                            error=exc,
                        )
                except WorkClaimLost:
                    dispatch_context.cancellation.cancel(
                        "work claim fencing token was lost"
                    )
                    return
                if should_ack:
                    claims_finished = self._finish_claims_with_ack(
                        worker_id=worker_id,
                        mailbox=mailbox,
                        delivery=delivery,
                        claims=claims,
                    )
                return

            claims_finished = self._finish_claims_with_ack(
                worker_id=worker_id,
                mailbox=mailbox,
                delivery=delivery,
                claims=claims,
            )
            if not claims_finished:
                return
            with self._condition:
                remaining = max(0, self._worker_in_flight_locked(worker_id) - 1)
            self._append(
                delivery.event,
                "runtime.agent.step.completed",
                {
                    "agent_id": worker_id,
                    "delivery_id": delivery.delivery_id,
                    "in_flight": remaining,
                },
            )
            if remaining == 0:
                self._append(
                    delivery.event,
                    "runtime.agent.idle",
                    {"agent_id": worker_id, "in_flight": 0},
                )
        finally:
            try:
                if not claims_finished:
                    with self._condition:
                        self._finalizing.add((worker_id, delivery.delivery_id))
                    finished = self.store.finish_work_claims(
                        claims=claims,
                        owner_id=self._owner_id,
                        state=claim_state,
                    )
                    if not finished:
                        self._record_claim_lost(
                            worker_id=worker_id,
                            delivery=delivery,
                            claims=claims,
                            reason="terminal_fence_rejected",
                        )
                    else:
                        self._record_claim_state(
                            worker_id=worker_id,
                            delivery=delivery,
                            claims=claims,
                            event_type="runtime.delivery.claim.released",
                            state=claim_state,
                        )
            finally:
                with self._condition:
                    self._release_reservation_locked(worker_id, delivery)

    def _finish_claims_with_ack(
        self,
        *,
        worker_id: str,
        mailbox: DurableMailbox,
        delivery: Delivery,
        claims: dict[str, int],
        allow_cancelled_run: bool = False,
    ) -> bool:
        with self._condition:
            self._finalizing.add((worker_id, delivery.delivery_id))
        finished = self.store.finish_work_claims(
            claims=claims,
            owner_id=self._owner_id,
            state="completed",
            final_event=mailbox.ack_event(delivery.delivery_id),
            allow_cancelled_run=allow_cancelled_run,
        )
        if not finished:
            self._record_claim_lost(
                worker_id=worker_id,
                delivery=delivery,
                claims=claims,
                reason="ack_fence_rejected",
            )
        else:
            self._record_claim_state(
                worker_id=worker_id,
                delivery=delivery,
                claims=claims,
                event_type="runtime.delivery.claim.released",
                state="completed",
            )
        return finished

    def _record_claim_state(
        self,
        *,
        worker_id: str,
        delivery: Delivery,
        claims: dict[str, int],
        event_type: str,
        state: str,
        expires_at: str | None = None,
    ) -> None:
        token_key = ":".join(f"{key}={token}" for key, token in sorted(claims.items()))
        self._append(
            delivery.event,
            event_type,
            {
                "agent_id": worker_id,
                "delivery_id": delivery.delivery_id,
                "claim_tokens": claims,
                "state": state,
                "expires_at": expires_at,
            },
            key=f"claim-state:{event_type}:{delivery.delivery_id}:{token_key}:{expires_at}",
        )

    def _record_claim_lost(
        self,
        *,
        worker_id: str,
        delivery: Delivery,
        claims: dict[str, int],
        reason: str,
    ) -> None:
        token_key = ":".join(f"{key}={token}" for key, token in sorted(claims.items()))
        self._append(
            delivery.event,
            "runtime.delivery.claim.lost",
            {
                "agent_id": worker_id,
                "delivery_id": delivery.delivery_id,
                "claim_tokens": claims,
                "reason": reason,
            },
            key=f"claim-lost:{worker_id}:{delivery.delivery_id}:{reason}:{token_key}",
        )

    def _reserve_next_locked(
        self,
        *,
        allowed_run_ids: frozenset[str] | None = None,
    ) -> (
        tuple[
            str,
            DurableMailbox,
            Handler,
            Delivery,
            dict[str, int],
            DispatchContext,
        ]
        | None
    ):
        if not self._accepting or len(self._in_flight) >= self._max_workers:
            return None
        worker_ids = list(self._workers)
        if not worker_ids:
            return None
        for offset in range(len(worker_ids)):
            index = (self._selection_cursor + offset) % len(worker_ids)
            worker_id = worker_ids[index]
            mailbox, handler, max_concurrency = self._workers[worker_id]
            if self._worker_in_flight_locked(worker_id) >= max_concurrency:
                continue
            for delivery in mailbox.pending():
                if (
                    allowed_run_ids is not None
                    and delivery.event.run_id not in allowed_run_ids
                ):
                    continue
                if delivery.event.run_id in self._cancelled_runs:
                    continue
                reservation_key = (worker_id, delivery.delivery_id)
                if reservation_key in self._in_flight:
                    continue
                claims = None
                claim_deadline = None
                for worker_slot in range(max_concurrency):
                    claimed_at = datetime.now(timezone.utc)
                    claims = self.store.claim_work(
                        claim_keys=self._claim_keys(
                            worker_id,
                            mailbox,
                            delivery,
                            worker_slot=worker_slot,
                        ),
                        owner_id=self._owner_id,
                        ttl_seconds=self._work_claim_seconds,
                        now=claimed_at,
                        run_id=delivery.event.run_id,
                    )
                    if claims is not None:
                        claim_deadline = claimed_at + timedelta(
                            seconds=self._work_claim_seconds
                        )
                        break
                if claims is None:
                    continue
                dispatch_context = DispatchContext.create(
                    worker_id=worker_id,
                    delivery_id=delivery.delivery_id,
                    claim_owner_id=self._owner_id,
                    claim_tokens=claims,
                )
                self._in_flight[reservation_key] = (
                    delivery,
                    claims,
                    dispatch_context,
                )
                assert claim_deadline is not None
                self._claim_deadlines[reservation_key] = claim_deadline
                self._ensure_renewer_locked()
                self._selection_cursor = (index + 1) % len(worker_ids)
                return (
                    worker_id,
                    mailbox,
                    handler,
                    delivery,
                    claims,
                    dispatch_context,
                )
        return None

    def _ensure_renewer_locked(self) -> None:
        if self._renewer_thread is not None and self._renewer_thread.is_alive():
            return
        self._renewer_stop.clear()
        self._renewer_thread = threading.Thread(
            target=self._renew_claims_loop,
            name="crazy-claim-renewer",
            daemon=False,
        )
        self._renewer_thread.start()

    def _stop_renewer(self) -> None:
        self._renewer_stop.set()
        self._renewer_wake.set()
        thread = self._renewer_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self._work_claim_seconds / 3 + 0.5))
            if thread.is_alive():
                raise RuntimeError("work claim renewer did not stop")
        self._renewer_thread = None

    def _renew_claims_loop(self) -> None:
        interval = max(0.05, self._work_claim_seconds / 3)
        while True:
            self._renewer_wake.wait(interval)
            self._renewer_wake.clear()
            if self._renewer_stop.is_set():
                return
            with self._condition:
                if not self._in_flight:
                    self._renewer_thread = None
                    return
                reservations = [
                    (
                        worker_id,
                        delivery,
                        claims,
                        dispatch_context,
                        self._claim_deadlines.get((worker_id, delivery_id)),
                    )
                    for (worker_id, delivery_id), (
                        delivery,
                        claims,
                        dispatch_context,
                    ) in self._in_flight.items()
                    if (worker_id, delivery_id) not in self._finalizing
                    and (worker_id, delivery_id) not in self._lost_reservations
                ]
            for (
                worker_id,
                delivery,
                claims,
                dispatch_context,
                claim_deadline,
            ) in reservations:
                renewed_at = datetime.now(timezone.utc)
                try:
                    renewed = self.store.renew_work_claims(
                        claims=claims,
                        owner_id=self._owner_id,
                        ttl_seconds=self._work_claim_seconds,
                        now=renewed_at,
                    )
                except Exception as exc:
                    reservation_key = (worker_id, delivery.delivery_id)
                    with self._condition:
                        if reservation_key not in self._in_flight:
                            continue
                        cancel_before_expiry = (
                            claim_deadline is None
                            or renewed_at + timedelta(seconds=interval)
                            >= claim_deadline
                        )
                        if cancel_before_expiry:
                            self._lost_reservations.add(reservation_key)
                            dispatch_context.cancellation.cancel(
                                "work claim renewal could not be confirmed"
                            )
                            # 终止事实必须先于 Reservation 释放可见；否则
                            # in_flight == 0 不能作为恢复与审计的完成边界。
                            self._record_renewal_failure(
                                worker_id=worker_id,
                                delivery=delivery,
                                error=exc,
                                will_retry=False,
                            )
                            continue
                    self._record_renewal_failure(
                        worker_id=worker_id,
                        delivery=delivery,
                        error=exc,
                        will_retry=True,
                    )
                    continue
                if renewed:
                    renewed_deadline = renewed_at + timedelta(
                        seconds=self._work_claim_seconds
                    )
                    with self._condition:
                        self._claim_deadlines[(worker_id, delivery.delivery_id)] = (
                            renewed_deadline
                        )
                    try:
                        self._record_claim_state(
                            worker_id=worker_id,
                            delivery=delivery,
                            claims=claims,
                            event_type="runtime.delivery.claim.renewed",
                            state="active",
                            expires_at=renewed_deadline.isoformat(),
                        )
                    except Exception as exc:
                        self._record_renewal_failure(
                            worker_id=worker_id,
                            delivery=delivery,
                            error=exc,
                            will_retry=True,
                        )
                    continue
                reservation_key = (worker_id, delivery.delivery_id)
                with self._condition:
                    if (
                        reservation_key not in self._in_flight
                        or reservation_key in self._finalizing
                        or reservation_key in self._lost_reservations
                    ):
                        continue
                    self._lost_reservations.add(reservation_key)
                    dispatch_context.cancellation.cancel(
                        "work claim renewal was rejected"
                    )
                try:
                    self._record_claim_lost(
                        worker_id=worker_id,
                        delivery=delivery,
                        claims=claims,
                        reason="renewal_fence_rejected",
                    )
                except Exception:
                    pass

    def _record_renewal_failure(
        self,
        *,
        worker_id: str,
        delivery: Delivery,
        error: Exception,
        will_retry: bool,
    ) -> None:
        try:
            self._append(
                delivery.event,
                "runtime.delivery.claim.renewal.failed",
                {
                    "agent_id": worker_id,
                    "delivery_id": delivery.delivery_id,
                    "reason": f"{type(error).__name__}: {error}",
                    "will_retry": will_retry,
                },
                key=(
                    f"claim-renewal-failed:{worker_id}:{delivery.delivery_id}:"
                    f"{type(error).__name__}:{will_retry}"
                ),
            )
        except Exception:
            pass

    def _claim_keys(
        self,
        worker_id: str,
        mailbox: DurableMailbox,
        delivery: Delivery,
        *,
        worker_slot: int | None = None,
    ) -> tuple[str, ...]:
        payload = delivery.event.payload
        # Supervisor patches one shared plan, so every coordinator delivery for
        # the same Run must contend on one durable single-flight claim.
        agent_run_id = (
            f"supervisor:{delivery.event.run_id}"
            if worker_id == "coordinator"
            else payload.get("agent_run_id")
        )
        if not agent_run_id and delivery.event.type == "a2a.peer.requested":
            correlation_id = payload.get("correlation_id", delivery.event.id)
            agent_run_id = f"peer:{delivery.event.run_id}:{correlation_id}"
        if not agent_run_id:
            assignment_id = payload.get("assignment_id")
            agent_run_id = (
                f"assignment:{delivery.event.run_id}:{assignment_id}"
                if assignment_id
                else f"task:{delivery.event.run_id}:{delivery.event.task_id}:{worker_id}"
            )
        keys = (
            f"delivery:{mailbox.mailbox_id}:{delivery.delivery_id}",
            f"agent-run:{agent_run_id}",
        )
        if worker_slot is None:
            return keys
        return (*keys, f"worker-slot:{worker_id}:{worker_slot}")

    def _worker_in_flight_locked(self, worker_id: str) -> int:
        return sum(key[0] == worker_id for key in self._in_flight)

    def _pending_count_locked(self) -> int:
        return len(self._dispatchable_pending_locked())

    def _dispatchable_pending_locked(
        self,
    ) -> list[tuple[str, DurableMailbox, Delivery]]:
        claimed_deliveries = self._active_delivery_claim_keys()
        return [
            (worker_id, mailbox, delivery)
            for worker_id, (mailbox, _, _) in self._workers.items()
            for delivery in mailbox.pending()
            if (worker_id, delivery.delivery_id) not in self._in_flight
            and f"delivery:{mailbox.mailbox_id}:{delivery.delivery_id}"
            not in claimed_deliveries
            and not self._delivery_deferred_by_run_control(delivery)
        ]

    def _delivery_deferred_by_run_control(self, delivery: Delivery) -> bool:
        run = self.store.projection("run", delivery.event.run_id)
        return bool(run and run.get("status") in {"pausing", "paused"})

    def _active_delivery_claim_keys(self) -> set[str]:
        current = datetime.now(timezone.utc)
        return {
            str(claim["claim_key"])
            for claim in self.store.list_work_claims(state="active")
            if str(claim["claim_key"]).startswith("delivery:")
            and datetime.fromisoformat(str(claim["expires_at"])) > current
        }

    def _backpressure_locked(
        self,
    ) -> tuple[Event, str, dict[str, int]] | None:
        pending = self._dispatchable_pending_locked()
        queued = len(pending)
        if queued == 0:
            self._backpressure_signature = None
            return None
        active_keys = tuple(
            sorted(f"{worker}:{delivery}" for worker, delivery in self._in_flight)
        )
        signature: tuple[object, ...] = (queued, active_keys)
        if signature == self._backpressure_signature:
            return None
        identity = pending[0][2].event
        self._backpressure_signature = signature
        key = f"backpressure:{identity.id}:{':'.join(active_keys) or 'claim-conflict'}"
        return (
            identity,
            key,
            {
                "active": len(self._in_flight),
                "capacity": self._max_workers,
                "queued": queued,
            },
        )

    def _release_reservation_locked(self, worker_id: str, delivery: Delivery) -> None:
        reservation_key = (worker_id, delivery.delivery_id)
        self._in_flight.pop(reservation_key, None)
        self._claim_deadlines.pop(reservation_key, None)
        self._finalizing.discard(reservation_key)
        self._lost_reservations.discard(reservation_key)
        self._completed_steps += 1
        self._backpressure_signature = None
        self._wake_generation += 1
        self._renewer_wake.set()
        self._condition.notify_all()

    def _handle_unexpected_failure(
        self,
        *,
        worker_id: str,
        delivery: Delivery,
        error: Exception,
    ) -> bool:
        attempts = 1 + sum(
            event.type == "runtime.agent.crashed"
            and event.payload.get("delivery_id") == delivery.delivery_id
            and event.payload.get("failure_class") == "unexpected_exception"
            for event in self.store.read_all()
        )
        reason = f"{type(error).__name__}: {error}"
        crashed = self._append(
            delivery.event,
            "runtime.agent.crashed",
            {
                "agent_id": worker_id,
                "delivery_id": delivery.delivery_id,
                "reason": reason,
                "failure_class": "unexpected_exception",
                "attempt": attempts,
                "redelivery": attempts < self.MAX_DELIVERY_FAILURES,
            },
            key=f"unexpected-crash:{delivery.delivery_id}:{attempts}",
        )
        if attempts < self.MAX_DELIVERY_FAILURES:
            return False
        dead_letter = self._append(
            delivery.event,
            "mailbox.delivery.dead_lettered",
            {
                "mailbox_id": worker_id,
                "agent_id": worker_id,
                "delivery_id": delivery.delivery_id,
                "delivery_event_id": delivery.event.id,
                "attempts": attempts,
                "reason": reason,
            },
            key=f"dead-letter:{delivery.delivery_id}",
            causation_id=crashed.id,
        )
        self._append(
            delivery.event,
            "runtime.agent.degraded",
            {
                "agent_id": worker_id,
                "delivery_id": delivery.delivery_id,
                "reason": "delivery_dead_lettered",
            },
            key=f"dead-letter-degraded:{delivery.delivery_id}",
            causation_id=crashed.id,
        )
        self._fail_running_run_for_dead_letter(delivery, dead_letter)
        return True

    def _dead_letter(self, delivery_id: str) -> Event | None:
        return next(
            (
                event
                for event in reversed(self.store.read_all())
                if event.type == "mailbox.delivery.dead_lettered"
                and event.payload.get("delivery_id") == delivery_id
            ),
            None,
        )

    def _fail_running_run_for_dead_letter(
        self, delivery: Delivery, dead_letter: Event
    ) -> Event | None:
        run = self.store.projection("run", delivery.event.run_id)
        if run is None or run.get("status") != "running":
            return None
        root_task_id = str(run.get("task_id") or delivery.event.task_id)
        snapshot = self.store.snapshot(run_id=delivery.event.run_id)
        active_leases = [
            lease for lease in snapshot["leases"] if lease.get("status") == "active"
        ]
        for lease in active_leases:
            assignment_id = str(lease["assignment_id"])
            assignment = self.store.projection("assignment", assignment_id)
            if assignment is not None and assignment.get("status") not in {
                "succeeded",
                "completed",
                "failed",
                "expired",
            }:
                self._append(
                    delivery.event,
                    "assignment.failed",
                    {
                        "assignment_id": assignment_id,
                        "agent_id": lease.get("agent_id"),
                        "reason": "delivery_dead_lettered",
                        "dead_letter_event_id": dead_letter.id,
                    },
                    key=(
                        f"dead-letter-assignment-failed:"
                        f"{delivery.delivery_id}:{assignment_id}"
                    ),
                    causation_id=dead_letter.id,
                    task_id=root_task_id,
                )
            self._append(
                delivery.event,
                "assignment.lease.released",
                {
                    "lease_id": lease.get("lease_id", f"lease:{assignment_id}"),
                    "assignment_id": assignment_id,
                    "stage_id": lease.get("stage_id"),
                    "agent_id": lease.get("agent_id"),
                    "reason": "run_failed_after_delivery_dead_letter",
                    "dead_letter_event_id": dead_letter.id,
                },
                key=(
                    f"dead-letter-lease-released:{delivery.delivery_id}:{assignment_id}"
                ),
                causation_id=dead_letter.id,
                task_id=root_task_id,
            )
        return self._append(
            delivery.event,
            "run.failed",
            {
                "reason": "delivery_dead_lettered",
                "failure": str(dead_letter.payload.get("reason", "unknown failure")),
                "delivery_id": delivery.delivery_id,
                "dead_letter_event_id": dead_letter.id,
            },
            key=f"dead-letter-run-failed:{delivery.delivery_id}",
            causation_id=dead_letter.id,
            task_id=root_task_id,
        )

    def _append(
        self,
        identity: Event,
        event_type: str,
        payload: dict,
        *,
        key: str | None = None,
        causation_id: str | None = None,
        task_id: str | None = None,
    ) -> Event:
        return self.store.append(
            Event(
                id=(
                    str(
                        uuid5(NAMESPACE_URL, f"crazy:scheduler:{identity.run_id}:{key}")
                    )
                    if key is not None
                    else str(uuid4())
                ),
                run_id=identity.run_id,
                task_id=task_id or identity.task_id,
                type=event_type,
                source="runtime.scheduler",
                payload=payload,
                causation_id=causation_id or identity.id,
            )
        )


class ResidentRuntime:
    """Cohesive resident runtime used by the API, Control Room, and learning tests."""

    SCHEDULER_RECOVERY_DELAY_SECONDS = 0.05
    EXTERNAL_WAKE_FALLBACK_SECONDS = 1.0
    SCHEDULER_FAILURE_BUFFER_LIMIT = 100

    AGENTS = (
        (
            "coordinator",
            "Coordinator / 总控",
            ["orchestration.plan", "completion.gate"],
        ),
        (
            "scout",
            "Scout / 侦察",
            ["evidence.collect", "repo.inspect", "peer.respond"],
        ),
        (
            "scout-backup",
            "Scout Backup / 侦察备用",
            ["evidence.collect", "repo.inspect", "peer.respond"],
        ),
        (
            "builder",
            "Builder / 构建",
            ["artifact.compose", "repo.edit", "test.verify", "peer.request"],
        ),
        (
            "reviewer",
            "Reviewer / 审查",
            [
                "artifact.review",
                "evidence.verify",
                "repo.review",
                "test.verify",
                "peer.respond",
            ],
        ),
        (
            "generalist",
            "Generalist / 通用执行",
            [
                "repo.inspect",
                "repo.edit",
                "test.verify",
                "research.browse",
                "research.cite",
            ],
        ),
    )

    def __init__(
        self,
        data_dir: Path,
        *,
        model_factory: ModelFactory | None = None,
        team_model_factory: TeamModelFactory | None = None,
        supervisor_policy: SupervisorPolicy | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteEventStore(self.data_dir / "control_plane.db")
        self.eval_service = PairedEvalService(self.store)
        self.campaign_service = EvalCampaignService(self.store)
        self.engineering_loop_service = EngineeringLoopService(self.store)
        self.model_call_authority = PersistentModelCallAuthority(self.store)
        self.artifacts = ArtifactStore(self.data_dir / "artifacts")
        self.checkpoint_snapshots = WorkspaceSnapshotStore(
            self.data_dir / "checkpoint_objects"
        )
        self.checkpoints = CheckpointService(
            self.store,
            self.checkpoint_snapshots,
            artifact_root=self.artifacts.root,
        )
        self.faults = FaultController()
        self.kernel = ControlKernel(self.store, fault_controller=self.faults)
        self.context = PersistentContextCompiler(self.store, self.artifacts)
        self.scheduler = ResidentScheduler(self.store, self.faults)
        self.team_pack = ResidentDemoTeamTaskPack()
        self.team_contract = self.team_pack.team_contract()
        self.repo_maintainer_team_pack = RepoMaintainerTeamTaskPack(self.data_dir)
        self.team_task_packs: dict[str, ResidentDemoTeamTaskPack] = {
            self.team_pack.task_pack_id: self.team_pack,
            self.repo_maintainer_team_pack.task_pack_id: self.repo_maintainer_team_pack,
        }
        self.supervisor_policy = supervisor_policy or CapabilitySupervisorPolicy()
        self.repo_maintainer_pack = RepoMaintainerTaskPack(self.data_dir)
        self.evidence_research_pack = EvidenceResearchTaskPack(self.data_dir)
        self.repo_quality_pack = RepoQualityTaskPack(self.data_dir)
        self.task_packs: dict[str, TaskPack] = {
            self.repo_maintainer_pack.task_pack_id: self.repo_maintainer_pack,
            self.evidence_research_pack.task_pack_id: self.evidence_research_pack,
            self.repo_quality_pack.task_pack_id: self.repo_quality_pack,
        }
        self.repo_quality_loop_pack = RepoQualityLoopPack(
            self.data_dir,
            task_pack=self.repo_quality_pack,
            snapshots=self.checkpoint_snapshots,
            artifacts=self.artifacts,
        )
        self.engineering_loop_packs: dict[str, LoopPack] = {
            self.repo_quality_loop_pack.loop_pack_id: self.repo_quality_loop_pack,
        }
        self._model_factory = model_factory
        self._single_models: dict[str, ModelProvider] = {}
        self._single_sessions: dict[str, AgentRunSession] = {}
        self.mailboxes: dict[str, DurableMailbox] = {
            worker_id: DurableMailbox(worker_id, self.store)
            for worker_id in [
                *(agent[0] for agent in self.AGENTS),
                "dream.worker",
                "context.evolver",
            ]
        }
        self.team_workers = TeamWorkerEngine(
            data_dir=self.data_dir,
            store=self.store,
            artifacts=self.artifacts,
            kernel=self.kernel,
            task_pack=self.team_pack,
            deliver=lambda receiver, event, delivery_id: self._deliver(
                receiver, event, delivery_id=delivery_id
            ),
            route_decision=self._route_decision,
            begin_leased_step=lambda event, agent_id: self._begin_leased_step(
                event, agent_id=agent_id
            ),
            fail_run=self._fail_team_run_from_model,
            model_factory=team_model_factory,
            task_pack_resolver=self.team_task_pack_for,
            model_call_authority=self.model_call_authority,
            fault_injector=self.faults.trip,
        )
        self._register_agents()
        self.scheduler.register(
            "coordinator", self.mailboxes["coordinator"], self._supervisor_step
        )
        for agent_id in ("scout", "scout-backup", "builder", "reviewer"):
            self.scheduler.register(
                agent_id,
                self.mailboxes[agent_id],
                lambda delivery, worker_id=agent_id: self.team_workers.handle(
                    delivery, agent_id=worker_id
                ),
            )
        self.scheduler.register(
            "generalist", self.mailboxes["generalist"], self._single_agent_step
        )
        self.scheduler.register(
            "dream.worker", self.mailboxes["dream.worker"], self._dream_step
        )
        self.scheduler.register(
            "context.evolver", self.mailboxes["context.evolver"], self._evolver_step
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scheduler_failure_buffer: deque[Event] = deque(
            maxlen=self.SCHEDULER_FAILURE_BUFFER_LIMIT
        )
        self._route_cursor = 0
        self._route_lock = threading.RLock()
        self._capacity_waiters: dict[str, Event] = {}
        self.eval_service.recover_pending(
            resume=lambda request: self.create_paired_eval(
                request,
                recovering=True,
            )
        )
        self._reconcile_routes()
        self._reconcile_failed_runs()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.scheduler.resume()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name="crazy-resident-runtime", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.scheduler.pause()
        self._stop.set()
        self.scheduler.signal()
        if self._thread is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                raise RuntimeError(
                    "resident scheduler thread did not stop within 3 seconds"
                )
        self.scheduler.shutdown(wait=True)
        self._thread = None

    def _serve(self) -> None:
        while not self._stop.is_set():
            stage = "scheduler_failure_flush"
            try:
                self._flush_scheduler_cycle_failures()
                stage = "route_reconciliation"
                self._reconcile_routes()
                stage = "failed_run_reconciliation"
                self._reconcile_failed_runs()
                stage = "cancellation_reconciliation"
                self._reconcile_cancellations()
                stage = "pause_reconciliation"
                self._reconcile_pauses()
                stage = "resume_reconciliation"
                self._reconcile_resumes()
                stage = "nudge_reconciliation"
                self._reconcile_agent_nudges()
                stage = "eval_control"
                if self._advance_eval_control():
                    continue
                stage = "engineering_loop_control"
                if self._advance_engineering_control():
                    continue
                stage = "lease_expiry"
                if self.expire_due_leases():
                    continue
                stage = "delivery_selection"
                if self.scheduler.dispatch_available():
                    continue
                stage = "deadline_wait"
                self.scheduler.wait(self._seconds_until_next_wake())
            except InjectedKernelCrash:
                # Chaos Lab 依赖该异常模拟进程中断；不能降级成普通可恢复错误。
                raise
            except Exception as exc:
                self._record_scheduler_cycle_failure(stage=stage, error=exc)
                self._stop.wait(self.SCHEDULER_RECOVERY_DELAY_SECONDS)

    def _record_scheduler_cycle_failure(self, *, stage: str, error: Exception) -> Event:
        event = Event(
            run_id="control-plane",
            task_id="resident-scheduler",
            type="runtime.scheduler.cycle.failed",
            source="runtime.scheduler",
            payload={
                "stage": stage,
                "reason": f"{type(error).__name__}: {error}",
                "recovering": True,
            },
        )
        try:
            return self.store.append(event)
        except Exception:
            # EventStore 自身短暂不可用时，诊断事实先进入有界内存缓冲；
            # 持久邮箱仍是真相源，线程不能因为“记录故障失败”而退出。
            self._scheduler_failure_buffer.append(event)
            return event

    def _flush_scheduler_cycle_failures(self) -> None:
        while self._scheduler_failure_buffer:
            event = self._scheduler_failure_buffer[0]
            self.store.append(event)
            self._scheduler_failure_buffer.popleft()

    def create_checkpoint(
        self,
        run_id: str,
        request: CheckpointCreateRequest,
    ) -> CheckpointContract:
        """Capture one verified, quiescent Run boundary as a composite checkpoint."""

        identity = next(
            (
                event
                for event in self.store.read_all(run_id=run_id)
                if event.type == "run.created"
            ),
            None,
        )
        if identity is None:
            raise KeyError(f"unknown run: {run_id}")
        barrier_id = f"checkpoint_barrier_{uuid4().hex}"
        checkpoint_id = self.checkpoints.checkpoint_id(run_id, request.request_id)
        acquired = self.store.append(
            Event(
                run_id=identity.run_id,
                task_id=identity.task_id,
                type="checkpoint.barrier.acquired",
                source="runtime.checkpoint",
                payload={
                    "barrier_id": barrier_id,
                    "checkpoint_id": checkpoint_id,
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(minutes=5)
                    ).isoformat(),
                },
                causation_id=identity.id,
            )
        )
        try:
            self._wait_for_checkpoint_quiescence(run_id, timeout_seconds=30)
            return self.checkpoints.create(run_id, request)
        finally:
            self.store.append(
                Event(
                    run_id=identity.run_id,
                    task_id=identity.task_id,
                    type="checkpoint.barrier.released",
                    source="runtime.checkpoint",
                    payload={
                        "barrier_id": barrier_id,
                        "checkpoint_id": checkpoint_id,
                    },
                    causation_id=acquired.id,
                )
            )

    def _wait_for_checkpoint_quiescence(
        self,
        run_id: str,
        *,
        timeout_seconds: float,
    ) -> None:
        deadline = monotonic() + timeout_seconds
        while True:
            now = datetime.now(timezone.utc)
            active = [
                claim
                for claim in self.store.list_work_claims(state="active")
                if str(claim["claim_key"]).startswith("agent-run:")
                and run_id in str(claim["claim_key"])
                and datetime.fromisoformat(str(claim["expires_at"])) > now
            ]
            if not active:
                return
            if monotonic() >= deadline:
                raise TimeoutError(
                    f"run did not reach checkpoint quiescence: {run_id}"
                )
            sleep(0.02)

    def restore_checkpoint(
        self,
        checkpoint_id: str,
        request: CheckpointRestoreRequest,
    ) -> CheckpointRestored:
        """Fork a new single-Agent Run from verified checkpoint facts."""

        contract = self.checkpoints.validate_restore(checkpoint_id)
        source_events = self.store.read_all(run_id=contract.source.run_id)
        source_created = next(
            (event for event in source_events if event.type == "run.created"),
            None,
        )
        source_assignment = next(
            (event for event in source_events if event.id == contract.state_refs.assignment_event_id),
            None,
        )
        if source_created is None or source_assignment is None:
            raise RuntimeError("checkpoint has no restorable Run or Assignment")
        if source_created.payload.get("execution_mode") != "single":
            raise ValueError("checkpoint MVP restores single-Agent Runs only")
        if contract.task_pack != self.repo_maintainer_pack.task_pack_id:
            raise ValueError("checkpoint MVP restores repo-maintainer Runs only")

        restore_key = f"{checkpoint_id}:{request.request_id}"
        identity = self.checkpoint_restore_identity(checkpoint_id, request.request_id)
        committed = next(
            (
                event
                for event in reversed(self.store.read_all(run_id=identity.run_id))
                if event.type == "checkpoint.restore.committed"
                and event.payload.get("checkpoint_id") == checkpoint_id
                and event.payload.get("request_id") == request.request_id
            ),
            None,
        )
        if committed is not None:
            return CheckpointRestored(
                checkpoint_id=checkpoint_id,
                source_run_id=contract.source.run_id,
                run_id=identity.run_id,
                task_id=identity.task_id,
            )
        workspace = self.data_dir / "workspaces" / identity.run_id
        if workspace.exists():
            restored_snapshot = self.checkpoint_snapshots.create(workspace)
            if restored_snapshot.object_id != contract.workspace.object_id:
                raise RuntimeError("existing restore workspace does not match checkpoint")
        else:
            self.checkpoint_snapshots.restore(contract.workspace, workspace)
        self.faults.trip("after_restore_workspace")

        task_request = TaskRequest(
            title=f"{source_created.payload['title']} (checkpoint restore)",
            brief=str(source_created.payload["brief"]),
            model_mode=str(source_created.payload["model_mode"]),
            execution_mode="single",
            task_pack=contract.task_pack,
            model_budget=ModelBudgetConfig.model_validate(
                source_created.payload.get("model_budget", {})
            ),
        )
        assignment_contract = AssignmentContract.model_validate(
            source_assignment.payload["contract"]
        )
        self._prepare_single_task(
            task_request,
            identity,
            restore_contract=contract,
            assignment_contract=assignment_contract,
            source_run_created=source_created,
        )
        restored_created = next(
            event
            for event in self.store.read_all(run_id=identity.run_id)
            if event.type == "run.created"
        )
        self._append_deterministic(
            restored_created,
            f"checkpoint-restore-committed:{restore_key}",
            "checkpoint.restore.committed",
            {
                "request_id": request.request_id,
                "checkpoint_id": checkpoint_id,
                "source_run_id": contract.source.run_id,
                "source_event_id": contract.source.event_id,
                "source_turn_id": contract.source.turn_id,
                "source_phase": contract.source.phase,
                "workspace_object_id": contract.workspace.object_id,
                "state_refs": contract.state_refs.model_dump(mode="json"),
                "effects": contract.effects.model_dump(mode="json"),
                "context_policy": contract.context_policy,
                "planning_directive": "Replan from verified checkpoint facts; do not assume hidden prior reasoning.",
                "full_model_context_copied": False,
            },
            source="runtime.checkpoint",
        )
        self.faults.trip("after_restore_committed")
        self._release_prepared_task("single", identity)
        return CheckpointRestored(
            checkpoint_id=checkpoint_id,
            source_run_id=contract.source.run_id,
            run_id=identity.run_id,
            task_id=identity.task_id,
        )

    def fork_agent_run(
        self,
        run_id: str,
        request: RunForkRequest,
    ) -> CheckpointRestored:
        """Fork a verified single-Agent branch without copying hidden context."""

        created = next(
            (
                event
                for event in self.store.read_all(run_id=run_id)
                if event.type == "run.created"
            ),
            None,
        )
        if created is None:
            raise KeyError(f"unknown run: {run_id}")
        if created.payload.get("execution_mode") != "single":
            raise ValueError(f"AgentRun fork currently requires single mode: {run_id}")
        if created.payload.get("task_pack") != self.repo_maintainer_pack.task_pack_id:
            raise ValueError("AgentRun fork currently supports repo-maintainer only")
        checkpoint = self.create_checkpoint(
            run_id,
            CheckpointCreateRequest(
                request_id=f"{request.request_id}:checkpoint",
                label=request.label,
            ),
        )
        return self.restore_checkpoint(
            checkpoint.checkpoint_id,
            CheckpointRestoreRequest(request_id=f"{request.request_id}:restore"),
        )

    def agent_run_branch(self, run_id: str) -> AgentRunBranchView:
        """Rebuild one branch node from immutable checkpoint restore facts."""

        if self.store.projection("run", run_id) is None:
            raise KeyError(f"unknown run: {run_id}")
        relations = self.store.read_all(event_type="checkpoint.restore.committed")
        parent = next(
            (event for event in reversed(relations) if event.run_id == run_id),
            None,
        )
        children = tuple(
            event.run_id
            for event in relations
            if event.payload.get("source_run_id") == run_id
        )
        return AgentRunBranchView(
            run_id=run_id,
            parent_run_id=(
                str(parent.payload["source_run_id"]) if parent is not None else None
            ),
            checkpoint_id=(
                str(parent.payload["checkpoint_id"]) if parent is not None else None
            ),
            source_event_id=(
                str(parent.payload["source_event_id"]) if parent is not None else None
            ),
            source_turn_id=(
                str(parent.payload["source_turn_id"])
                if parent is not None and parent.payload.get("source_turn_id")
                else None
            ),
            source_phase=(
                str(parent.payload["source_phase"]) if parent is not None else None
            ),
            children_run_ids=children,
        )

    @staticmethod
    def checkpoint_restore_identity(
        checkpoint_id: str,
        request_id: str,
    ) -> EvalRunIdentity:
        """Derive the stable fork identity used by restore retries."""

        restore_key = f"{checkpoint_id}:{request_id}"
        return EvalRunIdentity(
            run_id=f"run_{uuid5(NAMESPACE_URL, f'crazy:checkpoint-restore-run:{restore_key}').hex[:12]}",
            task_id=f"task_{uuid5(NAMESPACE_URL, f'crazy:checkpoint-restore-task:{restore_key}').hex[:12]}",
        )

    def submit_task(self, request: TaskRequest) -> RunCreated:
        if request.execution_mode == "single":
            return self._submit_single_task(request)
        task_pack_id = request.task_pack or self.team_pack.task_pack_id
        if task_pack_id not in self.team_task_packs:
            raise ValueError(f"unsupported Team task pack: {task_pack_id}")
        return self._submit_team_task(request)

    def pause_agent_run(
        self,
        run_id: str,
        request: RunPauseRequest,
    ) -> RunControlResult:
        created = self._controllable_single_run(run_id)
        request_event = self._append_deterministic(
            created,
            f"agent-run-pause-request:{run_id}:{request.request_id}",
            "run.pause.requested",
            request.model_dump(mode="json"),
            source="runtime.agent-control",
        )
        self._reconcile_pause(run_id)
        paused_event = next(
            (
                event
                for event in reversed(self.store.read_all(run_id=run_id))
                if event.type == "run.paused" and event.causation_id == request_event.id
            ),
            None,
        )
        self.scheduler.signal()
        return RunControlResult(
            run_id=run_id,
            request_id=request.request_id,
            action="pause",
            status="paused" if paused_event is not None else "pausing",
            request_event_id=request_event.id,
            applied_event_id=paused_event.id if paused_event is not None else None,
        )

    def resume_agent_run(
        self,
        run_id: str,
        request: RunResumeRequest,
    ) -> RunControlResult:
        self._controllable_single_run(run_id)
        events = self.store.read_all(run_id=run_id)
        existing = next(
            (
                event
                for event in events
                if event.type == "run.resume.requested"
                and event.payload.get("request_id") == request.request_id
            ),
            None,
        )
        if existing is not None:
            expected = request.model_dump(mode="json")
            if any(existing.payload.get(key) != value for key, value in expected.items()):
                raise ValueError(
                    f"resume request_id belongs to another payload: {request.request_id}"
                )
            request_event = existing
        else:
            pause_request = self._active_operator_pause_request(events)
            if pause_request is None:
                raise ValueError(f"run has no active operator pause: {run_id}")
            request_event = self._append_deterministic(
                pause_request,
                f"agent-run-resume-request:{run_id}:{request.request_id}",
                "run.resume.requested",
                request.model_dump(mode="json"),
                source="runtime.agent-control",
                causation_id=pause_request.id,
            )
        self._reconcile_resume(run_id)
        resumed_event = next(
            event
            for event in reversed(self.store.read_all(run_id=run_id))
            if event.type == "run.resumed" and event.causation_id == request_event.id
        )
        self.scheduler.signal()
        self._reconcile_agent_nudges(run_id)
        return RunControlResult(
            run_id=run_id,
            request_id=request.request_id,
            action="resume",
            status="running",
            request_event_id=request_event.id,
            applied_event_id=resumed_event.id,
        )

    def nudge_agent_run(
        self,
        run_id: str,
        request: RunNudgeRequest,
    ) -> RunNudgeResult:
        created = self._controllable_single_run(run_id)
        events = self.store.read_all(run_id=run_id)
        existing = next(
            (
                event
                for event in events
                if event.type == "agent.nudge.set"
                and event.payload.get("request_id") == request.request_id
            ),
            None,
        )
        if existing is None:
            previous = next(
                (event for event in reversed(events) if event.type == "agent.nudge.set"),
                None,
            )
            nudge_event = self._append_deterministic(
                created,
                f"agent-run-nudge:{run_id}:{request.request_id}",
                "agent.nudge.set",
                {
                    **request.model_dump(mode="json"),
                    "agent_id": "generalist",
                    "slot_policy": "latest_only",
                    "supersedes_event_id": previous.id if previous is not None else None,
                },
                source="runtime.agent-control",
            )
        else:
            nudge_event = existing
            expected = request.model_dump(mode="json")
            if any(nudge_event.payload.get(key) != value for key, value in expected.items()):
                raise ValueError(
                    f"nudge request_id belongs to another payload: {request.request_id}"
                )
        self._reconcile_agent_nudges(run_id)
        return RunNudgeResult(
            run_id=run_id,
            request_id=request.request_id,
            nudge_event_id=nudge_event.id,
            supersedes_event_id=nudge_event.payload.get("supersedes_event_id"),
        )

    def agent_run_view(self, run_id: str) -> AgentRunSessionView:
        """Project one single-Agent Run from durable events for UI and recovery."""

        events = self.store.read_all(run_id=run_id)
        created = next((event for event in events if event.type == "run.created"), None)
        if created is None:
            raise KeyError(f"unknown run: {run_id}")
        if created.payload.get("execution_mode") != "single":
            raise ValueError(f"run is not a single-Agent Run: {run_id}")
        task_pack_id = str(
            created.payload.get("task_pack", self.repo_maintainer_pack.task_pack_id)
        )
        pack = self.task_packs.get(task_pack_id)
        if pack is None:
            raise RuntimeError(f"unsupported task pack: {task_pack_id}")
        view = AgentRunSession.project_view(
            AgentRunSessionIdentity(
                run_id=created.run_id,
                task_id=created.task_id,
                agent_id=pack.agent_id,
                kind=AgentRunKind.SINGLE,
            ),
            events,
        )
        if not view.fork_supported:
            return view
        blocker = (
            "active Agent turn has not reached a checkpoint boundary"
            if self._run_has_active_agent_claim(run_id)
            else self.checkpoints.fork_blocker(run_id)
        )
        return replace(
            view,
            fork_ready=blocker is None,
            fork_blocker=(
                f"当前不是可派生的安全边界 / {blocker}"
                if blocker is not None
                else None
            ),
        )

    def _controllable_single_run(self, run_id: str) -> Event:
        events = self.store.read_all(run_id=run_id)
        created = next((event for event in events if event.type == "run.created"), None)
        if created is None:
            raise KeyError(f"unknown run: {run_id}")
        if created.payload.get("execution_mode") != "single":
            raise ValueError(f"AgentRun controls currently require single mode: {run_id}")
        projection = self.store.projection("run", run_id) or {}
        if projection.get("status") in {"succeeded", "failed", "cancelled", "cancelling"}:
            raise ValueError(f"terminal run cannot be controlled: {run_id}")
        return created

    def _reconcile_pauses(self) -> bool:
        changed = False
        for run in self.store.snapshot().get("runs", []):
            if run.get("status") == "pausing":
                changed = self._reconcile_pause(str(run["run_id"])) or changed
        return changed

    def _reconcile_resumes(self) -> bool:
        changed = False
        for run in self.store.snapshot().get("runs", []):
            if run.get("status") in {"succeeded", "failed", "cancelled", "cancelling"}:
                continue
            changed = self._reconcile_resume(str(run["run_id"])) or changed
        return changed

    def _reconcile_resume(self, run_id: str) -> bool:
        events = self.store.read_all(run_id=run_id)
        request_event = next(
            (
                event
                for event in reversed(events)
                if event.type == "run.resume.requested"
                and not any(
                    later.type == "run.resumed" and later.causation_id == event.id
                    for later in events
                )
            ),
            None,
        )
        if request_event is None:
            return False
        request_index = events.index(request_event)
        if any(
            event.type == "run.pause.requested"
            for event in events[request_index + 1 :]
        ):
            return False
        pause_request = self._active_operator_pause_request(events[:request_index])
        if pause_request is None:
            raise ValueError(f"resume request has no active operator pause: {run_id}")
        self._append_deterministic(
            request_event,
            f"agent-run-resumed:{run_id}:{request_event.payload['request_id']}",
            "run.resumed",
            {
                "request_id": request_event.payload["request_id"],
                "reason": request_event.payload.get("reason"),
                "pause_request_id": pause_request.payload.get("request_id"),
            },
            source="runtime.agent-control",
            causation_id=request_event.id,
        )
        return True

    @staticmethod
    def _active_operator_pause_request(events: list[Event]) -> Event | None:
        last_resume_index = next(
            (
                index
                for index in range(len(events) - 1, -1, -1)
                if events[index].type == "run.resumed"
            ),
            -1,
        )
        return next(
            (
                events[index]
                for index in range(len(events) - 1, last_resume_index, -1)
                if events[index].type == "run.pause.requested"
                and events[index].source == "runtime.agent-control"
            ),
            None,
        )

    def _reconcile_pause(self, run_id: str) -> bool:
        projection = self.store.projection("run", run_id) or {}
        if projection.get("status") != "pausing":
            return False
        now = datetime.now(timezone.utc)
        if self._run_has_active_agent_claim(run_id, now=now):
            return False
        request_event = next(
            (
                event
                for event in reversed(self.store.read_all(run_id=run_id))
                if event.type == "run.pause.requested"
            ),
            None,
        )
        if request_event is None:
            return False
        self._append_deterministic(
            request_event,
            f"agent-run-paused:{run_id}:{request_event.id}",
            "run.paused",
            {
                "request_id": request_event.payload.get("request_id"),
                "reason": request_event.payload.get("reason"),
                "boundary": "after_active_turn",
            },
            source="runtime.agent-control",
            causation_id=request_event.id,
        )
        return True

    def _reconcile_agent_nudges(self, run_id: str | None = None) -> bool:
        changed = False
        runs = self.store.snapshot().get("runs", [])
        for run in runs:
            current_run_id = str(run["run_id"])
            if run_id is not None and current_run_id != run_id:
                continue
            if run.get("status") != "running":
                continue
            events = self.store.read_all(run_id=current_run_id)
            nudge = next(
                (event for event in reversed(events) if event.type == "agent.nudge.set"),
                None,
            )
            if nudge is None or any(
                event.type == "model.requested"
                and event.payload.get("active_nudge_event_id") == nudge.id
                for event in events
            ):
                continue
            if self._run_has_active_agent_claim(current_run_id):
                continue
            mailbox = self.mailboxes["generalist"]
            if any(
                delivery.event.run_id == current_run_id
                for delivery in mailbox.pending()
            ):
                continue
            self._deliver(
                "generalist",
                nudge,
                delivery_id=f"agent-control:nudge:{nudge.id}",
            )
            changed = True
        return changed

    def _run_has_active_agent_claim(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        return any(
            claim["state"] == "active"
            and str(claim["claim_key"]).startswith("agent-run:")
            and run_id in str(claim["claim_key"])
            and datetime.fromisoformat(str(claim["expires_at"])) > current
            for claim in self.store.list_work_claims()
        )

    def create_paired_eval(
        self,
        request: PairedEvalRequest,
        *,
        recovering: bool = False,
        release_arms: bool | None = None,
    ) -> PairedEvalCreated:
        if release_arms is False and request.release_policy == "immediate":
            request = request.model_copy(update={"release_policy": "manual"})
        task_request = TaskRequest(
            title=request.title,
            brief=request.brief,
            model_mode=request.model_mode,
            task_pack=request.task_pack,
            model_budget=request.model_budget,
        )

        def prepare_arm(
            mode: Literal["single", "team"],
            identity: EvalRunIdentity,
        ) -> EvalRunIdentity:
            prepared_request = task_request.model_copy(
                update={"execution_mode": mode}
            )
            if mode == "single":
                self._prepare_single_task(
                    prepared_request,
                    identity,
                    hold_for_paired_commit=True,
                )
            else:
                self._prepare_team_task(prepared_request, identity)
            return identity

        def release_arm(
            mode: Literal["single", "team"],
            identity: EvalRunIdentity,
        ) -> None:
            self._release_prepared_task(mode, identity)

        return self.eval_service.create(
            request,
            prepare_arm=prepare_arm,
            release_arm=release_arm,
            cancel_arm=lambda run_id: self.cancel_run(
                run_id, reason="paired_eval_creation_failed"
            ),
            fail_precommit=not recovering,
            release_arms=release_arms,
        )

    def paired_eval(self, eval_id: str) -> PairedEvalReport:
        return self.eval_service.report(eval_id)

    def release_paired_eval(self, eval_id: str) -> PairedEvalCreated:
        return self.eval_service.release(
            eval_id,
            release_arm=lambda mode, identity: self._release_prepared_task(
                mode, identity
            ),
        )

    def finalize_paired_eval(self, eval_id: str) -> PairedEvalReport:
        return self.eval_service.finalize(eval_id)

    def create_eval_campaign(
        self,
        request: EvalCampaignRequest,
    ) -> EvalCampaignCreated:
        if request.model_mode == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
            campaign_id = self.campaign_service.campaign_id(request.request_id)
            try:
                self.campaign_service.contract(campaign_id)
            except KeyError as exc:
                raise ValueError(
                    "DEEPSEEK_API_KEY is required for deepseek mode"
                ) from exc
        created = self.campaign_service.create(request)
        self.scheduler.signal()
        return created

    def eval_campaign(self, campaign_id: str) -> EvalCampaignReport:
        return self.campaign_service.report(campaign_id)

    def finalize_eval_campaign(self, campaign_id: str) -> EvalCampaignReport:
        return self.campaign_service.finalize(campaign_id)

    def cancel_eval_campaign(self, campaign_id: str) -> EvalCampaignReport:
        def cancel_pair(eval_id: str) -> None:
            for run_id in self.eval_service.prepared_run_ids(eval_id):
                self.cancel_run(
                    run_id,
                    reason=f"parent_campaign_cancelled:{campaign_id}",
                )

        return self.campaign_service.cancel(
            campaign_id,
            cancel_pair=cancel_pair,
        )

    def create_engineering_loop(
        self,
        request: EngineeringLoopRequest,
    ) -> EngineeringLoopCreated:
        """Persist a frozen outer-loop contract; execution remains scheduler-driven."""

        loop_pack = self.engineering_loop_packs.get(request.loop_pack)
        if loop_pack is None:
            raise ValueError(f"unsupported engineering loop pack: {request.loop_pack}")
        if request.worker.execution_mode != "single":
            raise ValueError("repo-quality v1 supports single-Agent child runs only")
        if request.worker.task_pack != loop_pack.task_pack.task_pack_id:
            raise ValueError(
                "engineering worker task_pack must match its registered loop pack"
            )
        if frozenset(request.permissions) != loop_pack.required_permissions:
            raise ValueError(
                "engineering loop permissions must exactly match the LoopPack grant"
            )
        if request.initial_state_ref != "loop-pack://initial":
            if not request.initial_state_ref.startswith("snapshot://"):
                raise ValueError("engineering initial state must be a LoopPack or snapshot ref")
            self.checkpoint_snapshots.verify(
                request.initial_state_ref.removeprefix("snapshot://")
            )
        created = self.engineering_loop_service.create(request)
        self._persist_engineering_loop_pack_authorization(created, loop_pack)
        self.scheduler.signal()
        return created

    def engineering_loop(self, loop_id: str) -> EngineeringLoopReport:
        return self.engineering_loop_service.report(loop_id)

    def engineering_loops(self) -> list[EngineeringLoopReport]:
        return self.engineering_loop_service.list_reports()

    def _advance_engineering_control(self) -> bool:
        """Advance at most one durable parent phase per scheduler cycle."""

        for report in self.engineering_loop_service.list_reports():
            if report.status != "running":
                continue
            loop_pack = self.engineering_loop_packs.get(report.contract.loop_pack)
            if loop_pack is None:
                raise RuntimeError(
                    f"persisted engineering loop uses an unknown pack: "
                    f"{report.contract.loop_pack}"
                )
            self._assert_engineering_loop_pack_authorized(
                report.contract,
                loop_pack,
            )
            return self.engineering_loop_service.advance_one(
                report.loop_id,
                propose=loop_pack.propose,
                launch_child=lambda contract, identity, candidate: (
                    self._launch_engineering_child(
                        loop_pack,
                        contract,
                        identity,
                        candidate,
                    )
                ),
                child_outcome=self._engineering_child_outcome,
                evaluate=loop_pack.evaluate,
            )
        return False

    def _persist_engineering_loop_pack_authorization(
        self,
        created: EngineeringLoopCreated,
        loop_pack: LoopPack,
    ) -> None:
        events = self.store.read_all(run_id=created.loop_id)
        authorizations = [
            event
            for event in events
            if event.type == "engineering.loop_pack.authorized"
        ]
        if not authorizations:
            contract_event = next(
                event for event in events if event.type == "engineering.loop.created"
            )
            self._append_deterministic(
                contract_event,
                f"engineering-loop-pack-authorized:{created.loop_id}",
                "engineering.loop_pack.authorized",
                self._loop_pack_authorization(loop_pack),
                source="runtime.engineering-loop",
            )
        self._assert_engineering_loop_pack_authorized(created.contract, loop_pack)

    def _assert_engineering_loop_pack_authorized(
        self,
        contract: EngineeringLoopContract,
        loop_pack: LoopPack,
    ) -> None:
        authorizations = [
            event
            for event in self.store.read_all(run_id=contract.loop_id)
            if event.type == "engineering.loop_pack.authorized"
        ]
        if len(authorizations) != 1:
            raise RuntimeError(
                "LoopPack authorization is missing or ambiguous for persisted loop: "
                f"{contract.loop_id}"
            )
        current = self._loop_pack_authorization(loop_pack)
        if authorizations[0].payload != current:
            raise RuntimeError(
                "LoopPack authorization drifted since loop creation: "
                f"{contract.loop_id}"
            )
        if (
            contract.loop_pack != current["loop_pack_id"]
            or contract.worker.task_pack != current["task_pack_id"]
            or frozenset(contract.permissions)
            != frozenset(current["required_permissions"])
        ):
            raise RuntimeError(
                "LoopPack authorization no longer matches the persisted contract: "
                f"{contract.loop_id}"
            )

    @staticmethod
    def _loop_pack_authorization(loop_pack: LoopPack) -> dict[str, object]:
        declared_version = getattr(loop_pack, "pack_version", None)
        try:
            implementation_hash = sha256(
                getsource(type(loop_pack)).encode("utf-8")
            ).hexdigest()
        except (OSError, TypeError):
            implementation_hash = None
        if declared_version is None and implementation_hash is None:
            raise RuntimeError(
                "LoopPack authorization requires pack_version or inspectable source"
            )
        return {
            "loop_pack_id": loop_pack.loop_pack_id,
            "implementation_identity": (
                f"{type(loop_pack).__module__}.{type(loop_pack).__qualname__}"
            ),
            "implementation_version": str(
                declared_version or f"source:{implementation_hash}"
            ),
            "implementation_hash": implementation_hash,
            "task_pack_id": loop_pack.task_pack.task_pack_id,
            "required_permissions": sorted(loop_pack.required_permissions),
        }

    def _launch_engineering_child(
        self,
        loop_pack: LoopPack,
        contract: EngineeringLoopContract,
        identity: EngineeringIterationIdentity,
        candidate: CandidateProposal,
    ) -> None:
        """Idempotently prepare and release the canonical child AgentRun."""

        request = TaskRequest(
            title=f"{contract.title} / iteration {identity.iteration}",
            brief=(
                f"Parent objective: {contract.objective}\n"
                f"Candidate intent: {candidate.change_set}\n"
                f"Expected effect: {candidate.expected_effect}"
            ),
            model_mode=contract.worker.model_mode,
            execution_mode="single",
            task_pack=contract.worker.task_pack,
            model_budget=ModelBudgetConfig.model_validate(
                contract.worker.model_budget
            ),
        )
        run_identity = EvalRunIdentity(
            run_id=identity.child_run_id,
            task_id=identity.child_task_id,
        )
        self._prepare_single_task(
            request,
            run_identity,
            assignment_contract=loop_pack.task_pack.assignment_contract(),
            workspace_state_ref=candidate.base_state_ref,
            run_metadata={
                "engineering_loop_id": contract.loop_id,
                "engineering_iteration": identity.iteration,
                "engineering_iteration_id": identity.iteration_id,
                "engineering_candidate_id": candidate.candidate_id,
                "engineering_base_state_ref": candidate.base_state_ref,
            },
        )
        self._release_prepared_task("single", run_identity)

    def _engineering_child_outcome(
        self,
        identity: EngineeringIterationIdentity,
    ) -> ChildRunOutcome | None:
        run = self.store.projection("run", identity.child_run_id)
        if run is None or run.get("status") not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            return None
        events = self.store.read_all(run_id=identity.child_run_id)
        terminal = next(
            event
            for event in reversed(events)
            if event.type in {"run.succeeded", "run.failed", "run.cancelled"}
        )
        status = str(run["status"])
        raw_candidate_state_ref = terminal.payload.get("candidate_state_ref")
        candidate_state_ref: str | None
        if raw_candidate_state_ref is None and status != "succeeded":
            candidate_state_ref = None
        elif isinstance(raw_candidate_state_ref, str) and raw_candidate_state_ref.startswith(
            "snapshot://"
        ):
            candidate_state_ref = raw_candidate_state_ref
            self.checkpoint_snapshots.verify(
                candidate_state_ref.removeprefix("snapshot://")
            )
        else:
            raise RuntimeError("engineering child terminal has no valid candidate snapshot")
        evidence_events = [
            event
            for event in events
            if event.type in {
                "completion.gate.passed",
                "tool.completed",
                "agent.submitted",
                terminal.type,
            }
        ]
        evidence_refs = tuple(f"event://{event.id}" for event in evidence_events)
        if not evidence_refs:
            evidence_refs = (f"event://{terminal.id}",)
        return ChildRunOutcome(
            run_id=identity.child_run_id,
            task_id=identity.child_task_id,
            status=status,
            terminal_event_id=terminal.id,
            candidate_state_ref=candidate_state_ref,
            evidence_refs=evidence_refs,
        )

    def _advance_eval_control(self) -> bool:
        if self.eval_service.finalize_ready():
            return True
        if self.campaign_service.advance_ready(
            create_pair=self._create_campaign_pair,
            pair_contract=self.eval_service.contract,
            release_pair=self.release_paired_eval,
            pair_report=self.paired_eval,
        ):
            return True
        return bool(self.campaign_service.finalize_ready())

    def _create_campaign_pair(
        self,
        request: PairedEvalRequest,
    ) -> PairedEvalCreated:
        eval_id = paired_eval_id(request.request_id)
        recovering = any(
            event.type == "eval.pair.requested"
            for event in self.store.read_all(run_id=eval_id)
        )
        return self.create_paired_eval(
            request,
            recovering=recovering,
            release_arms=False,
        )

    def _requested_model_profile(
        self,
        request: TaskRequest,
        task_pack_id: str,
    ) -> dict[str, object]:
        if request.model_mode == "scripted":
            profile: dict[str, object] = {
                "provider": "FakeModelProvider",
                "model": f"{task_pack_id}-scripted-v1",
                "deterministic": True,
            }
            team_pack = self.team_task_packs.get(task_pack_id)
            manifest_hash = getattr(
                team_pack,
                "scripted_comparison_manifest_hash",
                None,
            )
            if callable(manifest_hash):
                profile.update(
                    {
                        "model": f"{task_pack_id}-script-suite-v1",
                        "comparison_semantics": (
                            "arm_specific_deterministic_scripts"
                        ),
                        "script_manifest_sha256": manifest_hash(),
                    }
                )
            return profile
        return DeepSeekOpenAIProvider(
            max_tokens=request.model_budget.max_output_tokens_per_call,
        ).attestation_profile()

    def cancel_run(self, run_id: str, *, reason: str = "operator_requested") -> dict:
        run = self.store.projection("run", run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        if run.get("status") in {"succeeded", "failed", "cancelled"}:
            return {
                "run_id": run_id,
                "status": str(run["status"]),
                "active_cancelled": 0,
                "queued_cancelled": 0,
            }
        identity = next(
            event
            for event in self.store.read_all(run_id=run_id)
            if event.type == "run.created"
        )
        requested = self._append_deterministic(
            identity,
            f"run-cancel-requested:{run_id}",
            "run.cancel.requested",
            {"reason": reason},
            source="runtime.cancel",
        )
        active_cancelled = self.scheduler.cancel_run(run_id, reason=reason)
        self._cancel_assignments_and_leases(requested, reason=reason)
        queued_cancelled = self._cancel_queued_deliveries(run_id, reason=reason)
        self._finalize_cancelled_run(requested, reason=reason)
        status = str(self.store.projection("run", run_id)["status"])
        return {
            "run_id": run_id,
            "status": status,
            "active_cancelled": active_cancelled,
            "queued_cancelled": queued_cancelled,
        }

    def _fail_team_run_from_model(self, identity: Event, reason: str) -> None:
        run = self.store.projection("run", identity.run_id)
        if run is None or run.get("status") in {"succeeded", "failed", "cancelled"}:
            return
        self._append_deterministic(
            identity,
            f"run-failure-requested:{identity.run_id}:model:{identity.id}",
            "run.failure.requested",
            {
                "reason": reason,
                "failure_class": "terminal_model_call",
                "failure_scope": "run",
            },
            source="runtime.model-governance",
        )
        self.scheduler.signal()

    def _finalize_failed_run(self, requested: Event, *, reason: str) -> bool:
        run = self.store.projection("run", requested.run_id)
        if run is None or run.get("status") in {"succeeded", "cancelled"}:
            return False
        # Failure finalization runs outside the Worker dispatch. Deny new local
        # reservations first, then fence remote claims before writing terminal facts.
        self.scheduler.cancel_run(
            requested.run_id,
            reason=reason,
            record_events=False,
        )
        self._fail_assignments_and_leases(requested, reason=reason)
        self._fail_queued_deliveries(requested.run_id, reason=reason)
        if run.get("status") != "failed":
            self._append_deterministic(
                requested,
                f"run-failed:{requested.run_id}:model",
                "run.failed",
                {
                    "reason": reason,
                    "failure_class": "terminal_model_call",
                    "failure_scope": "run",
                },
                source="runtime.model-governance",
            )
        return True

    def _fail_assignments_and_leases(self, identity: Event, *, reason: str) -> None:
        snapshot = self.store.snapshot(run_id=identity.run_id)
        for assignment in snapshot["assignments"]:
            if assignment.get("status") in {
                "succeeded",
                "completed",
                "failed",
                "expired",
                "cancelled",
            }:
                continue
            assignment_id = str(assignment["assignment_id"])
            self._append_deterministic(
                identity,
                f"model-failed-assignment:{assignment_id}",
                "assignment.failed",
                {
                    "assignment_id": assignment_id,
                    "agent_id": assignment.get("agent_id"),
                    "stage_id": assignment.get("stage_id"),
                    "reason": reason,
                },
                source="runtime.model-governance",
            )
        for lease in snapshot["leases"]:
            if lease.get("status") != "active":
                continue
            assignment_id = str(lease["assignment_id"])
            self._append_deterministic(
                identity,
                f"model-failed-lease-released:{assignment_id}",
                "assignment.lease.released",
                {
                    "lease_id": lease["lease_id"],
                    "assignment_id": assignment_id,
                    "agent_id": lease.get("agent_id"),
                    "stage_id": lease.get("stage_id"),
                    "reason": "run_failed_after_terminal_model_call",
                },
                source="runtime.model-governance",
            )

    def _fail_queued_deliveries(self, run_id: str, *, reason: str) -> int:
        failed = 0
        for worker_id, mailbox in self.mailboxes.items():
            for delivery in mailbox.pending():
                if delivery.event.run_id != run_id:
                    continue
                self.store.fail_work_claim_bundle(
                    f"delivery:{mailbox.mailbox_id}:{delivery.delivery_id}"
                )
                self._append_deterministic(
                    delivery.event,
                    f"failed-run-delivery:{worker_id}:{delivery.delivery_id}",
                    "runtime.delivery.cancelled",
                    {
                        "agent_id": worker_id,
                        "delivery_id": delivery.delivery_id,
                        "reason": reason,
                        "mode": "run_failed",
                    },
                    source="runtime.model-governance",
                )
                try:
                    mailbox.ack(delivery.delivery_id)
                except UnfencedAckError:
                    continue
                failed += 1
        return failed

    def _reconcile_failed_runs(self) -> int:
        """Rebuild the denylist and drain messages that arrive after Run failure."""

        changed = 0
        for run in self.store.snapshot()["runs"]:
            if run.get("status") != "failed":
                continue
            run_id = str(run["run_id"])
            failed = next(
                (
                    event
                    for event in reversed(self.store.read_all(run_id=run_id))
                    if event.type == "run.failed"
                ),
                None,
            )
            reason = (
                str(failed.payload.get("reason", "run failed"))
                if failed is not None
                else "run failed"
            )
            self.scheduler.cancel_run(run_id, reason=reason, record_events=False)
            changed += self._fail_queued_deliveries(run_id, reason=reason)
        return changed

    def _reconcile_cancellations(self) -> int:
        changed = 0
        for run in self.store.snapshot()["runs"]:
            status = run.get("status")
            if status not in {"cancelling", "cancelled"}:
                continue
            run_id = str(run["run_id"])
            requested = next(
                (
                    event
                    for event in reversed(self.store.read_all(run_id=run_id))
                    if event.type == "run.cancel.requested"
                ),
                None,
            )
            reason = (
                str(requested.payload.get("reason", "operator_requested"))
                if requested is not None
                else "operator_requested"
            )
            # Rebuild the process-local dispatch denylist from durable Run state.
            # This also drains messages that arrived after run.cancelled was persisted.
            self.scheduler.cancel_run(run_id, reason=reason)
            identity = requested or next(
                event
                for event in self.store.read_all(run_id=run_id)
                if event.type == "run.created"
            )
            self._cancel_assignments_and_leases(identity, reason=reason)
            changed += self._cancel_queued_deliveries(run_id, reason=reason)
            if status == "cancelling" and requested is not None:
                changed += int(self._finalize_cancelled_run(requested, reason=reason))
        return changed

    def _cancel_assignments_and_leases(self, identity: Event, *, reason: str) -> None:
        snapshot = self.store.snapshot(run_id=identity.run_id)
        for assignment in snapshot["assignments"]:
            if assignment.get("status") in {
                "succeeded",
                "completed",
                "failed",
                "expired",
                "cancelled",
            }:
                continue
            assignment_id = str(assignment["assignment_id"])
            self._append_deterministic(
                identity,
                f"assignment-cancelled:{assignment_id}",
                "assignment.cancelled",
                {
                    "assignment_id": assignment_id,
                    "agent_id": assignment.get("agent_id"),
                    "stage_id": assignment.get("stage_id"),
                    "reason": reason,
                },
                source="runtime.cancel",
            )
        for lease in snapshot["leases"]:
            if lease.get("status") != "active":
                continue
            assignment_id = str(lease["assignment_id"])
            self._append_deterministic(
                identity,
                f"cancel-lease-released:{assignment_id}",
                "assignment.lease.released",
                {
                    "lease_id": lease["lease_id"],
                    "assignment_id": assignment_id,
                    "agent_id": lease.get("agent_id"),
                    "stage_id": lease.get("stage_id"),
                    "reason": "run_cancelled",
                },
                source="runtime.cancel",
            )

    def _cancel_queued_deliveries(self, run_id: str, *, reason: str) -> int:
        cancelled = 0
        for worker_id, mailbox in self.mailboxes.items():
            for delivery in mailbox.pending():
                if delivery.event.run_id != run_id:
                    continue
                try:
                    mailbox.ack(delivery.delivery_id)
                except UnfencedAckError:
                    continue
                self._append_deterministic(
                    delivery.event,
                    f"queued-delivery-cancelled:{worker_id}:{delivery.delivery_id}",
                    "runtime.delivery.cancelled",
                    {
                        "agent_id": worker_id,
                        "delivery_id": delivery.delivery_id,
                        "reason": reason,
                        "mode": "queued",
                    },
                    source="runtime.cancel",
                )
                cancelled += 1
        return cancelled

    def _finalize_cancelled_run(self, identity: Event, *, reason: str) -> bool:
        run_id = identity.run_id
        if self.scheduler.in_flight_for_run(run_id):
            return False
        if any(
            delivery.event.run_id == run_id
            for mailbox in self.mailboxes.values()
            for delivery in mailbox.pending()
        ):
            return False
        self._append_deterministic(
            identity,
            f"run-cancelled:{run_id}",
            "run.cancelled",
            {"reason": reason},
            source="runtime.cancel",
        )
        return True

    def _submit_team_task(self, request: TaskRequest) -> RunCreated:
        identity = EvalRunIdentity(
            run_id=f"run_{uuid4().hex[:12]}",
            task_id=f"task_{uuid4().hex[:12]}",
        )
        created = self._prepare_team_task(request, identity)
        self._release_prepared_task("team", identity)
        return created

    def _prepare_team_task(
        self,
        request: TaskRequest,
        identity: EvalRunIdentity,
    ) -> RunCreated:
        if request.model_mode == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
            raise ValueError("DEEPSEEK_API_KEY is required for deepseek mode")
        task_pack_id = request.task_pack or self.team_pack.task_pack_id
        task_pack = self.team_task_packs.get(task_pack_id)
        if task_pack is None:
            raise ValueError(f"unsupported Team task pack: {task_pack_id}")
        run_id = identity.run_id
        task_id = identity.task_id
        task_metadata = task_pack.prepare_run(run_id)
        fixture_hash = task_metadata.get("fixture_hash")
        if fixture_hash is not None:
            task_metadata["input_hash"] = paired_input_hash(
                PairedEvalRequest(
                    request_id="runtime-prepare",
                    title=request.title,
                    brief=request.brief,
                    model_mode=request.model_mode,
                    task_pack="repo-maintainer",
                    model_budget=request.model_budget,
                ),
                str(fixture_hash),
            )
        team_contract = task_pack.team_contract()
        model_profile = self._requested_model_profile(request, task_pack_id)
        created = self.store.append(
            Event(
                id=str(uuid5(NAMESPACE_URL, f"crazy:run:{run_id}:created")),
                run_id=run_id,
                task_id=task_id,
                type="run.created",
                source="gateway.http",
                payload={
                    "title": request.title,
                    "brief": request.brief,
                    "model_mode": request.model_mode,
                    "execution_mode": "team",
                    "task_pack": task_pack_id,
                    "team_contract": team_contract.model_dump(mode="json"),
                    "model_budget": request.model_budget.model_dump(mode="json"),
                    "model_profile": model_profile,
                    "supervisor_policy": type(self.supervisor_policy).__name__,
                    "behavior_version": "v0.10.0-dev",
                    **task_metadata,
                },
            )
        )
        self.store.append(
            Event(
                id=str(uuid5(NAMESPACE_URL, f"crazy:run:{run_id}:ingress")),
                run_id=run_id,
                task_id=task_id,
                type="event.external.received",
                source="gateway.http",
                payload={
                    "title": request.title,
                    "brief": request.brief,
                    "receiver": "coordinator",
                },
                causation_id=created.id,
            )
        )
        return RunCreated(run_id=run_id, task_id=task_id)

    def _submit_single_task(self, request: TaskRequest) -> RunCreated:
        identity = EvalRunIdentity(
            run_id=f"run_{uuid4().hex[:12]}",
            task_id=f"task_{uuid4().hex[:12]}",
        )
        created = self._prepare_single_task(request, identity)
        self._release_prepared_task("single", identity)
        return created

    def _prepare_single_task(
        self,
        request: TaskRequest,
        identity: EvalRunIdentity,
        *,
        hold_for_paired_commit: bool = False,
        restore_contract: CheckpointContract | None = None,
        assignment_contract: AssignmentContract | None = None,
        source_run_created: Event | None = None,
        workspace_state_ref: str | None = None,
        run_metadata: dict[str, object] | None = None,
    ) -> RunCreated:
        task_pack_id = request.task_pack or self.repo_maintainer_pack.task_pack_id
        pack = self.task_packs.get(task_pack_id)
        if pack is None:
            raise ValueError(f"unsupported single-agent task pack: {task_pack_id}")
        if request.model_mode == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
            raise ValueError("DEEPSEEK_API_KEY is required for deepseek mode")
        run_id = identity.run_id
        task_id = identity.task_id
        existing_events = self.store.read_all(run_id=run_id)
        existing_created = next(
            (event for event in existing_events if event.type == "run.created"),
            None,
        )
        existing_assignment = next(
            (event for event in existing_events if event.type == "assignment.created"),
            None,
        )
        if existing_created is not None and existing_assignment is not None:
            expected_metadata = run_metadata or {}
            if (
                existing_created.task_id != task_id
                or existing_created.payload.get("task_pack") != task_pack_id
                or existing_created.payload.get("model_mode") != request.model_mode
                or any(
                    existing_created.payload.get(key) != value
                    for key, value in expected_metadata.items()
                )
            ):
                raise RuntimeError(
                    f"persisted single-Agent child conflicts with retry: {run_id}"
                )
            return RunCreated(run_id=run_id, task_id=task_id)

        if restore_contract is not None and workspace_state_ref is not None:
            raise ValueError("checkpoint restore and engineering state are mutually exclusive")
        if workspace_state_ref is not None:
            if not isinstance(pack, RepoQualityTaskPack):
                raise ValueError("engineering workspace state requires repo-quality")
            prepared = pack.prepare_from_state(
                run_id,
                workspace_state_ref,
                self.checkpoint_snapshots,
            )
        else:
            prepared = pack.prepare(run_id)
        model_profile = self._requested_model_profile(request, task_pack_id)
        if restore_contract is not None:
            if not isinstance(pack, RepoMaintainerTaskPack) or source_run_created is None:
                raise ValueError("checkpoint restore requires repo-maintainer source metadata")
            if pack.workspace_hash(prepared.workspace) != restore_contract.workspace.object_id:
                raise RuntimeError("restored workspace hash does not match checkpoint")
            if pack.workspace_hash(prepared.baseline) != pack.fixture_hash():
                raise RuntimeError("restored Run baseline does not match trusted fixture")
            case_metadata = {
                "case_id": str(source_run_created.payload.get("case_id", pack.case_id)),
                "fixture_hash": str(
                    source_run_created.payload.get("fixture_hash", pack.fixture_hash())
                ),
                "scorer_version": str(
                    source_run_created.payload.get("scorer_version", pack.scorer_version)
                ),
                "input_hash": str(source_run_created.payload.get("input_hash", "restored")),
                "restored_workspace_hash": restore_contract.workspace.object_id,
            }
        elif workspace_state_ref is not None:
            assert isinstance(pack, RepoQualityTaskPack)
            expected_hash = (
                pack.fixture_hash()
                if workspace_state_ref == "loop-pack://initial"
                else workspace_state_ref.removeprefix("snapshot://")
            )
            if pack.workspace_hash(prepared.workspace) != expected_hash:
                raise RuntimeError("engineering child workspace does not match Active state")
            if pack.workspace_hash(prepared.baseline) != expected_hash:
                raise RuntimeError("engineering child baseline does not match Active state")
            case_metadata = {
                "case_id": pack.case_id,
                "fixture_hash": expected_hash,
                "scorer_version": pack.scorer_version,
                "engineering_base_state_ref": workspace_state_ref,
            }
        else:
            case_metadata = (
                pack.case_metadata(prepared)
                if isinstance(pack, RepoMaintainerTaskPack)
                else {}
            )
        fixture_hash = case_metadata.get("fixture_hash")
        if fixture_hash is not None and restore_contract is None:
            case_metadata["input_hash"] = paired_input_hash(
                PairedEvalRequest(
                    request_id="runtime-prepare",
                    title=request.title,
                    brief=request.brief,
                    model_mode=request.model_mode,
                    task_pack="repo-maintainer",
                    model_budget=request.model_budget,
                ),
                str(fixture_hash),
            )
        metadata = dict(run_metadata or {})
        reserved_metadata = {
            "title",
            "brief",
            "model_mode",
            "execution_mode",
            "task_pack",
            "workspace_path",
            "baseline_path",
        }
        if reserved_metadata.intersection(metadata):
            raise ValueError("run_metadata cannot replace canonical Run fields")
        created = self.store.append(
            Event(
                id=str(uuid5(NAMESPACE_URL, f"crazy:run:{run_id}:created")),
                run_id=run_id,
                task_id=task_id,
                type="run.created",
                source="gateway.http",
                payload={
                    "title": request.title,
                    "brief": request.brief,
                    "model_mode": request.model_mode,
                    "execution_mode": "single",
                    "task_pack": task_pack_id,
                    "model_budget": request.model_budget.model_dump(mode="json"),
                    "model_profile": model_profile,
                    "workspace_path": str(prepared.workspace),
                    "baseline_path": str(
                        prepared.baseline
                        if hasattr(prepared, "baseline")
                        else prepared.workspace
                    ),
                    "behavior_version": "v0.10.0-dev",
                    **(
                        {
                            "restored_from_checkpoint_id": restore_contract.checkpoint_id,
                            "restored_from_run_id": restore_contract.source.run_id,
                        }
                        if restore_contract is not None
                        else {}
                    ),
                    **case_metadata,
                    **metadata,
                },
            )
        )
        contract = assignment_contract or pack.assignment_contract()
        self.store.append(
            Event(
                id=str(
                    uuid5(NAMESPACE_URL, f"crazy:run:{run_id}:single-assignment")
                ),
                run_id=run_id,
                task_id=task_id,
                type="assignment.created",
                source="runtime.single",
                payload={
                    "assignment_id": task_id,
                    "agent_id": pack.agent_id,
                    "goal": contract.goal,
                    "exit_criteria": list(contract.exit_criteria),
                    "contract_version": contract.version,
                    "contract": contract.model_dump(mode="json"),
                    "receiver": pack.agent_id,
                    "workspace_path": str(prepared.workspace),
                    **(
                        {"release_policy": "checkpoint_restore_commit"}
                        if restore_contract is not None
                        else {"release_policy": "paired_eval_commit"}
                        if hold_for_paired_commit
                        else {}
                    ),
                },
                causation_id=created.id,
            )
        )
        return RunCreated(run_id=run_id, task_id=task_id)

    def _release_prepared_task(
        self,
        mode: Literal["single", "team"],
        identity: EvalRunIdentity,
    ) -> None:
        events = self.store.read_all(run_id=identity.run_id)
        if mode == "team":
            trigger_type = "event.external.received"
            receiver = "coordinator"
            delivery_id = f"ingress:{identity.run_id}"
        else:
            trigger_type = "assignment.created"
            created = next(event for event in events if event.type == "run.created")
            task_pack_id = str(created.payload["task_pack"])
            receiver = self.task_packs[task_pack_id].agent_id
        triggers = [event for event in events if event.type == trigger_type]
        if len(triggers) != 1:
            raise RuntimeError(
                f"prepared {mode} run has no unique release trigger: {identity.run_id}"
            )
        trigger = triggers[0]
        if mode == "single":
            delivery_id = f"route:{trigger.id}:{receiver}"
        self._deliver(receiver, trigger, delivery_id=delivery_id)

    def run_until_idle(self, *, max_steps: int = 100) -> int:
        started_at = self.scheduler.completed_steps
        waits_without_progress = 0
        control_steps = 0
        while self.scheduler.completed_steps - started_at < max_steps:
            self._reconcile_routes()
            self._reconcile_failed_runs()
            self._reconcile_cancellations()
            self._reconcile_pauses()
            self._reconcile_resumes()
            self._reconcile_agent_nudges()
            if self._advance_eval_control():
                control_steps += 1
                waits_without_progress = 0
                if control_steps >= max_steps:
                    raise RuntimeError(
                        f"eval control did not become idle after {max_steps} steps"
                    )
                continue
            if self._advance_engineering_control():
                control_steps += 1
                waits_without_progress = 0
                if control_steps >= max_steps:
                    raise RuntimeError(
                        f"engineering control did not become idle after {max_steps} steps"
                    )
                continue
            if self.scheduler.run_once():
                waits_without_progress = 0
                continue
            expired = self.expire_due_leases()
            if expired:
                waits_without_progress = 0
                continue
            if self.scheduler.in_flight_count:
                before = self.scheduler.completed_steps
                self.scheduler.wait_for_progress(completed_steps=before, timeout=0.05)
                waits_without_progress = (
                    waits_without_progress + 1
                    if self.scheduler.completed_steps == before
                    else 0
                )
                continue
            if self._reconcile_routes():
                waits_without_progress = 0
                continue
            if self.scheduler.pending_count:
                # Another Runtime may hold the durable Claim. Wait without stealing it.
                waits_without_progress += 1
                if waits_without_progress >= max_steps:
                    break
                self.scheduler.wait(0.05)
                continue
            break
        steps = self.scheduler.completed_steps - started_at
        if (
            steps >= max_steps or waits_without_progress >= max_steps
        ) and self.scheduler.has_pending():
            raise RuntimeError(
                f"resident runtime did not become idle after {max_steps} steps"
            )
        return steps

    def run_eval_campaign_until_idle(
        self,
        campaign_id: str,
        *,
        max_steps: int = 1500,
    ) -> int:
        """Synchronously advance only one Campaign and its pre-registered Runs."""

        contract = self.campaign_service.contract(campaign_id)
        steps = 0
        waits_without_progress = 0
        while steps < max_steps:
            # Durable control facts outrank queued work. In particular, a
            # persisted failure/cancel request must build its dispatch fence
            # before this scoped loop is allowed to reserve a child Delivery.
            self._reconcile_routes()
            self._reconcile_failed_runs()
            self._reconcile_cancellations()
            campaign = self.campaign_service.report(campaign_id)
            if campaign.status != "running":
                return steps

            target_run_ids: set[str] = set()
            pair_finalized = False
            for trial in contract.trials:
                try:
                    pair_contract = self.eval_service.contract(trial.eval_id)
                except KeyError:
                    continue
                target_run_ids.update(
                    {
                        pair_contract.single.run_id,
                        pair_contract.team.run_id,
                    }
                )
                before = self.eval_service.report(trial.eval_id)
                after = self.eval_service.finalize(trial.eval_id)
                if before.status != after.status:
                    steps += 1
                    pair_finalized = True
                    break
            if pair_finalized:
                waits_without_progress = 0
                continue

            if self.campaign_service.advance_one(
                campaign_id,
                create_pair=self._create_campaign_pair,
                pair_contract=self.eval_service.contract,
                release_pair=self.release_paired_eval,
                pair_report=self.paired_eval,
            ):
                steps += 1
                waits_without_progress = 0
                continue

            finalized = self.campaign_service.finalize(campaign_id)
            if finalized.status != campaign.status:
                steps += 1
                waits_without_progress = 0
                continue

            allowed = frozenset(target_run_ids)
            if allowed and self.scheduler.run_once(allowed_run_ids=allowed):
                steps += 1
                waits_without_progress = 0
                continue
            if allowed and any(
                self.scheduler.in_flight_for_run(run_id) for run_id in allowed
            ):
                before = self.scheduler.completed_steps
                self.scheduler.wait_for_progress(completed_steps=before, timeout=0.05)
                waits_without_progress = (
                    waits_without_progress + 1
                    if self.scheduler.completed_steps == before
                    else 0
                )
                if waits_without_progress < max_steps:
                    continue
            break

        if self.campaign_service.report(campaign_id).status == "running" and (
            steps >= max_steps or waits_without_progress >= max_steps
        ):
            raise RuntimeError(
                f"eval campaign did not become idle after {max_steps} scoped steps"
            )
        return steps

    def expire_due_leases(self, *, now: datetime | None = None) -> int:
        """Persist timeout facts and wake Supervisor; wall-clock memory is not authority."""

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("lease expiry clock must be timezone-aware")
        expired_count = 0
        active_leases = [
            item
            for item in self.store.snapshot()["leases"]
            if item.get("status") == "active"
        ]
        for lease in active_leases:
            deadline = datetime.fromisoformat(str(lease["expires_at"]))
            if deadline > current:
                continue
            assignment_id = str(lease["assignment_id"])
            identity = next(
                (
                    event
                    for event in self.store.read_all(run_id=str(lease["run_id"]))
                    if event.type == "assignment.created"
                    and event.payload.get("assignment_id") == assignment_id
                ),
                None,
            )
            if identity is None:
                continue
            self._append_deterministic(
                identity,
                f"assignment-expired:{assignment_id}",
                "assignment.expired",
                {
                    "assignment_id": assignment_id,
                    "stage_id": lease.get("stage_id"),
                    "agent_id": lease["agent_id"],
                    "reason": "lease_deadline_exceeded",
                },
                source="runtime.deadline",
            )
            self._append_deterministic(
                identity,
                f"agent-degraded:{assignment_id}",
                "runtime.agent.degraded",
                {
                    "agent_id": lease["agent_id"],
                    "assignment_id": assignment_id,
                    "reason": "lease_deadline_exceeded",
                },
                source="runtime.deadline",
            )
            expired = self._append_deterministic(
                identity,
                f"lease-expired:{assignment_id}",
                "assignment.lease.expired",
                {
                    "lease_id": lease["lease_id"],
                    "assignment_id": assignment_id,
                    "stage_id": lease.get("stage_id"),
                    "agent_id": lease["agent_id"],
                    "expires_at": lease["expires_at"],
                    "expired_at": deadline.isoformat(),
                    "reason": "deadline_exceeded",
                },
                source="runtime.deadline",
            )
            self._deliver(
                "coordinator",
                expired,
                delivery_id=f"lease-expired:{assignment_id}",
            )
            expired_count += 1
        return expired_count

    def _seconds_until_next_wake(self, *, now: datetime | None = None) -> float:
        current = now or datetime.now(timezone.utc)
        deadlines = [
            datetime.fromisoformat(str(item["expires_at"]))
            for item in self.store.snapshot()["leases"]
            if item.get("status") == "active"
        ]
        deadlines.extend(
            datetime.fromisoformat(str(item["expires_at"]))
            for item in self.store.list_work_claims(state="active")
        )
        if not deadlines:
            return self.EXTERNAL_WAKE_FALLBACK_SECONDS
        deadline_delay = (min(deadlines) - current).total_seconds()
        if deadline_delay <= 0:
            return self.EXTERNAL_WAKE_FALLBACK_SECONDS
        return min(self.EXTERNAL_WAKE_FALLBACK_SECONDS, deadline_delay)

    def arm_fault(self, point: str) -> None:
        self.faults.arm(point)

    def snapshot(self, run_id: str | None = None) -> dict:
        snapshot = self.store.snapshot(run_id=run_id)
        runs = snapshot.pop("runs")
        run = next(
            (item for item in runs if run_id is None or item["run_id"] == run_id), None
        )
        latest = self.store.last(run_id=run_id) if run_id else self.store.last()
        queued_deliveries: list[dict[str, object]] = []
        visible_claim_keys: set[str] = set()
        all_work_claims = self.store.list_work_claims()
        claim_by_key = {str(claim["claim_key"]): claim for claim in all_work_claims}
        for worker_id, mailbox in self.mailboxes.items():
            for position, delivery in enumerate(mailbox.pending(), start=1):
                if run_id is not None and delivery.event.run_id != run_id:
                    continue
                delivery_claim_key = (
                    f"delivery:{mailbox.mailbox_id}:{delivery.delivery_id}"
                )
                claim = claim_by_key.get(delivery_claim_key)
                visible_claim_keys.update(
                    self.scheduler.claim_keys_for(worker_id, mailbox, delivery)
                )
                if claim is not None:
                    visible_claim_keys.update(
                        str(bundle_claim["claim_key"])
                        for bundle_claim in all_work_claims
                        if bundle_claim["owner_id"] == claim["owner_id"]
                        and bundle_claim["claimed_at"] == claim["claimed_at"]
                    )
                queued_deliveries.append(
                    {
                        "delivery_id": delivery.delivery_id,
                        "worker_id": worker_id,
                        "run_id": delivery.event.run_id,
                        "task_id": delivery.event.task_id,
                        "event_type": delivery.event.type,
                        "assignment_id": delivery.event.payload.get("assignment_id"),
                        "stage_id": delivery.event.payload.get("stage_id"),
                        "position": position,
                        "claim_state": claim.get("state") if claim else None,
                        "fencing_token": claim.get("fencing_token") if claim else None,
                    }
                )
        work_claims = [
            claim
            for claim in all_work_claims
            if run_id is None or claim["claim_key"] in visible_claim_keys
        ]
        selected_run_id = str(run["run_id"]) if run is not None else None
        model_calls = (
            self.store.list_model_calls(run_id=selected_run_id)
            if selected_run_id is not None
            else []
        )
        model_budget = None
        if selected_run_id is not None:
            model_budget = self.store.model_budget_status(selected_run_id)
            limits = ModelBudgetConfig.model_validate(run.get("model_budget") or {})
            max_cost_microusd = int(limits.max_cost_usd * Decimal(1_000_000))
            model_budget.update(
                {
                    **limits.model_dump(mode="json"),
                    "max_cost_microusd": max_cost_microusd,
                    "remaining_tokens": max(
                        0,
                        limits.max_total_tokens
                        - int(model_budget["committed_tokens"]),
                    ),
                    "remaining_cost_microusd": max(
                        0,
                        max_cost_microusd
                        - int(model_budget["committed_cost_microusd"]),
                    ),
                    "cost_kind": "estimate",
                }
            )
        return {
            "run": run,
            **snapshot,
            "model_budget": model_budget,
            "model_calls": model_calls,
            "queued_deliveries": queued_deliveries,
            "work_claims": work_claims,
            "runtime": {
                "status": "running"
                if self._thread is not None and self._thread.is_alive()
                else "manual",
                "latest_event_id": latest.id if latest else None,
                "deepseek_configured": bool(os.getenv("DEEPSEEK_API_KEY")),
                "fact_source": str(self.store.path),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "scheduler": self.scheduler.snapshot(),
            },
        }

    def submit_peer_probe(
        self,
        run_id: str,
        *,
        sender: str,
        receiver: str,
        depth: int,
    ) -> KernelDecision:
        run = self.store.projection("run", run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        token = uuid4().hex[:8]
        return self.kernel.submit(
            CommandCandidate(
                candidate_id=f"candidate_probe_{token}",
                idempotency_key=f"{run_id}:probe:{token}",
                run_id=run_id,
                task_id=run["task_id"],
                actor_id=sender,
                kind=CommandKind.PEER_REQUEST,
                payload={
                    "assignment_id": f"{run_id}:probe",
                    "receiver": receiver,
                    "scope": ["evidence"],
                    "permissions": ["read"],
                    "depth": depth,
                    "peer_budget": 1,
                    "brief": "policy probe",
                },
            )
        )

    def _model_for(
        self,
        model_mode: str,
        task_pack: TaskPack,
        model_profile: dict[str, object] | None = None,
    ) -> ModelProvider:
        if self._model_factory is not None:
            return self._model_factory(model_mode)
        if model_mode == "deepseek":
            profile = model_profile or {}
            thinking_mode = str(profile.get("thinking_mode", "disabled"))
            if thinking_mode not in {"enabled", "disabled"}:
                raise ValueError("invalid persisted DeepSeek thinking_mode")
            return DeepSeekOpenAIProvider(
                base_url=str(
                    profile.get("base_url", "https://api.deepseek.com")
                ),
                model=str(profile.get("model", "deepseek-v4-flash")),
                timeout_seconds=float(profile.get("timeout_seconds", 60.0)),
                thinking_mode=thinking_mode,
                max_tokens=int(profile.get("max_output_tokens", 4096)),
            )
        if model_mode == "scripted":
            return FakeModelProvider(task_pack.scripted_responses(run_metadata=None))
        raise ValueError(f"unsupported model mode: {model_mode}")

    def _single_agent_step(self, delivery: Delivery) -> None:
        trigger = delivery.event
        try:
            session = self._single_session_for(trigger)
            session.step()
        except InjectedKernelCrash:
            raise
        except Exception as exc:
            failed = self._append_deterministic(
                trigger,
                f"single-agent-error:{trigger.task_id}",
                "agent.failed",
                {
                    "agent_id": "generalist",
                    "assignment_id": trigger.task_id,
                    "reason": str(exc),
                    "error_type": type(exc).__name__,
                },
                source="runtime.single",
            )
            self._finish_single_run(failed, succeeded=False, reason=str(exc))
            return

        events = self.store.read_all(task_id=trigger.task_id)
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.type in {"agent.submitted", "agent.stopped", "agent.failed"}
            ),
            None,
        )
        if terminal is not None:
            succeeded = terminal.type == "agent.submitted"
            self._finish_single_run(
                terminal,
                succeeded=succeeded,
                reason=str(terminal.payload.get("reason", terminal.type)),
            )
            return

        unknown_ids = {
            event.payload.get("operation_id")
            for event in events
            if event.type == "operation.unknown"
        }
        reconciled_ids = {
            event.payload.get("operation_id")
            for event in events
            if event.type == "operation.reconciled"
        }
        if unknown_ids - reconciled_ids or (
            events and events[-1].type == "agent.waiting"
        ):
            self._append_deterministic(
                events[-1],
                f"single-paused:{trigger.task_id}",
                "run.paused",
                {"reason": "waiting for reconciliation or an external event"},
                source="runtime.single",
            )
            return

        completed_turns = sum(event.type == "model.completed" for event in events)
        max_turns = (
            session.loop.assignment_contract.budgets.turns
            if session.loop.assignment_contract
            else 20
        )
        if max_turns is not None and completed_turns >= max_turns:
            failed = self._append_deterministic(
                events[-1],
                f"single-turn-budget-exhausted:{trigger.task_id}",
                "agent.failed",
                {
                    "agent_id": "generalist",
                    "assignment_id": trigger.task_id,
                    "reason": f"turn budget exhausted after {completed_turns} turns",
                },
                source="runtime.single",
            )
            self._finish_single_run(
                failed, succeeded=False, reason=failed.payload["reason"]
            )
            return

        ready = self._append_deterministic(
            events[-1],
            f"single-turn-ready:{trigger.task_id}:{completed_turns + 1}",
            "runtime.turn.ready",
            {
                "agent_id": "generalist",
                "assignment_id": trigger.task_id,
                "next_turn": completed_turns + 1,
            },
            source="runtime.scheduler",
        )
        self._deliver(
            "generalist",
            ready,
            delivery_id=f"single:{trigger.task_id}:turn:{completed_turns + 1}",
        )

    def _single_session_for(self, trigger: Event) -> AgentRunSession:
        existing = self._single_sessions.get(trigger.task_id)
        if existing is not None:
            return existing
        events = self.store.read_all(task_id=trigger.task_id)
        created = next((event for event in events if event.type == "run.created"), None)
        if created is None:
            raise RuntimeError(
                f"single-agent task has no run.created event: {trigger.task_id}"
            )
        task_pack_id = str(
            created.payload.get("task_pack", self.repo_maintainer_pack.task_pack_id)
        )
        pack = self.task_packs.get(task_pack_id)
        if pack is None:
            raise RuntimeError(f"unsupported task pack: {task_pack_id}")
        assignment = next(
            (event for event in events if event.type == "assignment.created"), None
        )
        contract_payload = (
            assignment.payload.get("contract") if assignment is not None else None
        )
        # New runs persist the exact Contract; the fallback keeps legacy runs recoverable.
        contract = (
            AssignmentContract.model_validate(contract_payload)
            if contract_payload is not None
            else pack.assignment_contract()
        )
        model_mode = str(created.payload.get("model_mode", "scripted"))
        model = self._single_models.get(trigger.task_id)
        if model is None:
            if model_mode == "scripted" and self._model_factory is None:
                responses = pack.scripted_responses(
                    run_metadata=dict(created.payload)
                )
                completed = sum(
                    event.type == "model.completed" for event in events
                )
                if completed > len(responses):
                    raise RuntimeError(
                        "persisted Single model cursor exceeds scripted responses"
                    )
                model = FakeModelProvider(responses[completed:])
            else:
                model = self._model_for(
                    model_mode,
                    pack,
                    dict(created.payload.get("model_profile") or {}),
                )
            self._single_models[trigger.task_id] = model
        loop = pack.build_loop(
            run_id=trigger.run_id,
            task_id=trigger.task_id,
            brief=str(created.payload.get("brief", contract.goal)),
            model_mode=model_mode,
            model=model,
            event_log=self.store,
            artifact_store=self.artifacts,
            ledger_path=self.data_dir / "operations" / f"{trigger.run_id}.jsonl",
            assignment_contract=contract,
            fault_injector=self.faults.trip,
        )
        loop.model_call_authority = self.model_call_authority
        session = AgentRunSession(
            identity=AgentRunSessionIdentity(
                run_id=trigger.run_id,
                task_id=trigger.task_id,
                agent_id=pack.agent_id,
                kind=AgentRunKind.SINGLE,
            ),
            loop=loop,
        )
        self._single_sessions[trigger.task_id] = session
        return session

    def _finish_single_run(
        self, trigger: Event, *, succeeded: bool, reason: str
    ) -> None:
        created = next(
            event
            for event in self.store.read_all(run_id=trigger.run_id)
            if event.type == "run.created"
        )
        terminal_payload: dict[str, object] = {
            "reason": reason,
            "agent_id": "generalist",
        }
        if succeeded and created.payload.get("engineering_loop_id"):
            workspace = Path(str(created.payload["workspace_path"]))
            snapshot = self.checkpoint_snapshots.create(workspace)
            terminal_payload["candidate_state_ref"] = f"snapshot://{snapshot.object_id}"
        assignment_type = "assignment.completed" if succeeded else "assignment.failed"
        assignment = self._append_deterministic(
            trigger,
            f"single-assignment-terminal:{trigger.task_id}:{assignment_type}",
            assignment_type,
            {
                "assignment_id": trigger.task_id,
                "agent_id": "generalist",
                "reason": reason,
                **(
                    {"candidate_state_ref": terminal_payload["candidate_state_ref"]}
                    if "candidate_state_ref" in terminal_payload
                    else {}
                ),
            },
            source="runtime.single",
        )
        run_type = "run.succeeded" if succeeded else "run.failed"
        self._append_deterministic(
            assignment,
            f"single-run-terminal:{trigger.run_id}:{run_type}",
            run_type,
            terminal_payload,
            source="runtime.single",
        )

    def _register_agents(self) -> None:
        for agent_id, role, capabilities in self.AGENTS:
            current = self.store.projection("agent", agent_id)
            if current is not None and (
                current.get("role") == role
                and current.get("capabilities") == capabilities
                and int(current.get("max_concurrency", 1)) == 1
            ):
                continue
            self.store.append(
                Event(
                    id=str(
                        uuid5(
                            NAMESPACE_URL,
                            (
                                f"crazy:agent-card:{agent_id}:{role}:"
                                f"{','.join(capabilities)}:1"
                            ),
                        )
                    ),
                    run_id="control-plane",
                    task_id="control-plane",
                    type="agent.registered",
                    source="runtime.bootstrap",
                    payload={
                        "agent_id": agent_id,
                        "role": role,
                        "capabilities": capabilities,
                        "max_concurrency": 1,
                    },
                )
            )

    def _supervisor_step(self, delivery: Delivery) -> None:
        event = delivery.event
        if event.type not in {
            "event.external.received",
            "agent.result.submitted",
            "assignment.lease.expired",
            "agent.nudged",
        }:
            return
        contract = self._team_contract_for(event.run_id)
        patch = self.supervisor_policy.propose(
            contract,
            self._supervisor_context(event, contract),
        )
        decision = self._propose(
            agent_id="coordinator",
            trigger=event,
            kind=CommandKind.PLAN_PATCH,
            payload=patch.command_payload(),
            # The same delivery reuses its persisted response after a crash,
            # while a newer fact may retry the same rejected plan revision.
            key=(
                f"{event.run_id}:coordinator:plan:{patch.revision}:trigger:{event.id}"
            ),
        )
        waiting_event_id = (
            event.payload.get("waiting_event_id")
            if event.type == "agent.nudged"
            and event.source == "runtime.supervisor"
            and event.payload.get("kind") == "capacity_available"
            else None
        )
        if waiting_event_id:
            self._append_deterministic(
                event,
                f"capacity-wait-consumed:{waiting_event_id}",
                "orchestration.capacity.resumed",
                {
                    "waiting_event_id": waiting_event_id,
                    "nudge_event_id": event.id,
                    "candidate_id": decision.candidate_id,
                },
                source="runtime.supervisor",
            )
        if not decision.accepted:
            return
        capacity_waiting = bool(patch.waiting_stage_ids)
        if capacity_waiting:
            stage_ids = patch.waiting_stage_ids
            stages = {stage.stage_id: stage for stage in contract.stages}
            waiting = self._append_deterministic(
                event,
                f"orchestration-capacity-waiting:{event.run_id}:{patch.revision}",
                "orchestration.capacity.waiting",
                {
                    "reason": patch.waiting_reason,
                    "plan_revision": patch.revision,
                    "stage_ids": list(stage_ids),
                    "required_capability_sets": [
                        sorted(stages[stage_id].required_capabilities)
                        for stage_id in stage_ids
                        if stage_id in stages
                    ],
                },
                source="runtime.supervisor",
            )
            # A release may have raced ahead of this persisted wait. Rechecking
            # the current projection here gives the wait sticky-signal semantics.
            with self._route_lock:
                self._route_event(waiting)
            return
        if patch.blocked_reason:
            self._append_deterministic(
                event,
                f"orchestration-paused:{patch.revision}",
                "run.paused",
                {"reason": patch.blocked_reason, "plan_revision": patch.revision},
                source="runtime.supervisor",
            )
        if patch.completion_ready:
            self._propose(
                agent_id="coordinator",
                trigger=event,
                kind=CommandKind.COMPLETE,
                payload={"decision": event.payload.get("decision", "approved")},
                key=f"{event.run_id}:coordinator:complete",
            )

    def _team_contract_for(self, run_id: str) -> TeamContract:
        created = next(
            (
                event
                for event in self.store.read_all(run_id=run_id)
                if event.type == "run.created"
            ),
            None,
        )
        if created is None:
            raise RuntimeError(f"team run has no run.created event: {run_id}")
        persisted = created.payload.get("team_contract")
        return (
            TeamContract.model_validate(persisted)
            if persisted
            else self.team_task_pack_for(run_id).team_contract()
        )

    def team_task_pack_for(self, run_id: str) -> ResidentDemoTeamTaskPack:
        created = next(
            (
                event
                for event in self.store.read_all(run_id=run_id)
                if event.type == "run.created"
            ),
            None,
        )
        if created is None:
            raise RuntimeError(f"Team run has no run.created event: {run_id}")
        task_pack_id = str(created.payload.get("task_pack", self.team_pack.task_pack_id))
        try:
            return self.team_task_packs[task_pack_id]
        except KeyError as exc:
            raise RuntimeError(
                f"unsupported persisted Team task pack: {task_pack_id}"
            ) from exc

    def _supervisor_context(
        self,
        trigger: Event,
        contract: TeamContract,
    ) -> SupervisorContext:
        del contract
        run_snapshot = self.store.snapshot(run_id=trigger.run_id)
        global_snapshot = self.store.snapshot()
        assignments = run_snapshot["assignments"]
        run_active_leases = [
            lease
            for lease in run_snapshot["leases"]
            if lease.get("status") == "active"
        ]
        global_active_leases = [
            lease
            for lease in global_snapshot["leases"]
            if lease.get("status") == "active"
        ]
        completed = frozenset(
            str(item["stage_id"])
            for item in assignments
            if item.get("stage_id") and item.get("status") in {"succeeded", "completed"}
        )
        attempts: dict[str, int] = {}
        for item in assignments:
            stage_id = item.get("stage_id")
            if stage_id:
                attempts[str(stage_id)] = max(
                    attempts.get(str(stage_id), 0),
                    int(item.get("attempt", 1)),
                )

        active_loads: dict[str, int] = {}
        active_stage_agents: dict[str, str] = {}
        for lease in global_active_leases:
            agent_id = str(lease["agent_id"])
            active_loads[agent_id] = active_loads.get(agent_id, 0) + 1
        for lease in run_active_leases:
            agent_id = str(lease["agent_id"])
            if lease.get("stage_id"):
                active_stage_agents[str(lease["stage_id"])] = agent_id

        cards = tuple(
            AgentCard(
                agent_id=str(agent["agent_id"]),
                role=str(agent["role"]),
                capabilities=list(agent.get("capabilities", [])),
                max_concurrency=int(agent.get("max_concurrency", 1)),
            )
            for agent in global_snapshot["agents"]
        )
        agent_states = {
            str(agent["agent_id"]): agent for agent in global_snapshot["agents"]
        }
        statuses = {
            card.agent_id: AgentStatus(
                agent_states[card.agent_id].get("status", AgentStatus.OFFLINE.value)
            )
            for card in cards
        }
        revisions = [
            int(event.payload["revision"])
            for event in self.store.read_all(run_id=trigger.run_id)
            if event.type == "orchestration.plan.patched"
        ]
        run = self.store.projection("run", trigger.run_id) or {}
        return SupervisorContext(
            run_id=trigger.run_id,
            task_id=trigger.task_id,
            brief=str(run.get("brief", "")),
            revision=max(revisions, default=0),
            cards=cards,
            statuses=statuses,
            completed_stage_ids=completed,
            active_stage_ids=frozenset(active_stage_agents),
            active_stage_agents=active_stage_agents,
            attempts=attempts,
            active_loads=active_loads,
        )

    def _begin_leased_step(self, trigger: Event, *, agent_id: str) -> bool:
        assignment_id = trigger.payload.get("assignment_id")
        assignment = (
            self.store.projection("assignment", str(assignment_id))
            if assignment_id
            else None
        )
        lease = (
            self.store.projection("lease", str(assignment_id))
            if assignment_id
            else None
        )
        current_expiry = None
        if lease is not None and lease.get("expires_at"):
            try:
                current_expiry = datetime.fromisoformat(str(lease["expires_at"]))
            except ValueError:
                current_expiry = None
        checked_at = datetime.now(timezone.utc)
        if (
            assignment is None
            or assignment.get("agent_id") != agent_id
            or lease is None
            or lease.get("status") != "active"
            or lease.get("agent_id") != agent_id
            or current_expiry is None
            or current_expiry.tzinfo is None
            or current_expiry <= checked_at
        ):
            self._append_deterministic(
                trigger,
                f"stale-delivery:{agent_id}:{assignment_id}:{trigger.id}",
                "assignment.delivery.stale",
                {
                    "assignment_id": assignment_id,
                    "agent_id": agent_id,
                    "delivery_event_id": trigger.id,
                    "reason": "active_lease_not_held",
                },
                source="runtime.scheduler",
            )
            return False

        heartbeat = self._append_deterministic(
            trigger,
            f"heartbeat:{agent_id}:{assignment_id}:{trigger.id}",
            "runtime.agent.heartbeat",
            {"agent_id": agent_id, "assignment_id": assignment_id},
            source="runtime.scheduler",
        )
        # 重投同一 Delivery 时，复用确定性 heartbeat 首次落盘的时间锚点，
        # 使 Lease 续期 Event 的 ID 和 payload 都保持一致。
        renewed_at = heartbeat.created_at
        lease_seconds = int(lease.get("lease_seconds", 30))
        proposed_expiry = renewed_at + timedelta(seconds=lease_seconds)
        expires_at = max(current_expiry, proposed_expiry)
        self._append_deterministic(
            trigger,
            f"lease-renewed:{agent_id}:{assignment_id}:{trigger.id}",
            "assignment.lease.renewed",
            {
                "lease_id": lease["lease_id"],
                "assignment_id": assignment_id,
                "stage_id": lease.get("stage_id"),
                "agent_id": agent_id,
                "lease_seconds": lease_seconds,
                "renewed_at": renewed_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
            source="runtime.scheduler",
        )
        return True

    def _dream_step(self, delivery: Delivery) -> None:
        event = delivery.event
        if event.type != "dream.job.queued":
            return
        job_id = str(event.payload["job_id"])
        started = self._append_deterministic(
            event,
            f"dream:{job_id}:started",
            "dream.job.started",
            {"job_id": job_id, "mode": "read_only", "side_effects": "forbidden"},
            source="dream.worker",
        )
        evidence = [
            item.id
            for item in self.store.read_all(run_id=event.run_id)
            if item.type
            in {"evidence.recorded", "artifact.recorded", "review.recorded"}
        ]
        frozen = self._append_deterministic(
            event,
            f"dream:{job_id}:evidence",
            "dream.evidence.frozen",
            {"job_id": job_id, "evidence_refs": evidence, "immutable": True},
            source="dream.worker",
            causation_id=started.id,
        )
        memory_id = f"memory_{event.run_id}_peer_reconcile"
        self._propose_service(
            actor_id="dream.worker",
            trigger=frozen,
            kind=CommandKind.MEMORY,
            payload={
                "candidate_id": memory_id,
                "slot": "procedure",
                "content": "Before composing an artifact, use one bounded peer check when evidence freshness matters.",
                "scope": "resident-a2a-demo",
                "evidence_refs": evidence,
                "confidence": 0.92,
                "risk": "low",
                "expiry": None,
            },
            key=f"{event.run_id}:dream:memory",
        )
        signal = self._append_deterministic(
            event,
            f"dream:{job_id}:evolution-signal",
            "evolution.signal.ready",
            {
                "job_id": job_id,
                "receiver": "context.evolver",
                "evidence_refs": evidence,
                "signal": "large tool results were safely offloaded",
            },
            source="dream.worker",
        )
        self._deliver(
            "context.evolver", signal, delivery_id=f"evolution:{event.run_id}"
        )
        self._append_deterministic(
            event,
            f"dream:{job_id}:completed",
            "dream.job.completed",
            {"job_id": job_id, "memory_candidate_id": memory_id},
            source="dream.worker",
        )

    def _evolver_step(self, delivery: Delivery) -> None:
        event = delivery.event
        if event.type != "evolution.signal.ready":
            return
        offloads = sum(
            item.type == "context.item.offloaded"
            for item in self.store.read_all(run_id=event.run_id)
        )
        candidate_id = f"evolution_{event.run_id}_context_limit"
        self._propose_service(
            actor_id="context.evolver",
            trigger=event,
            kind=CommandKind.EVOLUTION,
            payload={
                "candidate_id": candidate_id,
                "base_version": "v0.1.0",
                "proposed_version": "v0.1.1-candidate",
                "rationale": "Retain slightly more structured facts after successful offloading.",
                "evidence_refs": event.payload["evidence_refs"],
                "diffs": [
                    {
                        "target": "context",
                        "path": "recent_event_limit",
                        "before": 20,
                        "after": 24,
                        "permission_effect": "unchanged",
                    }
                ],
                "offline_metrics": {
                    "required_fact_coverage": 1.0,
                    "offloaded_large_results": offloads,
                },
            },
            key=f"{event.run_id}:evolver:candidate",
        )

    def _propose(
        self,
        *,
        agent_id: str,
        trigger: Event,
        kind: CommandKind,
        payload: dict,
        key: str,
    ) -> KernelDecision:
        command = CommandCandidate(
            candidate_id=f"candidate_{uuid5(NAMESPACE_URL, key).hex}",
            idempotency_key=key,
            run_id=trigger.run_id,
            task_id=trigger.task_id,
            actor_id=agent_id,
            kind=kind,
            payload=payload,
        )
        # Response/Candidate 已经落盘时，恢复路径必须复用，不能重编 Prompt 或重调模型。
        if self.store.command_record(key) is not None:
            self._append_deterministic(
                trigger,
                f"model-response-reused:{key}",
                "model.response.reused",
                {
                    "agent_id": agent_id,
                    "candidate_id": command.candidate_id,
                    "idempotency_key": key,
                    "reason": "persisted response precedes the interrupted harness step",
                },
                source="runtime.recovery",
            )
            decision = self.kernel.submit(command)
            self._route_decision(decision)
            return decision

        compiled = self.context.compile(
            agent_id=agent_id, trigger=trigger, context_key=key
        )
        request = self._append_deterministic(
            trigger,
            f"model-request:{key}",
            "model.requested",
            {
                "agent_id": agent_id,
                "provider": "scripted",
                "prompt_hash": compiled.manifest.prompt_hash,
                "token_estimate": compiled.manifest.token_estimate,
            },
            source=agent_id,
            causation_id=compiled.event.id,
        )
        self._append_deterministic(
            trigger,
            f"model-response:{key}",
            "model.completed",
            {
                "agent_id": agent_id,
                "provider": "scripted",
                "response": command.model_dump(mode="json"),
                "boundary": "proposal_only",
            },
            source=agent_id,
            causation_id=request.id,
        )
        decision = self.kernel.submit(command)
        self._route_decision(decision)
        return decision

    def _propose_service(
        self,
        *,
        actor_id: str,
        trigger: Event,
        kind: CommandKind,
        payload: dict,
        key: str,
    ) -> KernelDecision:
        command = CommandCandidate(
            candidate_id=f"candidate_{uuid5(NAMESPACE_URL, key).hex}",
            idempotency_key=key,
            run_id=trigger.run_id,
            task_id=trigger.task_id,
            actor_id=actor_id,
            kind=kind,
            payload=payload,
        )
        decision = self.kernel.submit(command)
        self._route_decision(decision)
        return decision

    def _route_decision(self, decision: KernelDecision) -> None:
        with self._route_lock:
            for event in self.kernel.events_for(decision):
                self._route_event(event)

    def _reconcile_routes(self) -> int:
        with self._route_lock:
            routed = 0
            for record in self.store.read_records(after=self._route_cursor):
                if self._route_event(record.event):
                    routed += 1
                self._route_cursor = record.cursor
            return routed

    def _route_event(self, event: Event) -> bool:
        if event.type == "orchestration.capacity.waiting":
            run = self.store.projection("run", event.run_id) or {}
            if run.get("status") != "running":
                self._capacity_waiters.pop(event.run_id, None)
                return False
            self._capacity_waiters[event.run_id] = event
            return self._route_capacity_waiter_if_ready(event)
        if event.type == "orchestration.capacity.resumed":
            waiting = self._capacity_waiters.get(event.run_id)
            if waiting is not None and waiting.id == event.payload.get(
                "waiting_event_id"
            ):
                self._capacity_waiters.pop(event.run_id, None)
            return False
        if event.type in {
            "run.cancel.requested",
            "run.succeeded",
            "run.failed",
            "run.cancelled",
        }:
            self._capacity_waiters.pop(event.run_id, None)
        if event.type == "run.failure.requested":
            self._finalize_failed_run(
                event,
                reason=str(event.payload.get("reason", "model call failed")),
            )
            return True
        if (
            event.type == "candidate.rejected"
            and event.payload.get("kind") == CommandKind.PLAN_PATCH.value
            and str(event.payload.get("reason", "")).startswith(
                "agent_concurrency_exceeded:"
            )
        ):
            run = self.store.projection("run", event.run_id) or {}
            if run.get("status") != "running":
                return False
            reason = str(event.payload["reason"])
            nudge = self._append_deterministic(
                event,
                f"supervisor-capacity-nudge:{event.id}",
                "agent.nudged",
                {
                    "agent_id": "coordinator",
                    "candidate_id": event.payload.get("candidate_id"),
                    "kind": "capacity_replan",
                    "message": (
                        "Plan admission raced with current agent capacity; "
                        "recompile from durable status."
                    ),
                    "reason": reason,
                    "rejected_event_id": event.id,
                },
                source="runtime.supervisor",
            )
            self._deliver(
                "coordinator",
                nudge,
                delivery_id=f"route:{nudge.id}:coordinator",
            )
            return True
        if event.type in {
            "agent.registered",
            "assignment.lease.released",
            "runtime.agent.recovered",
        }:
            return self._route_capacity_waiters(event)
        receiver: str | None = None
        if event.type == "assignment.created":
            if (
                event.payload.get("release_policy") == "paired_eval_commit"
                and not self._paired_assignment_is_committed(event)
            ):
                return False
            if (
                event.payload.get("release_policy") == "checkpoint_restore_commit"
                and not self._checkpoint_restore_assignment_is_committed(event)
            ):
                return False
            receiver = event.payload.get("agent_id")
        elif event.type in {
            "agent.result.submitted",
            "a2a.peer.requested",
            "a2a.peer.responded",
        }:
            receiver = event.payload.get("receiver")
        elif event.type == "agent.nudged":
            receiver = event.payload.get("agent_id")
        if receiver in self.mailboxes:
            self._deliver(
                receiver,
                event,
                delivery_id=f"route:{event.id}:{receiver}",
            )
            return True
        if event.type == "run.succeeded":
            self._queue_dream(event)
            return True
        return False

    def _route_capacity_waiters(self, capacity_event: Event) -> bool:
        routed = False
        del capacity_event
        for waiting in sorted(
            self._capacity_waiters.values(), key=lambda item: item.run_id
        ):
            routed = self._route_capacity_waiter_if_ready(waiting) or routed
        return routed

    def _route_capacity_waiter_if_ready(self, waiting: Event) -> bool:
        run = self.store.projection("run", waiting.run_id) or {}
        if run.get("status") != "running":
            self._capacity_waiters.pop(waiting.run_id, None)
            return False
        required_sets = tuple(
            frozenset(str(capability) for capability in required)
            for required in waiting.payload.get("required_capability_sets", [])
            if isinstance(required, list)
        )
        if not required_sets:
            return False

        snapshot = self.store.snapshot()
        active_loads: dict[str, int] = {}
        for lease in snapshot["leases"]:
            if lease.get("status") != "active":
                continue
            agent_id = str(lease.get("agent_id", ""))
            active_loads[agent_id] = active_loads.get(agent_id, 0) + 1
        available = any(
            str(agent.get("status")) in {AgentStatus.IDLE.value, AgentStatus.BUSY.value}
            and active_loads.get(str(agent["agent_id"]), 0)
            < int(agent.get("max_concurrency", 1))
            and any(
                required.issubset(set(agent.get("capabilities", [])))
                for required in required_sets
            )
            for agent in snapshot["agents"]
        )
        if not available:
            return False

        nudge = self._append_deterministic(
            waiting,
            f"capacity-available-nudge:{waiting.id}",
            "agent.nudged",
            {
                "agent_id": "coordinator",
                "kind": "capacity_available",
                "message": (
                    "Agent capacity changed; recompile the plan from durable status."
                ),
                "reason": waiting.payload.get("reason"),
                "waiting_event_id": waiting.id,
            },
            source="runtime.supervisor",
            causation_id=waiting.id,
        )
        self._deliver(
            "coordinator",
            nudge,
            delivery_id=f"route:{nudge.id}:coordinator",
        )
        return True

    def _paired_assignment_is_committed(self, assignment: Event) -> bool:
        links = [
            event
            for event in self.store.read_all(run_id=assignment.run_id)
            if event.type == "eval.arm.linked"
        ]
        if len(links) != 1:
            return False
        eval_id = str(links[0].payload.get("eval_id", ""))
        eval_events = self.store.read_all(run_id=eval_id)
        committed = any(
            event.type == "eval.pair.committed"
            for event in eval_events
        )
        released = any(
            event.type == "eval.arm.released"
            and event.payload.get("execution_mode")
            == assignment.payload.get("execution_mode", "single")
            for event in eval_events
        )
        return committed and released

    def _checkpoint_restore_assignment_is_committed(
        self,
        assignment: Event,
    ) -> bool:
        events = self.store.read_all(run_id=assignment.run_id)
        created = next((event for event in events if event.type == "run.created"), None)
        if created is None:
            return False
        checkpoint_id = created.payload.get("restored_from_checkpoint_id")
        return isinstance(checkpoint_id, str) and any(
            event.type == "checkpoint.restore.committed"
            and event.payload.get("checkpoint_id") == checkpoint_id
            for event in events
        )

    def _queue_dream(self, succeeded: Event) -> None:
        signal = self._append_deterministic(
            succeeded,
            f"learning-signal:{succeeded.run_id}",
            "learning.signal.detected",
            {
                "signal_id": f"signal_{succeeded.run_id}",
                "kind": "successful_peer_reconciliation",
                "detector": "deterministic_rule",
            },
            source="learning.detector",
        )
        job_id = f"dream_{succeeded.run_id}"
        job = self._append_deterministic(
            succeeded,
            f"dream-job:{succeeded.run_id}",
            "dream.job.queued",
            {
                "job_id": job_id,
                "signal_id": signal.payload["signal_id"],
                "receiver": "dream.worker",
            },
            source="dream.scheduler",
            causation_id=signal.id,
        )
        self._deliver("dream.worker", job, delivery_id=f"dream:{succeeded.run_id}")

    def _deliver(self, receiver: str, event: Event, *, delivery_id: str) -> None:
        self.mailboxes[receiver].send(event, delivery_id=delivery_id)
        self.scheduler.signal()

    def _append_deterministic(
        self,
        identity: Event,
        key: str,
        event_type: str,
        payload: dict,
        *,
        source: str,
        causation_id: str | None = None,
    ) -> Event:
        return self.store.append(
            Event(
                id=str(uuid5(NAMESPACE_URL, f"crazy:{key}")),
                run_id=identity.run_id,
                task_id=identity.task_id,
                type=event_type,
                source=source,
                payload=payload,
                causation_id=causation_id or identity.id,
            )
        )
