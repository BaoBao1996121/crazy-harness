from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.control_plane.context import PersistentContextCompiler
from crazy_harness.control_plane.kernel import (
    CommandCandidate,
    CommandKind,
    ControlKernel,
    FaultController,
    InjectedKernelCrash,
    KernelDecision,
)
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.a2a.coordinator import AgentStatus
from crazy_harness.core.a2a.messages import AgentCard
from crazy_harness.core.a2a.orchestration import (
    CapabilitySupervisorPolicy,
    SupervisorContext,
    SupervisorPolicy,
    TeamContract,
)
from crazy_harness.core.agents import AgentLoop, AssignmentContract
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.events import Event
from crazy_harness.core.models import DeepSeekOpenAIProvider, FakeModelProvider, ModelProvider
from crazy_harness.core.runtime import DurableMailbox
from crazy_harness.core.runtime.mailbox import Delivery
from crazy_harness.taskpacks import (
    EvidenceResearchTaskPack,
    RepoMaintainerTaskPack,
    ResidentDemoTeamTaskPack,
    TaskPack,
)


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(min_length=1, max_length=4000)
    model_mode: Literal["scripted", "deepseek"] = "scripted"
    execution_mode: Literal["team", "single"] = "team"
    task_pack: Literal["resident-demo", "repo-maintainer", "evidence-research"] | None = None


class RunCreated(BaseModel):
    run_id: str
    task_id: str
    status: str = "queued"


Handler = Callable[[Delivery], None]
ModelFactory = Callable[[str], ModelProvider]


class ResidentScheduler:
    """A tiny always-on dispatcher; durable mailboxes remain the source of pending work."""

    def __init__(self, store: SQLiteEventStore, fault_controller: FaultController) -> None:
        self.store = store
        self.fault_controller = fault_controller
        self._workers: dict[str, tuple[DurableMailbox, Handler]] = {}
        self._condition = threading.Condition()
        self._wake_generation = 0
        self._consumed_generation = 0
        self._run_lock = threading.Lock()

    def register(self, worker_id: str, mailbox: DurableMailbox, handler: Handler) -> None:
        if worker_id in self._workers:
            raise ValueError(f"worker already registered: {worker_id}")
        self._workers[worker_id] = (mailbox, handler)

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
        return any(mailbox.peek() is not None for mailbox, _ in self._workers.values())

    def run_once(self) -> bool:
        # API drain 与后台线程可能同时唤醒 Scheduler；选取、处理和 ack 必须是单消费者临界区。
        with self._run_lock:
            return self._run_once_serialized()

    def _run_once_serialized(self) -> bool:
        selected = next(
            (
                (worker_id, mailbox, handler, delivery)
                for worker_id, (mailbox, handler) in self._workers.items()
                if (delivery := mailbox.peek()) is not None
            ),
            None,
        )
        if selected is None:
            return False
        worker_id, mailbox, handler, delivery = selected
        self._append(
            delivery.event,
            "runtime.agent.busy",
            {"agent_id": worker_id, "delivery_id": delivery.delivery_id},
        )
        try:
            handler(delivery)
            self.fault_controller.trip("before_mailbox_ack")
        except InjectedKernelCrash as exc:
            # 不 ack：同一 Delivery 会再次出现，业务命令依靠幂等键恢复。
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
            return True

        mailbox.ack(delivery.delivery_id)
        self._append(
            delivery.event,
            "runtime.agent.step.completed",
            {"agent_id": worker_id, "delivery_id": delivery.delivery_id},
        )
        self._append(
            delivery.event,
            "runtime.agent.idle",
            {"agent_id": worker_id},
        )
        return True

    def _append(self, identity: Event, event_type: str, payload: dict) -> Event:
        return self.store.append(
            Event(
                run_id=identity.run_id,
                task_id=identity.task_id,
                type=event_type,
                source="runtime.scheduler",
                payload=payload,
                causation_id=identity.id,
            )
        )


class ResidentRuntime:
    """Cohesive resident runtime used by the API, Control Room, and learning tests."""

    AGENTS = (
        ("coordinator", "Coordinator / 总控", ["orchestration.plan", "completion.gate"]),
        ("scout", "Scout / 侦察", ["evidence.collect", "peer.respond"]),
        ("scout-backup", "Scout Backup / 侦察备用", ["evidence.collect", "peer.respond"]),
        ("builder", "Builder / 构建", ["artifact.compose", "peer.request"]),
        ("reviewer", "Reviewer / 审查", ["artifact.review", "evidence.verify"]),
        ("generalist", "Generalist / 通用执行", ["repo.inspect", "repo.edit", "test.verify", "research.browse", "research.cite"]),
    )

    def __init__(
        self,
        data_dir: Path,
        *,
        model_factory: ModelFactory | None = None,
        supervisor_policy: SupervisorPolicy | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteEventStore(self.data_dir / "control_plane.db")
        self.artifacts = ArtifactStore(self.data_dir / "artifacts")
        self.faults = FaultController()
        self.kernel = ControlKernel(self.store, fault_controller=self.faults)
        self.context = PersistentContextCompiler(self.store, self.artifacts)
        self.scheduler = ResidentScheduler(self.store, self.faults)
        self.team_pack = ResidentDemoTeamTaskPack()
        self.team_contract = self.team_pack.team_contract()
        self.supervisor_policy = supervisor_policy or CapabilitySupervisorPolicy()
        self.repo_maintainer_pack = RepoMaintainerTaskPack(self.data_dir)
        self.evidence_research_pack = EvidenceResearchTaskPack(self.data_dir)
        self.task_packs: dict[str, TaskPack] = {
            self.repo_maintainer_pack.task_pack_id: self.repo_maintainer_pack,
            self.evidence_research_pack.task_pack_id: self.evidence_research_pack,
        }
        self._model_factory = model_factory
        self._single_models: dict[str, ModelProvider] = {}
        self._single_loops: dict[str, AgentLoop] = {}
        self.mailboxes: dict[str, DurableMailbox] = {
            worker_id: DurableMailbox(worker_id, self.store)
            for worker_id in [*(agent[0] for agent in self.AGENTS), "dream.worker", "context.evolver"]
        }
        self._register_agents()
        self.scheduler.register("coordinator", self.mailboxes["coordinator"], self._supervisor_step)
        self.scheduler.register(
            "scout",
            self.mailboxes["scout"],
            lambda delivery: self._scout_step(delivery, agent_id="scout"),
        )
        self.scheduler.register(
            "scout-backup",
            self.mailboxes["scout-backup"],
            lambda delivery: self._scout_step(delivery, agent_id="scout-backup"),
        )
        self.scheduler.register("builder", self.mailboxes["builder"], self._builder_step)
        self.scheduler.register("reviewer", self.mailboxes["reviewer"], self._reviewer_step)
        self.scheduler.register("generalist", self.mailboxes["generalist"], self._single_agent_step)
        self.scheduler.register("dream.worker", self.mailboxes["dream.worker"], self._dream_step)
        self.scheduler.register("context.evolver", self.mailboxes["context.evolver"], self._evolver_step)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="crazy-resident-runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.scheduler.signal()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None

    def _serve(self) -> None:
        while not self._stop.is_set():
            if self.expire_due_leases():
                continue
            if self.scheduler.run_once():
                continue
            self.scheduler.wait(self._seconds_until_next_lease())

    def submit_task(self, request: TaskRequest) -> RunCreated:
        if request.execution_mode == "single":
            return self._submit_single_task(request)
        if request.model_mode != "scripted":
            raise ValueError(
                "team mode currently uses a deterministic Supervisor with scripted "
                "workers; use execution_mode='single' for DeepSeek"
            )
        if request.task_pack not in {None, "resident-demo"}:
            raise ValueError("team mode currently supports only the resident-demo task pack")
        return self._submit_team_task(request)

    def _submit_team_task(self, request: TaskRequest) -> RunCreated:
        if request.model_mode == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
            raise ValueError("DEEPSEEK_API_KEY is required for deepseek mode")
        run_id = f"run_{uuid4().hex[:12]}"
        task_id = f"task_{uuid4().hex[:12]}"
        created = self.store.append(
            Event(
                run_id=run_id,
                task_id=task_id,
                type="run.created",
                source="gateway.http",
                payload={
                    "title": request.title,
                    "brief": request.brief,
                    "model_mode": request.model_mode,
                    "execution_mode": "team",
                    "task_pack": self.team_pack.task_pack_id,
                    "team_contract": self.team_contract.model_dump(mode="json"),
                    "supervisor_policy": type(self.supervisor_policy).__name__,
                    "behavior_version": "v0.4.0-dev",
                },
            )
        )
        ingress = self.store.append(
            Event(
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
        self._deliver("coordinator", ingress, delivery_id=f"ingress:{run_id}")
        return RunCreated(run_id=run_id, task_id=task_id)

    def _submit_single_task(self, request: TaskRequest) -> RunCreated:
        task_pack_id = request.task_pack or self.repo_maintainer_pack.task_pack_id
        pack = self.task_packs.get(task_pack_id)
        if pack is None:
            raise ValueError(f"unsupported single-agent task pack: {task_pack_id}")
        if request.model_mode == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
            raise ValueError("DEEPSEEK_API_KEY is required for deepseek mode")
        run_id = f"run_{uuid4().hex[:12]}"
        task_id = f"task_{uuid4().hex[:12]}"
        prepared = pack.prepare(run_id)
        created = self.store.append(
            Event(
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
                    "workspace_path": str(prepared.workspace),
                    "behavior_version": "v0.3.0-dev",
                },
            )
        )
        contract = pack.assignment_contract()
        assignment = self.store.append(
            Event(
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
                },
                causation_id=created.id,
            )
        )
        self._deliver(pack.agent_id, assignment, delivery_id=f"single:{task_id}:turn:1")
        return RunCreated(run_id=run_id, task_id=task_id)

    def run_until_idle(self, *, max_steps: int = 100) -> int:
        steps = 0
        while steps < max_steps:
            if self.scheduler.run_once():
                steps += 1
                continue
            expired = self.expire_due_leases()
            if expired:
                steps += expired
                continue
            break
        if steps == max_steps and self.scheduler.has_pending():
            raise RuntimeError(f"resident runtime did not become idle after {max_steps} steps")
        return steps

    def expire_due_leases(self, *, now: datetime | None = None) -> int:
        """Persist timeout facts and wake Supervisor; wall-clock memory is not authority."""

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("lease expiry clock must be timezone-aware")
        expired_count = 0
        active_leases = [
            item for item in self.store.snapshot()["leases"] if item.get("status") == "active"
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

    def _seconds_until_next_lease(self, *, now: datetime | None = None) -> float | None:
        current = now or datetime.now(timezone.utc)
        deadlines = [
            datetime.fromisoformat(str(item["expires_at"]))
            for item in self.store.snapshot()["leases"]
            if item.get("status") == "active"
        ]
        if not deadlines:
            return None
        return max(0.0, (min(deadlines) - current).total_seconds())

    def arm_fault(self, point: str) -> None:
        self.faults.arm(point)

    def snapshot(self, run_id: str | None = None) -> dict:
        snapshot = self.store.snapshot(run_id=run_id)
        runs = snapshot.pop("runs")
        run = next((item for item in runs if run_id is None or item["run_id"] == run_id), None)
        latest = self.store.last(run_id=run_id) if run_id else self.store.last()
        return {
            "run": run,
            **snapshot,
            "runtime": {
                "status": "running" if self._thread is not None and self._thread.is_alive() else "manual",
                "latest_event_id": latest.id if latest else None,
                "deepseek_configured": bool(os.getenv("DEEPSEEK_API_KEY")),
                "fact_source": str(self.store.path),
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

    def _model_for(self, model_mode: str, task_pack: TaskPack) -> ModelProvider:
        if self._model_factory is not None:
            return self._model_factory(model_mode)
        if model_mode == "deepseek":
            return DeepSeekOpenAIProvider()
        if model_mode == "scripted":
            return FakeModelProvider(task_pack.scripted_responses())
        raise ValueError(f"unsupported model mode: {model_mode}")

    def _single_agent_step(self, delivery: Delivery) -> None:
        trigger = delivery.event
        try:
            loop = self._single_loop_for(trigger)
            loop.run_once()
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
        if unknown_ids - reconciled_ids or (events and events[-1].type == "agent.waiting"):
            self._append_deterministic(
                events[-1],
                f"single-paused:{trigger.task_id}",
                "run.paused",
                {"reason": "waiting for reconciliation or an external event"},
                source="runtime.single",
            )
            return

        completed_turns = sum(event.type == "model.completed" for event in events)
        max_turns = loop.assignment_contract.budgets.turns if loop.assignment_contract else 20
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
            self._finish_single_run(failed, succeeded=False, reason=failed.payload["reason"])
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

    def _single_loop_for(self, trigger: Event) -> AgentLoop:
        existing = self._single_loops.get(trigger.task_id)
        if existing is not None:
            return existing
        events = self.store.read_all(task_id=trigger.task_id)
        created = next((event for event in events if event.type == "run.created"), None)
        if created is None:
            raise RuntimeError(f"single-agent task has no run.created event: {trigger.task_id}")
        task_pack_id = str(
            created.payload.get("task_pack", self.repo_maintainer_pack.task_pack_id)
        )
        pack = self.task_packs.get(task_pack_id)
        if pack is None:
            raise RuntimeError(f"unsupported task pack: {task_pack_id}")
        assignment = next((event for event in events if event.type == "assignment.created"), None)
        contract_payload = assignment.payload.get("contract") if assignment is not None else None
        # New runs persist the exact Contract; the fallback keeps legacy runs recoverable.
        contract = (
            AssignmentContract.model_validate(contract_payload)
            if contract_payload is not None
            else pack.assignment_contract()
        )
        model_mode = str(created.payload.get("model_mode", "scripted"))
        model = self._single_models.get(trigger.task_id)
        if model is None:
            model = self._model_for(model_mode, pack)
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
        self._single_loops[trigger.task_id] = loop
        return loop

    def _finish_single_run(self, trigger: Event, *, succeeded: bool, reason: str) -> None:
        assignment_type = "assignment.completed" if succeeded else "assignment.failed"
        assignment = self._append_deterministic(
            trigger,
            f"single-assignment-terminal:{trigger.task_id}:{assignment_type}",
            assignment_type,
            {
                "assignment_id": trigger.task_id,
                "agent_id": "generalist",
                "reason": reason,
            },
            source="runtime.single",
        )
        run_type = "run.succeeded" if succeeded else "run.failed"
        self._append_deterministic(
            assignment,
            f"single-run-terminal:{trigger.run_id}:{run_type}",
            run_type,
            {"reason": reason, "agent_id": "generalist"},
            source="runtime.single",
        )

    def _register_agents(self) -> None:
        for agent_id, role, capabilities in self.AGENTS:
            if self.store.projection("agent", agent_id) is not None:
                continue
            self.store.append(
                Event(
                    id=str(uuid5(NAMESPACE_URL, f"crazy:agent-card:{agent_id}")),
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
            key=f"{event.run_id}:coordinator:plan:{patch.revision}",
        )
        if not decision.accepted:
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
        return TeamContract.model_validate(persisted) if persisted else self.team_contract

    def _supervisor_context(
        self,
        trigger: Event,
        contract: TeamContract,
    ) -> SupervisorContext:
        del contract
        snapshot = self.store.snapshot(run_id=trigger.run_id)
        assignments = snapshot["assignments"]
        active_leases = [
            lease for lease in snapshot["leases"] if lease.get("status") == "active"
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
        for lease in active_leases:
            agent_id = str(lease["agent_id"])
            active_loads[agent_id] = active_loads.get(agent_id, 0) + 1
            if lease.get("stage_id"):
                active_stage_agents[str(lease["stage_id"])] = agent_id

        cards = tuple(
            AgentCard(
                agent_id=str(agent["agent_id"]),
                role=str(agent["role"]),
                capabilities=list(agent.get("capabilities", [])),
                max_concurrency=int(agent.get("max_concurrency", 1)),
            )
            for agent in snapshot["agents"]
        )
        agent_states = {str(agent["agent_id"]): agent for agent in snapshot["agents"]}
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
        current = datetime.now(timezone.utc)
        if (
            assignment is None
            or assignment.get("agent_id") != agent_id
            or lease is None
            or lease.get("status") != "active"
            or lease.get("agent_id") != agent_id
            or current_expiry is None
            or current_expiry.tzinfo is None
            or current_expiry <= current
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

        lease_seconds = int(lease.get("lease_seconds", 30))
        proposed_expiry = current + timedelta(seconds=lease_seconds)
        expires_at = max(current_expiry, proposed_expiry)
        self._append_deterministic(
            trigger,
            f"heartbeat:{agent_id}:{assignment_id}:{trigger.id}",
            "runtime.agent.heartbeat",
            {"agent_id": agent_id, "assignment_id": assignment_id},
            source="runtime.scheduler",
        )
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
                "renewed_at": current.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
            source="runtime.scheduler",
        )
        return True

    def _scout_step(self, delivery: Delivery, *, agent_id: str) -> None:
        event = delivery.event
        if event.type == "assignment.created":
            if not self._begin_leased_step(event, agent_id=agent_id):
                return
            tool_result = self._collect_evidence(event)
            self._propose(
                agent_id=agent_id,
                trigger=event,
                kind=CommandKind.EVIDENCE,
                payload={
                    "assignment_id": event.payload["assignment_id"],
                    "summary": "Repository evidence and deterministic test observations are available.",
                    "evidence_refs": [tool_result.id],
                },
                key=f"{event.run_id}:{agent_id}:{event.payload['assignment_id']}:evidence",
            )
        elif event.type == "a2a.peer.requested":
            evidence_refs = [
                item.id
                for item in self.store.read_all(run_id=event.run_id)
                if item.type in {"evidence.recorded", "tool.completed"}
            ]
            self._propose(
                agent_id=agent_id,
                trigger=event,
                kind=CommandKind.PEER_RESPONSE,
                payload={
                    "assignment_id": event.payload["assignment_id"],
                    "receiver": event.payload["sender"],
                    "brief": "Cross-check complete: the cited tool observation exists and is current.",
                    "evidence_refs": evidence_refs,
                    "correlation_id": event.payload.get("correlation_id"),
                },
                key=f"{event.run_id}:{agent_id}:peer-response:{event.payload['assignment_id']}",
            )

    def _builder_step(self, delivery: Delivery) -> None:
        event = delivery.event
        if event.type == "assignment.created":
            if not self._begin_leased_step(event, agent_id="builder"):
                return
            evidence_agent = next(
                (
                    item.payload.get("agent_id")
                    for item in reversed(self.store.read_all(run_id=event.run_id))
                    if item.type == "evidence.recorded"
                ),
                "scout",
            )
            self._propose(
                agent_id="builder",
                trigger=event,
                kind=CommandKind.PEER_REQUEST,
                payload={
                    "assignment_id": event.payload["assignment_id"],
                    "receiver": evidence_agent,
                    "scope": ["evidence"],
                    "permissions": ["read"],
                    "depth": 1,
                    "peer_budget": 1,
                    "brief": "Confirm that the evidence is current before I compose the artifact.",
                },
                key=f"{event.run_id}:builder:{event.payload['assignment_id']}:peer-request",
            )
        elif event.type == "a2a.peer.responded":
            if not self._begin_leased_step(event, agent_id="builder"):
                return
            self._propose(
                agent_id="builder",
                trigger=event,
                kind=CommandKind.ARTIFACT,
                payload={
                    "assignment_id": event.payload["assignment_id"],
                    "title": "Bounded execution plan",
                    "summary": "A reversible plan grounded in the Scout evidence capsule.",
                    "evidence_refs": event.payload["evidence_refs"],
                    "content": {
                        "steps": ["inspect evidence", "apply bounded change", "run checks"],
                        "rollback": "restore the previous immutable behavior version",
                    },
                },
                key=f"{event.run_id}:builder:{event.payload['assignment_id']}:artifact",
            )

    def _reviewer_step(self, delivery: Delivery) -> None:
        event = delivery.event
        if event.type != "assignment.created":
            return
        if not self._begin_leased_step(event, agent_id="reviewer"):
            return
        evidence_refs = [
            item.id
            for item in self.store.read_all(run_id=event.run_id)
            if item.type in {"evidence.recorded", "artifact.recorded", "a2a.peer.responded"}
        ]
        self._propose(
            agent_id="reviewer",
            trigger=event,
            kind=CommandKind.REVIEW,
            payload={
                "assignment_id": event.payload["assignment_id"],
                "decision": "approved",
                "summary": "Evidence references exist, A2A depth is bounded, and rollback is explicit.",
                "evidence_refs": evidence_refs,
            },
            key=f"{event.run_id}:reviewer:{event.payload['assignment_id']}:review",
        )

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
            if item.type in {"evidence.recorded", "artifact.recorded", "review.recorded"}
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
        self._deliver("context.evolver", signal, delivery_id=f"evolution:{event.run_id}")
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

        compiled = self.context.compile(agent_id=agent_id, trigger=trigger, context_key=key)
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
        for event in self.kernel.events_for(decision):
            receiver: str | None = None
            if event.type == "assignment.created":
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
            if event.type == "run.succeeded":
                self._queue_dream(event)

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

    def _collect_evidence(self, assignment: Event) -> Event:
        operation_id = f"operation_{assignment.run_id}_evidence"
        started = self._append_deterministic(
            assignment,
            f"tool:{operation_id}:started",
            "operation.started",
            {
                "operation_id": operation_id,
                "assignment_id": assignment.payload["assignment_id"],
                "tool_name": "evidence.collect",
                "idempotency_key": operation_id,
            },
            source="tool.pipeline",
        )
        self._append_deterministic(
            assignment,
            f"tool:{operation_id}:requested",
            "tool.requested",
            {"operation_id": operation_id, "tool_name": "evidence.collect", "args": {"depth": "bounded"}},
            source="tool.pipeline",
            causation_id=started.id,
        )
        trace = "\n".join(
            f"trace[{index:03d}] verified deterministic repository fact and test boundary"
            for index in range(40)
        )
        completed = self._append_deterministic(
            assignment,
            f"tool:{operation_id}:completed",
            "tool.completed",
            {
                "operation_id": operation_id,
                "result": {
                    "name": "evidence.collect",
                    "ok": True,
                    "summary": "40 bounded observations collected",
                    "content": trace,
                },
            },
            source="tool.pipeline",
            causation_id=started.id,
        )
        self._append_deterministic(
            assignment,
            f"tool:{operation_id}:operation-completed",
            "operation.completed",
            {"operation_id": operation_id, "result_event_id": completed.id},
            source="tool.pipeline",
            causation_id=completed.id,
        )
        return completed

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
