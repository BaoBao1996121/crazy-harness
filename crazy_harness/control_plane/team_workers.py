from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from crazy_harness.control_plane.kernel import (
    CommandCandidate,
    CommandKind,
    ControlKernel,
    KernelDecision,
)
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.a2a.orchestration import TeamContract
from crazy_harness.core.agents import AgentAction, AgentLoop, AssignmentContract
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.events import Event
from crazy_harness.core.models import FakeModelProvider, ModelProvider
from crazy_harness.core.runtime.mailbox import Delivery
from crazy_harness.taskpacks import ResidentDemoTeamTaskPack

Deliver = Callable[[str, Event, str], None]
RouteDecision = Callable[[KernelDecision], None]
LeaseGuard = Callable[[Event, str], bool]
FaultInjector = Callable[[str], None]


class TeamWorkerEngine:
    """Drive one canonical AgentLoop turn for every durable Team worker wake."""

    _WORKER_IDS = frozenset({"scout", "scout-backup", "builder", "reviewer"})

    def __init__(
        self,
        *,
        data_dir: Path,
        store: SQLiteEventStore,
        artifacts: ArtifactStore,
        kernel: ControlKernel,
        task_pack: ResidentDemoTeamTaskPack,
        deliver: Deliver,
        route_decision: RouteDecision,
        begin_leased_step: LeaseGuard,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.store = store
        self.artifacts = artifacts
        self.kernel = kernel
        self.task_pack = task_pack
        self.deliver = deliver
        self.route_decision = route_decision
        self.begin_leased_step = begin_leased_step
        self.fault_injector = fault_injector
        self._models: dict[str, ModelProvider] = {}
        self._loops: dict[str, AgentLoop] = {}

    def handle(self, delivery: Delivery, *, agent_id: str) -> None:
        trigger = delivery.event
        if trigger.type == "a2a.peer.requested":
            self._run_peer_turn(trigger, agent_id=agent_id)
            return
        if (
            trigger.type == "runtime.turn.ready"
            and trigger.payload.get("agent_run_kind") == "peer"
        ):
            request = self._peer_request(
                trigger.run_id,
                str(trigger.payload["correlation_id"]),
            )
            self._run_peer_turn(request, agent_id=agent_id)
            return
        if trigger.type not in {
            "assignment.created",
            "runtime.turn.ready",
            "a2a.peer.responded",
        }:
            return

        assignment_id = str(trigger.payload.get("assignment_id", ""))
        assignment = self._assignment_event(trigger.run_id, assignment_id)
        if assignment is None:
            self._append(
                trigger,
                f"team-missing-assignment:{trigger.id}",
                "assignment.delivery.stale",
                {
                    "assignment_id": assignment_id,
                    "agent_id": agent_id,
                    "reason": "assignment_event_not_found",
                },
                source="runtime.team",
            )
            return
        if not self.begin_leased_step(trigger, agent_id):
            return
        if trigger.type == "a2a.peer.responded":
            self._mirror_peer_response(trigger, assignment)

        self._ensure_assignment_seed(assignment)
        loop = self._assignment_loop(assignment)
        loop.run_once()
        self._advance_assignment(assignment, loop)

    def _assignment_loop(self, assignment: Event) -> AgentLoop:
        assignment_id = str(assignment.payload["assignment_id"])
        agent_run_id = self.task_pack.assignment_agent_run_id(assignment_id)
        existing = self._loops.get(agent_run_id)
        if existing is not None:
            return existing

        child_events = self.store.read_all(task_id=agent_run_id)
        seed = next(
            event for event in child_events if event.type == "agent.run.created"
        )
        contract = AssignmentContract.model_validate(seed.payload["contract"])
        stage_id = str(assignment.payload["stage_id"])
        peer_receiver = self._evidence_agent(assignment.run_id)
        responses = self.task_pack.scripted_assignment_responses(
            stage_id,
            peer_receiver=peer_receiver,
        )
        model = self._scripted_model(agent_run_id, responses)
        run = self.store.projection("run", assignment.run_id) or {}
        loop = self.task_pack.build_assignment_loop(
            run_id=assignment.run_id,
            root_task_id=assignment.task_id,
            task_id=agent_run_id,
            assignment_id=assignment_id,
            stage_id=stage_id,
            agent_id=str(assignment.payload["agent_id"]),
            brief=(
                f"Root task: {run.get('brief', '')}\n"
                f"Assignment: {assignment.payload.get('goal', contract.goal)}"
            ),
            model=model,
            event_log=self.store,
            artifact_store=self.artifacts,
            ledger_path=self._ledger_path(agent_run_id),
            assignment_contract=contract,
            message_handler=lambda action, turn_id: self._handle_peer_action(
                assignment,
                action,
                turn_id,
            ),
            fault_injector=self.fault_injector,
        )
        self._loops[agent_run_id] = loop
        return loop

    def _ensure_assignment_seed(self, assignment: Event) -> Event:
        assignment_id = str(assignment.payload["assignment_id"])
        agent_run_id = self.task_pack.assignment_agent_run_id(assignment_id)
        existing = next(
            (
                event
                for event in self.store.read_all(task_id=agent_run_id)
                if event.type == "agent.run.created"
            ),
            None,
        )
        if existing is not None:
            return existing
        stage_id = str(assignment.payload["stage_id"])
        contract_payload = assignment.payload.get("contract")
        contract = (
            AssignmentContract.model_validate(contract_payload)
            if contract_payload is not None
            else self.task_pack.assignment_contract(stage_id)
        )
        public_refs = [
            event.id
            for event in self.store.read_all(run_id=assignment.run_id)
            if event.type
            in {"evidence.recorded", "artifact.recorded", "review.recorded"}
        ]
        return self.store.append(
            Event(
                id=self._event_id(assignment.run_id, f"agent-run-seed:{agent_run_id}"),
                run_id=assignment.run_id,
                task_id=agent_run_id,
                type="agent.run.created",
                source="runtime.team",
                payload={
                    "agent_run_id": agent_run_id,
                    "agent_run_kind": "assignment",
                    "root_task_id": assignment.task_id,
                    "assignment_id": assignment_id,
                    "stage_id": stage_id,
                    "result_kind": assignment.payload["result_kind"],
                    "agent_id": assignment.payload["agent_id"],
                    "goal": assignment.payload["goal"],
                    "exit_criteria": assignment.payload.get("exit_criteria", []),
                    "contract": contract.model_dump(mode="json"),
                    "public_event_refs": public_refs,
                    "context_sharing": "summary_schema_refs_only",
                },
                causation_id=assignment.id,
            )
        )

    def _advance_assignment(self, assignment: Event, loop: AgentLoop) -> None:
        assignment_id = str(assignment.payload["assignment_id"])
        agent_run_id = self.task_pack.assignment_agent_run_id(assignment_id)
        events = self.store.read_all(task_id=agent_run_id)
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.type in {"agent.submitted", "agent.stopped", "agent.failed"}
            ),
            None,
        )
        if terminal is not None:
            if terminal.type == "agent.submitted":
                self._promote_assignment_result(assignment, terminal, events)
            else:
                self._fail_assignment(
                    assignment,
                    reason=str(terminal.payload.get("reason", terminal.type)),
                    causation_id=terminal.id,
                )
            return
        if AgentLoop._has_active_wait(events) or self._has_unresolved_unknown(events):
            return

        completed_turns = sum(event.type == "model.completed" for event in events)
        max_turns = (
            loop.assignment_contract.budgets.turns if loop.assignment_contract else 20
        )
        if max_turns is not None and completed_turns >= max_turns:
            self._fail_assignment(
                assignment,
                reason=f"turn budget exhausted after {completed_turns} turns",
                causation_id=events[-1].id,
            )
            return
        ready = self._append(
            events[-1],
            f"assignment-turn-ready:{agent_run_id}:{completed_turns + 1}",
            "runtime.turn.ready",
            {
                "agent_id": assignment.payload["agent_id"],
                "agent_run_id": agent_run_id,
                "agent_run_kind": "assignment",
                "root_task_id": assignment.task_id,
                "assignment_id": assignment_id,
                "next_turn": completed_turns + 1,
            },
            source="runtime.scheduler",
        )
        self.deliver(
            str(assignment.payload["agent_id"]),
            ready,
            f"team:{assignment_id}:turn:{completed_turns + 1}",
        )

    def _promote_assignment_result(
        self,
        assignment: Event,
        submission: Event,
        child_events: list[Event],
    ) -> None:
        assignment_id = str(assignment.payload["assignment_id"])
        agent_run_id = self.task_pack.assignment_agent_run_id(assignment_id)
        artifact = dict(submission.payload.get("artifact") or {})
        evidence_refs = self._dedupe(
            event.id
            for event in child_events
            if event.type in {"tool.completed", "a2a.peer.responded"}
        )
        common = {
            "assignment_id": assignment_id,
            "agent_run_id": agent_run_id,
            "submission_event_id": submission.id,
            "evidence_refs": evidence_refs,
        }
        result_kind = CommandKind(str(assignment.payload["result_kind"]))
        if result_kind is CommandKind.EVIDENCE:
            payload = {**common, "summary": artifact["summary"]}
        elif result_kind is CommandKind.ARTIFACT:
            payload = {
                **common,
                "title": artifact["title"],
                "summary": artifact["summary"],
                "content": artifact["content"],
            }
        elif result_kind is CommandKind.REVIEW:
            payload = {
                **common,
                "decision": artifact["decision"],
                "summary": artifact["summary"],
            }
        else:
            raise ValueError(f"unsupported Team result kind: {result_kind}")

        key = f"{assignment.run_id}:{assignment.payload['agent_id']}:{assignment_id}:agent-loop-result"
        decision = self.kernel.submit(
            CommandCandidate(
                candidate_id=f"candidate_{uuid5(NAMESPACE_URL, key).hex}",
                idempotency_key=key,
                run_id=assignment.run_id,
                task_id=assignment.task_id,
                actor_id=str(assignment.payload["agent_id"]),
                kind=result_kind,
                payload=payload,
            )
        )
        if self.fault_injector is not None:
            self.fault_injector("after_command_finalized")
        self.route_decision(decision)
        promotion = self._append(
            submission,
            f"assignment-result-promotion:{assignment_id}",
            "agent.result.promoted" if decision.accepted else "agent.result.rejected",
            {
                "assignment_id": assignment_id,
                "agent_run_id": agent_run_id,
                "candidate_id": decision.candidate_id,
                "accepted": decision.accepted,
                "reason": decision.reason,
            },
            source="runtime.team.adapter",
        )
        if not decision.accepted:
            self._fail_assignment(
                assignment,
                reason=f"assignment result rejected: {decision.reason}",
                causation_id=promotion.id,
            )

    def _handle_peer_action(
        self,
        assignment: Event,
        action: AgentAction,
        turn_id: str,
    ) -> dict[str, object]:
        if action.type != "send_message" or action.receiver not in self._WORKER_IDS:
            raise PermissionError("peer receiver is not an authorized Team worker")
        allowed_fields = {"brief", "scope", "permissions", "depth", "peer_budget"}
        extra = set(action.message) - allowed_fields
        if extra:
            raise PermissionError(
                f"peer message contains non-public fields: {sorted(extra)}"
            )
        assignment_id = str(assignment.payload["assignment_id"])
        actor_id = str(assignment.payload["agent_id"])
        key = f"{assignment.run_id}:{actor_id}:{assignment_id}:peer-request:{turn_id}"
        candidate = CommandCandidate(
            candidate_id=f"candidate_{uuid5(NAMESPACE_URL, key).hex}",
            idempotency_key=key,
            run_id=assignment.run_id,
            task_id=assignment.task_id,
            actor_id=actor_id,
            kind=CommandKind.PEER_REQUEST,
            payload={
                "assignment_id": assignment_id,
                "receiver": action.receiver,
                "brief": str(action.message.get("brief", action.reason)),
                "scope": action.message.get("scope", ["evidence"]),
                "permissions": action.message.get("permissions", ["read"]),
                "depth": action.message.get("depth", 1),
                "peer_budget": action.message.get("peer_budget", 1),
            },
        )
        decision = self.kernel.submit(candidate)
        if not decision.accepted:
            raise PermissionError(decision.reason)
        self.route_decision(decision)
        request = next(
            event
            for event in self.kernel.events_for(decision)
            if event.type == "a2a.peer.requested"
        )
        return {
            "correlation_id": request.payload["correlation_id"],
            "request_event_id": request.id,
            "sharing": "summary_schema_refs_only",
        }

    def _run_peer_turn(self, request: Event, *, agent_id: str) -> None:
        if request.payload.get("receiver") != agent_id:
            self._append(
                request,
                f"peer-delivery-stale:{request.id}:{agent_id}",
                "a2a.delivery.stale",
                {"agent_id": agent_id, "reason": "receiver_mismatch"},
                source="runtime.team",
            )
            return
        correlation_id = str(request.payload["correlation_id"])
        self._ensure_peer_seed(request, agent_id=agent_id)
        loop = self._peer_loop(request, agent_id=agent_id)
        loop.run_once()
        self._advance_peer(
            request, agent_id=agent_id, correlation_id=correlation_id, loop=loop
        )

    def _peer_loop(self, request: Event, *, agent_id: str) -> AgentLoop:
        correlation_id = str(request.payload["correlation_id"])
        agent_run_id = self.task_pack.peer_agent_run_id(correlation_id)
        existing = self._loops.get(agent_run_id)
        if existing is not None:
            return existing
        child_events = self.store.read_all(task_id=agent_run_id)
        seed = next(
            event for event in child_events if event.type == "agent.run.created"
        )
        contract = AssignmentContract.model_validate(seed.payload["contract"])
        model = self._scripted_model(
            agent_run_id,
            self.task_pack.scripted_peer_responses(),
        )
        loop = self.task_pack.build_peer_loop(
            run_id=request.run_id,
            root_task_id=request.task_id,
            task_id=agent_run_id,
            correlation_id=correlation_id,
            agent_id=agent_id,
            brief=str(request.payload.get("brief", "bounded peer reconciliation")),
            model=model,
            event_log=self.store,
            artifact_store=self.artifacts,
            ledger_path=self._ledger_path(agent_run_id),
            assignment_contract=contract,
            fault_injector=self.fault_injector,
        )
        self._loops[agent_run_id] = loop
        return loop

    def _ensure_peer_seed(self, request: Event, *, agent_id: str) -> Event:
        correlation_id = str(request.payload["correlation_id"])
        agent_run_id = self.task_pack.peer_agent_run_id(correlation_id)
        existing = next(
            (
                event
                for event in self.store.read_all(task_id=agent_run_id)
                if event.type == "agent.run.created"
            ),
            None,
        )
        if existing is not None:
            return existing
        contract = self._peer_contract_for_run(request.run_id)
        return self.store.append(
            Event(
                id=self._event_id(request.run_id, f"agent-run-seed:{agent_run_id}"),
                run_id=request.run_id,
                task_id=agent_run_id,
                type="agent.run.created",
                source="runtime.team",
                payload={
                    "agent_run_id": agent_run_id,
                    "agent_run_kind": "peer",
                    "root_task_id": request.task_id,
                    "assignment_id": request.payload["assignment_id"],
                    "correlation_id": correlation_id,
                    "agent_id": agent_id,
                    "request": {
                        "sender": request.payload["sender"],
                        "brief": request.payload.get("brief"),
                        "scope": request.payload.get("scope", []),
                        "permissions": request.payload.get("permissions", []),
                    },
                    "contract": contract.model_dump(mode="json"),
                    "context_sharing": "summary_schema_refs_only",
                },
                causation_id=request.id,
            )
        )

    def _peer_contract_for_run(self, run_id: str) -> AssignmentContract:
        created = next(
            (
                event
                for event in self.store.read_all(run_id=run_id)
                if event.type == "run.created"
            ),
            None,
        )
        if created is not None and created.payload.get("team_contract") is not None:
            team_contract = TeamContract.model_validate(
                created.payload["team_contract"]
            )
            if team_contract.peer_contract is not None:
                return team_contract.peer_contract
        return self.task_pack.peer_contract()

    def _advance_peer(
        self,
        request: Event,
        *,
        agent_id: str,
        correlation_id: str,
        loop: AgentLoop,
    ) -> None:
        agent_run_id = self.task_pack.peer_agent_run_id(correlation_id)
        events = self.store.read_all(task_id=agent_run_id)
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.type in {"agent.submitted", "agent.stopped", "agent.failed"}
            ),
            None,
        )
        if terminal is not None:
            if terminal.type == "agent.submitted":
                self._promote_peer_response(
                    request, terminal, events, agent_id=agent_id
                )
            else:
                terminal_state = terminal.type.removeprefix("agent.")
                terminal_reason = str(terminal.payload.get("reason", terminal.type))
                self._fail_requesting_assignment(
                    request,
                    reason=f"peer AgentRun {terminal_state}: {terminal_reason}",
                    causation_id=terminal.id,
                )
            return
        if AgentLoop._has_active_wait(events) or self._has_unresolved_unknown(events):
            return
        completed_turns = sum(event.type == "model.completed" for event in events)
        max_turns = (
            loop.assignment_contract.budgets.turns if loop.assignment_contract else 20
        )
        if max_turns is not None and completed_turns >= max_turns:
            self._fail_requesting_assignment(
                request,
                reason=f"peer turn budget exhausted after {completed_turns} turns",
                causation_id=events[-1].id,
            )
            return
        ready = self._append(
            events[-1],
            f"peer-turn-ready:{agent_run_id}:{completed_turns + 1}",
            "runtime.turn.ready",
            {
                "agent_id": agent_id,
                "agent_run_id": agent_run_id,
                "agent_run_kind": "peer",
                "root_task_id": request.task_id,
                "assignment_id": request.payload["assignment_id"],
                "correlation_id": correlation_id,
                "next_turn": completed_turns + 1,
            },
            source="runtime.scheduler",
        )
        self.deliver(
            agent_id,
            ready,
            f"team:peer:{correlation_id}:turn:{completed_turns + 1}",
        )

    def _promote_peer_response(
        self,
        request: Event,
        submission: Event,
        child_events: list[Event],
        *,
        agent_id: str,
    ) -> None:
        correlation_id = str(request.payload["correlation_id"])
        agent_run_id = self.task_pack.peer_agent_run_id(correlation_id)
        artifact = dict(submission.payload.get("artifact") or {})
        refs: list[str] = [
            event.id for event in child_events if event.type == "tool.completed"
        ]
        for event in child_events:
            if event.type != "tool.completed":
                continue
            result = event.payload.get("result", {})
            try:
                output = json.loads(str(result.get("output", "{}")))
            except (TypeError, json.JSONDecodeError):
                continue
            for ref in output.get("evidence_refs", []):
                try:
                    self.store.get_event(str(ref))
                except KeyError:
                    continue
                refs.append(str(ref))
        key = f"{request.run_id}:{agent_id}:peer-response:{correlation_id}"
        decision = self.kernel.submit(
            CommandCandidate(
                candidate_id=f"candidate_{uuid5(NAMESPACE_URL, key).hex}",
                idempotency_key=key,
                run_id=request.run_id,
                task_id=request.task_id,
                actor_id=agent_id,
                kind=CommandKind.PEER_RESPONSE,
                payload={
                    "assignment_id": request.payload["assignment_id"],
                    "receiver": request.payload["sender"],
                    "brief": artifact["brief"],
                    "evidence_refs": self._dedupe(refs),
                    "correlation_id": correlation_id,
                    "agent_run_id": agent_run_id,
                    "submission_event_id": submission.id,
                },
            )
        )
        if self.fault_injector is not None:
            self.fault_injector("after_command_finalized")
        self.route_decision(decision)
        promotion = self._append(
            submission,
            f"peer-result-promotion:{correlation_id}",
            "agent.result.promoted" if decision.accepted else "agent.result.rejected",
            {
                "agent_run_id": agent_run_id,
                "candidate_id": decision.candidate_id,
                "accepted": decision.accepted,
                "reason": decision.reason,
            },
            source="runtime.team.adapter",
        )
        if not decision.accepted:
            self._fail_requesting_assignment(
                request,
                reason=f"peer result rejected: {decision.reason}",
                causation_id=promotion.id,
            )

    def _mirror_peer_response(self, response: Event, assignment: Event) -> Event:
        assignment_id = str(assignment.payload["assignment_id"])
        agent_run_id = self.task_pack.assignment_agent_run_id(assignment_id)
        return self.store.append(
            Event(
                id=self._event_id(
                    response.run_id,
                    f"peer-response-mirror:{response.id}:{agent_run_id}",
                ),
                run_id=response.run_id,
                task_id=agent_run_id,
                type="a2a.peer.responded",
                source="runtime.a2a.bridge",
                payload={
                    "assignment_id": assignment_id,
                    "correlation_id": response.payload["correlation_id"],
                    "sender": response.payload["sender"],
                    "brief": response.payload["brief"],
                    "evidence_refs": response.payload["evidence_refs"],
                    "peer_agent_run_id": response.payload.get("agent_run_id"),
                    "root_event_id": response.id,
                    "sharing": "summary_schema_refs_only",
                },
                causation_id=response.id,
            )
        )

    def _scripted_model(self, agent_run_id: str, responses: list[str]) -> ModelProvider:
        existing = self._models.get(agent_run_id)
        if existing is not None:
            return existing
        completed = sum(
            event.type == "model.completed"
            for event in self.store.read_all(task_id=agent_run_id)
        )
        if completed > len(responses):
            raise RuntimeError(
                f"persisted model cursor exceeds scripted responses: {agent_run_id}"
            )
        model = FakeModelProvider(responses[completed:])
        self._models[agent_run_id] = model
        return model

    def _fail_requesting_assignment(
        self,
        request: Event,
        *,
        reason: str,
        causation_id: str,
    ) -> None:
        assignment_id = str(request.payload["assignment_id"])
        assignment = self._assignment_event(request.run_id, assignment_id)
        if assignment is None:
            self._append(
                request,
                f"peer-failure-unroutable:{request.id}",
                "a2a.peer.failure.unroutable",
                {
                    "assignment_id": assignment_id,
                    "correlation_id": request.payload.get("correlation_id"),
                    "reason": reason,
                },
                source="runtime.team",
                causation_id=causation_id,
            )
            return
        self._fail_assignment(assignment, reason=reason, causation_id=causation_id)

    def _fail_assignment(
        self, assignment: Event, *, reason: str, causation_id: str
    ) -> None:
        assignment_id = str(assignment.payload["assignment_id"])
        lease = self.store.projection("lease", assignment_id)
        failed = self._append(
            assignment,
            f"assignment-failed:{assignment_id}",
            "assignment.failed",
            {
                "assignment_id": assignment_id,
                "stage_id": assignment.payload["stage_id"],
                "agent_id": assignment.payload["agent_id"],
                "reason": reason,
            },
            source="runtime.team",
            causation_id=causation_id,
        )
        if lease is not None and lease.get("status") == "active":
            self._append(
                failed,
                f"assignment-lease-released-after-failure:{assignment_id}",
                "assignment.lease.released",
                {
                    "lease_id": lease["lease_id"],
                    "assignment_id": assignment_id,
                    "stage_id": assignment.payload["stage_id"],
                    "agent_id": assignment.payload["agent_id"],
                    "released_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "agent_run_failed",
                },
                source="runtime.team",
            )
        nudge = self._append(
            failed,
            f"assignment-failure-nudge:{assignment_id}",
            "agent.nudged",
            {
                "agent_id": "coordinator",
                "assignment_id": assignment_id,
                "kind": "assignment_failure",
                "message": reason,
            },
            source="runtime.team",
        )
        self.deliver("coordinator", nudge, f"team:failure:{assignment_id}")

    def _assignment_event(self, run_id: str, assignment_id: str) -> Event | None:
        return next(
            (
                event
                for event in self.store.read_all(run_id=run_id)
                if event.type == "assignment.created"
                and event.payload.get("assignment_id") == assignment_id
            ),
            None,
        )

    def _peer_request(self, run_id: str, correlation_id: str) -> Event:
        request = next(
            (
                event
                for event in reversed(self.store.read_all(run_id=run_id))
                if event.type == "a2a.peer.requested"
                and event.payload.get("correlation_id") == correlation_id
            ),
            None,
        )
        if request is None:
            raise RuntimeError(f"peer request is missing: {correlation_id}")
        return request

    def _evidence_agent(self, run_id: str) -> str:
        return str(
            next(
                (
                    event.payload.get("agent_id")
                    for event in reversed(self.store.read_all(run_id=run_id))
                    if event.type == "evidence.recorded"
                ),
                "scout",
            )
        )

    def _ledger_path(self, agent_run_id: str) -> Path:
        token = uuid5(NAMESPACE_URL, f"crazy:ledger:{agent_run_id}").hex
        return self.data_dir / "operations" / f"{token}.jsonl"

    def _append(
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
                id=self._event_id(identity.run_id, key),
                run_id=identity.run_id,
                task_id=identity.task_id,
                type=event_type,
                source=source,
                payload=payload,
                causation_id=causation_id or identity.id,
            )
        )

    @staticmethod
    def _event_id(run_id: str, key: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"crazy:{run_id}:{key}"))

    @staticmethod
    def _has_unresolved_unknown(events: list[Event]) -> bool:
        unknown = {
            event.payload.get("operation_id")
            for event in events
            if event.type == "operation.unknown"
        }
        reconciled = {
            event.payload.get("operation_id")
            for event in events
            if event.type == "operation.reconciled"
        }
        return bool(unknown - reconciled)

    @staticmethod
    def _dedupe(values) -> list[str]:
        return list(dict.fromkeys(str(value) for value in values if value))
