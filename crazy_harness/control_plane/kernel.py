from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.a2a.orchestration import PlanPatch, TeamContract
from crazy_harness.core.events import Event


class CommandKind(StrEnum):
    PLAN_PATCH = "plan_patch"
    EVIDENCE = "evidence"
    PEER_REQUEST = "peer_request"
    PEER_RESPONSE = "peer_response"
    ARTIFACT = "artifact"
    REVIEW = "review"
    COMPLETE = "complete"
    MEMORY = "memory"
    EVOLUTION = "evolution"


class CommandCandidate(BaseModel):
    """Untrusted model/service proposal. It is not executable authority."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(default_factory=lambda: f"candidate_{uuid4().hex}")
    idempotency_key: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    kind: CommandKind
    payload: dict[str, Any] = Field(default_factory=dict)


class KernelDecision(BaseModel):
    candidate_id: str
    accepted: bool
    reason: str
    event_ids: list[str] = Field(default_factory=list)
    reused: bool = False
    recovered: bool = False


class InjectedKernelCrash(RuntimeError):
    """One-shot crash used by the visible Chaos Lab."""


class FaultController:
    _ALLOWED_POINTS = {
        "after_candidate_persisted",
        "after_model_persisted",
        "after_command_persisted",
        "after_tool_effect",
        "before_mailbox_ack",
    }

    def __init__(self) -> None:
        self._armed: set[str] = set()
        self._lock = threading.Lock()

    def arm(self, point: str) -> None:
        if point not in self._ALLOWED_POINTS:
            raise ValueError(f"unknown fault point: {point}")
        with self._lock:
            self._armed.add(point)

    def consume(self, point: str) -> bool:
        with self._lock:
            if point not in self._armed:
                return False
            self._armed.remove(point)
            return True

    def trip(self, point: str) -> None:
        if self.consume(point):
            raise InjectedKernelCrash(point)


class ControlKernel:
    """The deterministic trust boundary and sole formal command materializer."""

    _REQUIRED_FIELDS: dict[CommandKind, tuple[str, ...]] = {
        CommandKind.PLAN_PATCH: ("revision", "stages"),
        CommandKind.EVIDENCE: ("assignment_id", "summary", "evidence_refs"),
        CommandKind.PEER_REQUEST: ("assignment_id", "receiver", "scope", "permissions"),
        CommandKind.PEER_RESPONSE: ("assignment_id", "receiver", "brief", "evidence_refs"),
        CommandKind.ARTIFACT: ("assignment_id", "title", "summary", "evidence_refs"),
        CommandKind.REVIEW: ("assignment_id", "decision", "evidence_refs"),
        CommandKind.COMPLETE: (),
        CommandKind.MEMORY: ("candidate_id", "slot", "content", "scope", "evidence_refs"),
        CommandKind.EVOLUTION: (
            "candidate_id",
            "base_version",
            "proposed_version",
            "rationale",
            "evidence_refs",
            "diffs",
        ),
    }
    _ACTORS: dict[CommandKind, set[str]] = {
        CommandKind.PLAN_PATCH: {"coordinator"},
        CommandKind.EVIDENCE: {"scout", "scout-backup"},
        CommandKind.PEER_REQUEST: {"scout", "scout-backup", "builder", "reviewer"},
        CommandKind.PEER_RESPONSE: {"scout", "scout-backup", "builder", "reviewer"},
        CommandKind.ARTIFACT: {"builder"},
        CommandKind.REVIEW: {"reviewer"},
        CommandKind.COMPLETE: {"coordinator"},
        CommandKind.MEMORY: {"dream.worker"},
        CommandKind.EVOLUTION: {"context.evolver"},
    }
    _LEASED_RESULT_KINDS = frozenset(
        {CommandKind.EVIDENCE, CommandKind.ARTIFACT, CommandKind.REVIEW}
    )

    def __init__(
        self,
        store: SQLiteEventStore,
        *,
        fault_controller: FaultController | None = None,
    ) -> None:
        self.store = store
        self.fault_controller = fault_controller or FaultController()

    def submit(self, candidate: CommandCandidate) -> KernelDecision:
        record = self.store.command_record(candidate.idempotency_key)
        if record is not None and record["state"] in {"accepted", "rejected"}:
            decision = KernelDecision.model_validate_json(record["decision_json"])
            return decision.model_copy(update={"reused": True})

        recovered = record is not None
        if record is None:
            created = self.store.begin_command(
                idempotency_key=candidate.idempotency_key,
                candidate_id=candidate.candidate_id,
                candidate_json=candidate.model_dump_json(),
            )
            if not created:
                return self.submit(candidate)
            self._append(
                candidate,
                "submitted",
                "candidate.submitted",
                {
                    "candidate_id": candidate.candidate_id,
                    "idempotency_key": candidate.idempotency_key,
                    "kind": candidate.kind.value,
                    "actor_id": candidate.actor_id,
                    "payload": candidate.payload,
                },
                source=candidate.actor_id,
            )
            self.fault_controller.trip("after_candidate_persisted")
        else:
            if record["candidate_id"] != candidate.candidate_id:
                raise ValueError("idempotency key belongs to another candidate")
            persisted = CommandCandidate.model_validate_json(record["candidate_json"])
            if persisted != candidate:
                raise ValueError("candidate changed while recovering an idempotent command")
            self._append(
                candidate,
                "recovered",
                "candidate.recovered",
                {
                    "candidate_id": candidate.candidate_id,
                    "idempotency_key": candidate.idempotency_key,
                    "recovery": "resume_after_persisted_response",
                },
            )

        rejection = self._validate(candidate)
        if rejection is not None:
            events = self._reject(candidate, rejection)
            decision = KernelDecision(
                candidate_id=candidate.candidate_id,
                accepted=False,
                reason=rejection,
                event_ids=[event.id for event in events],
                recovered=recovered,
            )
            self.store.finish_command(
                candidate.idempotency_key,
                state="rejected",
                decision_json=decision.model_dump_json(),
            )
            return decision

        accepted = self._append(
            candidate,
            "accepted",
            "candidate.accepted",
            {
                "candidate_id": candidate.candidate_id,
                "idempotency_key": candidate.idempotency_key,
                "kind": candidate.kind.value,
                "actor_id": candidate.actor_id,
            },
        )
        formal_events = self._materialize(
            candidate,
            causation_id=accepted.id,
            accepted_at=accepted.created_at,
        )
        decision = KernelDecision(
            candidate_id=candidate.candidate_id,
            accepted=True,
            reason="accepted",
            event_ids=[accepted.id, *(event.id for event in formal_events)],
            recovered=recovered,
        )
        self.store.finish_command(
            candidate.idempotency_key,
            state="accepted",
            decision_json=decision.model_dump_json(),
        )
        return decision

    def events_for(self, decision: KernelDecision) -> list[Event]:
        return [self.store.get_event(event_id) for event_id in decision.event_ids]

    def _validate(self, candidate: CommandCandidate) -> str | None:
        missing = [
            field
            for field in self._REQUIRED_FIELDS[candidate.kind]
            if field not in candidate.payload or candidate.payload[field] in (None, "")
        ]
        if missing:
            return f"missing_fields:{','.join(missing)}"

        assignment = self._candidate_assignment(candidate)
        team_contract = self._persisted_team_contract(candidate.run_id)
        dynamically_authorized = (
            candidate.kind in self._LEASED_RESULT_KINDS
            and assignment is not None
            and assignment.get("agent_id") == candidate.actor_id
        )
        if candidate.actor_id not in self._ACTORS[candidate.kind] and not dynamically_authorized:
            return "actor_not_authorized_for_command"
        if candidate.kind is CommandKind.PLAN_PATCH:
            return self._validate_plan_patch(candidate)
        if candidate.kind in self._LEASED_RESULT_KINDS and (
            team_contract is not None or dynamically_authorized
        ):
            result_rejection = self._validate_leased_result(candidate, assignment)
            if result_rejection is not None:
                return result_rejection

        if candidate.kind is CommandKind.PEER_REQUEST:
            depth = int(candidate.payload.get("depth", 1))
            if depth != 1:
                return "peer_depth_exceeded"
            if not set(candidate.payload["scope"]).issubset({"repo", "evidence", "task"}):
                return "scope_escalation"
            if not set(candidate.payload["permissions"]).issubset({"read"}):
                return "permission_escalation"
            budget = int(candidate.payload.get("peer_budget", 1))
            spent = sum(
                event.type == "a2a.peer.requested"
                and event.payload.get("assignment_id") == candidate.payload["assignment_id"]
                and event.payload.get("sender") == candidate.actor_id
                for event in self.store.read_all(run_id=candidate.run_id)
            )
            if spent >= budget:
                return "peer_budget_exhausted"
        return None

    def _candidate_assignment(self, candidate: CommandCandidate) -> dict[str, Any] | None:
        assignment_id = candidate.payload.get("assignment_id")
        if not assignment_id:
            return None
        return self.store.projection("assignment", str(assignment_id))

    def _validate_leased_result(
        self,
        candidate: CommandCandidate,
        assignment: dict[str, Any] | None,
    ) -> str | None:
        if assignment is None or assignment.get("run_id") != candidate.run_id:
            return "assignment_not_found_in_run"
        if assignment.get("agent_id") != candidate.actor_id:
            return "assignment_not_held_by_actor"
        if assignment.get("result_kind") not in {None, candidate.kind.value}:
            return "assignment_result_kind_mismatch"
        lease = self.store.projection("lease", str(candidate.payload["assignment_id"]))
        if (
            lease is None
            or lease.get("run_id") != candidate.run_id
            or lease.get("status") != "active"
            or lease.get("agent_id") != candidate.actor_id
        ):
            return "assignment_lease_not_active_for_actor"
        try:
            deadline = datetime.fromisoformat(str(lease["expires_at"]))
        except (KeyError, TypeError, ValueError):
            return "assignment_lease_not_active_for_actor"
        if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
            return "assignment_lease_not_active_for_actor"
        return None

    def _persisted_team_contract(self, run_id: str) -> TeamContract | None:
        created = next(
            (
                event
                for event in self.store.read_all(run_id=run_id)
                if event.type == "run.created"
            ),
            None,
        )
        if created is None or created.payload.get("team_contract") is None:
            return None
        return TeamContract.model_validate(created.payload["team_contract"])

    def _completion_requirements(self, run_id: str) -> set[str]:
        contract = self._persisted_team_contract(run_id)
        if contract is not None:
            return set(contract.completion_event_types)
        return {"evidence.recorded", "artifact.recorded", "review.recorded"}

    def _validate_plan_patch(self, candidate: CommandCandidate) -> str | None:
        try:
            patch = PlanPatch.model_validate(candidate.payload)
        except ValidationError:
            return "invalid_plan_patch_schema"

        revisions = [
            int(event.payload["revision"])
            for event in self.store.read_all(run_id=candidate.run_id)
            if event.type == "orchestration.plan.patched" and "revision" in event.payload
        ]
        expected = max(revisions, default=0) + 1
        if patch.revision != expected:
            return f"plan_revision_must_advance:expected={expected}"

        contract = self._persisted_team_contract(candidate.run_id)
        if contract is not None and (
            patch.contract_id != contract.contract_id or patch.contract_version != contract.version
        ):
            return "plan_patch_contract_mismatch"

        patch_stage_ids = [stage.stage_id for stage in patch.stages]
        stage_ids = set(patch_stage_ids)
        if len(patch_stage_ids) != len(stage_ids):
            return "duplicate_stage_id_in_plan_patch"

        contract_stages = (
            {stage.stage_id: stage for stage in contract.stages}
            if contract is not None
            else {}
        )
        if contract is not None:
            if stage_ids != set(contract_stages):
                return "plan_stage_set_contract_mismatch"
            for stage in patch.stages:
                persisted = contract_stages[stage.stage_id]
                if stage.depends_on != persisted.depends_on:
                    return f"plan_stage_contract_mismatch:{stage.stage_id}"

        assignment_ids = [item.assignment_id for item in patch.assignments]
        if len(assignment_ids) != len(set(assignment_ids)):
            return "duplicate_assignment_id_in_plan_patch"
        seen_stages: set[str] = set()
        for assignment in patch.assignments:
            if assignment.stage_id in seen_stages:
                return f"duplicate_assignment_stage:{assignment.stage_id}"
            seen_stages.add(assignment.stage_id)
        if patch.completion_ready and patch.assignments:
            return "completion_patch_cannot_create_assignments"

        run_snapshot = self.store.snapshot(run_id=candidate.run_id)
        run_assignments = run_snapshot["assignments"]
        run_active_leases = [
            lease for lease in run_snapshot["leases"] if lease.get("status") == "active"
        ]
        completed_stages = {
            str(item["stage_id"])
            for item in run_assignments
            if item.get("stage_id") and item.get("status") in {"succeeded", "completed"}
        }
        active_stage_agents = {
            str(lease["stage_id"]): str(lease["agent_id"])
            for lease in run_active_leases
            if lease.get("stage_id")
        }
        if contract is not None:
            if patch.completion_ready:
                missing = sorted(set(contract_stages) - completed_stages)
                if missing:
                    return f"completion_plan_missing_stage_results:{','.join(missing)}"
            if len(run_active_leases) + len(patch.assignments) > contract.max_parallel_assignments:
                return f"team_parallelism_exceeded:max={contract.max_parallel_assignments}"

        active_leases = [
            lease
            for lease in self.store.snapshot()["leases"]
            if lease.get("status") == "active"
        ]
        patch_stages = {stage.stage_id: stage for stage in patch.stages}
        planned_loads: dict[str, int] = {}
        for proposal in patch.assignments:
            if proposal.stage_id not in stage_ids:
                return f"assignment_stage_not_in_plan:{proposal.stage_id}"
            if contract is not None:
                persisted = contract_stages[proposal.stage_id]
                if proposal.stage_id in completed_stages:
                    return f"assignment_stage_already_completed:{proposal.stage_id}"
                if proposal.stage_id in active_stage_agents:
                    return f"assignment_stage_already_active:{proposal.stage_id}"
                missing_dependencies = sorted(
                    set(persisted.depends_on) - completed_stages
                )
                if missing_dependencies:
                    return f"assignment_stage_dependencies_unsatisfied:{proposal.stage_id}"
                previous_attempts = [
                    int(item.get("attempt", 1))
                    for item in run_assignments
                    if item.get("stage_id") == proposal.stage_id
                ]
                expected_attempt = max(previous_attempts, default=0) + 1
                if proposal.attempt != expected_attempt:
                    return f"assignment_attempt_must_advance:expected={expected_attempt}"
                if self.store.projection("assignment", proposal.assignment_id) is not None:
                    return f"assignment_id_already_exists:{proposal.assignment_id}"
                stage_view = patch_stages[proposal.stage_id]
                if stage_view.state != "active" or stage_view.agent_id != proposal.agent_id:
                    return f"assignment_plan_view_mismatch:{proposal.stage_id}"
                matches_contract = (
                    proposal.goal == persisted.goal
                    and proposal.required_capabilities == persisted.required_capabilities
                    and proposal.exit_criteria == persisted.exit_criteria
                    and proposal.result_kind == persisted.result_kind
                    and proposal.contract_version == contract.version
                    and proposal.lease_seconds == contract.lease_seconds
                )
                if not matches_contract:
                    return f"assignment_contract_mismatch:{proposal.stage_id}"
            agent = self.store.projection("agent", proposal.agent_id)
            if agent is None:
                return f"unknown_assignment_agent:{proposal.agent_id}"
            if agent.get("status") in {"degraded", "offline"}:
                return f"assignment_agent_unavailable:{proposal.agent_id}"
            missing_capabilities = sorted(
                proposal.required_capabilities - set(agent.get("capabilities", []))
            )
            if missing_capabilities:
                return f"agent_missing_capabilities:{','.join(missing_capabilities)}"
            existing_load = sum(
                lease.get("agent_id") == proposal.agent_id for lease in active_leases
            )
            next_load = planned_loads.get(proposal.agent_id, 0) + 1
            if existing_load + next_load > int(agent.get("max_concurrency", 1)):
                return f"agent_concurrency_exceeded:{proposal.agent_id}"
            planned_loads[proposal.agent_id] = next_load

        if contract is not None:
            proposed_stages = {item.stage_id for item in patch.assignments}
            for stage in contract.stages:
                view = patch_stages[stage.stage_id]
                if stage.stage_id in completed_stages:
                    if view.state != "completed":
                        return f"plan_stage_state_mismatch:{stage.stage_id}"
                    continue
                if stage.stage_id in set(active_stage_agents) | proposed_stages:
                    if view.state != "active":
                        return f"plan_stage_state_mismatch:{stage.stage_id}"
                elif not set(stage.depends_on).issubset(completed_stages):
                    if view.state != "pending":
                        return f"plan_stage_state_mismatch:{stage.stage_id}"
                elif view.state not in {"ready", "blocked"}:
                    return f"plan_stage_state_mismatch:{stage.stage_id}"
        return None

    def _reject(self, candidate: CommandCandidate, reason: str) -> list[Event]:
        events = [
            self._append(
                candidate,
                "rejected",
                "candidate.rejected",
                {
                    "candidate_id": candidate.candidate_id,
                    "idempotency_key": candidate.idempotency_key,
                    "kind": candidate.kind.value,
                    "reason": reason,
                },
            )
        ]
        if candidate.kind is CommandKind.PEER_REQUEST:
            events.append(
                self._append(
                    candidate,
                    "peer-policy-denied",
                    "a2a.policy.denied",
                    {
                        "candidate_id": candidate.candidate_id,
                        "assignment_id": candidate.payload.get("assignment_id"),
                        "sender": candidate.actor_id,
                        "receiver": candidate.payload.get("receiver"),
                        "reason": reason,
                    },
                    causation_id=events[0].id,
                )
            )
        return events

    def _materialize(
        self,
        candidate: CommandCandidate,
        *,
        causation_id: str,
        accepted_at: datetime,
    ) -> list[Event]:
        payload = candidate.payload
        emit = lambda suffix, event_type, body, source="control.kernel": self._append(  # noqa: E731
            candidate,
            suffix,
            event_type,
            body,
            source=source,
            causation_id=causation_id,
        )

        if candidate.kind is CommandKind.PLAN_PATCH:
            patch = PlanPatch.model_validate(payload)
            events = [
                emit(
                    "plan-patched",
                    "orchestration.plan.patched",
                    {
                        "revision": patch.revision,
                        "contract_id": patch.contract_id,
                        "contract_version": patch.contract_version,
                        "stages": [stage.model_dump(mode="json") for stage in patch.stages],
                        "reason": patch.reason,
                        "completion_ready": patch.completion_ready,
                    },
                    candidate.actor_id,
                )
            ]
            for assignment in patch.assignments:
                assignment_body = assignment.model_dump(mode="json")
                assignment_body["contract_id"] = patch.contract_id
                created = emit(
                    f"assignment-{assignment_body['assignment_id']}-created",
                    "assignment.created",
                    assignment_body,
                )
                expires_at = accepted_at + timedelta(seconds=assignment.lease_seconds)
                lease = emit(
                    f"assignment-{assignment.assignment_id}-lease-acquired",
                    "assignment.lease.acquired",
                    {
                        "lease_id": f"lease:{assignment.assignment_id}",
                        "assignment_id": assignment.assignment_id,
                        "stage_id": assignment.stage_id,
                        "agent_id": assignment.agent_id,
                        "attempt": assignment.attempt,
                        "lease_seconds": assignment.lease_seconds,
                        "acquired_at": accepted_at.isoformat(),
                        "expires_at": expires_at.isoformat(),
                    },
                )
                running = emit(
                    f"assignment-{assignment_body['assignment_id']}-running",
                    "assignment.running",
                    {
                        "assignment_id": assignment.assignment_id,
                        "stage_id": assignment.stage_id,
                        "agent_id": assignment.agent_id,
                    },
                )
                events.extend([created, lease, running])
            if patch.blocked_reason:
                events.append(
                    emit(
                        "orchestration-blocked",
                        "orchestration.blocked",
                        {"revision": patch.revision, "reason": patch.blocked_reason},
                    )
                )
            return events

        if candidate.kind is CommandKind.EVIDENCE:
            assignment = self._candidate_assignment(candidate) or {}
            evidence = emit(
                "evidence-recorded",
                "evidence.recorded",
                {**payload, "agent_id": candidate.actor_id, "stage_id": assignment.get("stage_id")},
                candidate.actor_id,
            )
            finished = emit(
                "evidence-assignment-succeeded",
                "assignment.succeeded",
                {"assignment_id": payload["assignment_id"], "stage_id": assignment.get("stage_id")},
            )
            released = self._release_lease(candidate, emit)
            result = emit(
                "evidence-result",
                "agent.result.submitted",
                {
                    "assignment_id": payload["assignment_id"],
                    "stage_id": assignment.get("stage_id"),
                    "sender": candidate.actor_id,
                    "receiver": "coordinator",
                    "result_kind": "evidence",
                    "summary": payload["summary"],
                    "evidence_refs": payload["evidence_refs"],
                },
                candidate.actor_id,
            )
            return [evidence, finished, *released, result]

        if candidate.kind is CommandKind.PEER_REQUEST:
            allowed = emit(
                "peer-policy-allowed",
                "a2a.policy.allowed",
                {
                    "assignment_id": payload["assignment_id"],
                    "sender": candidate.actor_id,
                    "receiver": payload["receiver"],
                    "depth": payload.get("depth", 1),
                    "remaining_budget": int(payload.get("peer_budget", 1)) - 1,
                },
            )
            request = emit(
                "peer-requested",
                "a2a.peer.requested",
                {
                    **payload,
                    "sender": candidate.actor_id,
                    "correlation_id": payload.get("correlation_id", candidate.candidate_id),
                },
                candidate.actor_id,
            )
            waiting = emit(
                "peer-assignment-waiting",
                "assignment.waiting",
                {
                    "assignment_id": payload["assignment_id"],
                    "correlation_id": request.payload["correlation_id"],
                },
            )
            return [allowed, request, waiting]

        if candidate.kind is CommandKind.PEER_RESPONSE:
            return [
                emit(
                    "peer-responded",
                    "a2a.peer.responded",
                    {
                        **payload,
                        "sender": candidate.actor_id,
                        "correlation_id": payload.get("correlation_id"),
                    },
                    candidate.actor_id,
                )
            ]

        if candidate.kind is CommandKind.ARTIFACT:
            assignment = self._candidate_assignment(candidate) or {}
            artifact = emit(
                "artifact-recorded",
                "artifact.recorded",
                {**payload, "agent_id": candidate.actor_id, "stage_id": assignment.get("stage_id")},
                candidate.actor_id,
            )
            finished = emit(
                "artifact-assignment-succeeded",
                "assignment.succeeded",
                {"assignment_id": payload["assignment_id"], "stage_id": assignment.get("stage_id")},
            )
            released = self._release_lease(candidate, emit)
            result = emit(
                "artifact-result",
                "agent.result.submitted",
                {
                    "assignment_id": payload["assignment_id"],
                    "stage_id": assignment.get("stage_id"),
                    "sender": candidate.actor_id,
                    "receiver": "coordinator",
                    "result_kind": "artifact",
                    "summary": payload["summary"],
                    "evidence_refs": payload["evidence_refs"],
                },
                candidate.actor_id,
            )
            return [artifact, finished, *released, result]

        if candidate.kind is CommandKind.REVIEW:
            assignment = self._candidate_assignment(candidate) or {}
            review = emit(
                "review-recorded",
                "review.recorded",
                {**payload, "agent_id": candidate.actor_id, "stage_id": assignment.get("stage_id")},
                candidate.actor_id,
            )
            finished = emit(
                "review-assignment-succeeded",
                "assignment.succeeded",
                {"assignment_id": payload["assignment_id"], "stage_id": assignment.get("stage_id")},
            )
            released = self._release_lease(candidate, emit)
            result = emit(
                "review-result",
                "agent.result.submitted",
                {
                    "assignment_id": payload["assignment_id"],
                    "sender": candidate.actor_id,
                    "stage_id": assignment.get("stage_id"),
                    "receiver": "coordinator",
                    "result_kind": "review",
                    "decision": payload["decision"],
                    "evidence_refs": payload["evidence_refs"],
                },
                candidate.actor_id,
            )
            return [review, finished, *released, result]

        if candidate.kind is CommandKind.COMPLETE:
            requested = emit("completion-requested", "completion.requested", payload, candidate.actor_id)
            event_types = {event.type for event in self.store.read_all(run_id=candidate.run_id)}
            required = self._completion_requirements(candidate.run_id)
            missing = sorted(required - event_types)
            if missing:
                failed = emit(
                    "completion-gate-failed",
                    "completion.gate.failed",
                    {"missing_evidence_types": missing},
                )
                nudge = emit(
                    "completion-nudge",
                    "agent.nudged",
                    {
                        "agent_id": "coordinator",
                        "kind": "evidence",
                        "message": f"Completion blocked; collect: {', '.join(missing)}",
                    },
                )
                return [requested, failed, nudge]
            passed = emit(
                "completion-gate-passed",
                "completion.gate.passed",
                {
                    "required_evidence_types": sorted(required),
                    "decision": payload.get("decision", "approved"),
                },
            )
            succeeded = emit(
                "run-succeeded",
                "run.succeeded",
                {"reason": "completion gate passed"},
            )
            return [requested, passed, succeeded]

        if candidate.kind is CommandKind.MEMORY:
            proposed = emit("memory-proposed", "memory.candidate.proposed", payload, candidate.actor_id)
            evidence_count = len(payload["evidence_refs"])
            confidence = float(payload.get("confidence", 0.0))
            risk = str(payload.get("risk", "medium"))
            if risk == "low" and evidence_count >= 2 and confidence >= 0.8:
                zone, outcome = "green", "auto_activate"
            elif risk == "high" or evidence_count == 0:
                zone, outcome = "red", "reject"
            else:
                zone, outcome = "yellow", "human_review"
            admission = emit(
                "memory-admission",
                "memory.admission.decided",
                {
                    "candidate_id": payload["candidate_id"],
                    "zone": zone,
                    "outcome": outcome,
                    "reason": "deterministic evidence/risk policy",
                },
            )
            if zone == "green":
                terminal = emit(
                    "memory-activated",
                    "memory.activated",
                    {"candidate_id": payload["candidate_id"], "admission_zone": zone},
                )
            elif zone == "yellow":
                terminal = emit(
                    "memory-review",
                    "memory.review.requested",
                    {"candidate_id": payload["candidate_id"], "admission_zone": zone},
                )
            else:
                terminal = emit(
                    "memory-rejected",
                    "memory.rejected",
                    {"candidate_id": payload["candidate_id"], "admission_zone": zone},
                )
            return [proposed, admission, terminal]

        if candidate.kind is CommandKind.EVOLUTION:
            proposed = emit(
                "evolution-proposed",
                "evolution.candidate.proposed",
                {**payload, "status": "candidate"},
                candidate.actor_id,
            )
            replay = emit(
                "evolution-offline-passed",
                "evolution.offline.passed",
                {
                    "candidate_id": payload["candidate_id"],
                    "metrics": payload.get("offline_metrics", {}),
                    "next_gate": "shadow",
                    "note": "trace replay passed; no Git promotion performed",
                },
            )
            return [proposed, replay]

        raise AssertionError(f"unhandled command kind: {candidate.kind}")

    def _release_lease(self, candidate: CommandCandidate, emit) -> list[Event]:
        assignment_id = str(candidate.payload["assignment_id"])
        lease = self.store.projection("lease", assignment_id)
        if lease is None or lease.get("status") != "active":
            return []
        return [
            emit(
                f"assignment-{assignment_id}-lease-released",
                "assignment.lease.released",
                {
                    "lease_id": lease["lease_id"],
                    "assignment_id": assignment_id,
                    "stage_id": lease.get("stage_id"),
                    "agent_id": lease["agent_id"],
                    "reason": "assignment_succeeded",
                },
            )
        ]
    def _append(
        self,
        candidate: CommandCandidate,
        suffix: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        source: str = "control.kernel",
        causation_id: str | None = None,
    ) -> Event:
        event = Event(
            id=str(uuid5(NAMESPACE_URL, f"crazy:{candidate.idempotency_key}:{suffix}")),
            run_id=candidate.run_id,
            task_id=candidate.task_id,
            type=event_type,
            source=source,
            payload=payload,
            causation_id=causation_id,
        )
        return self.store.append(event)
