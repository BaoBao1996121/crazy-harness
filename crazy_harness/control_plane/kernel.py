from __future__ import annotations

import threading
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.control_plane.store import SQLiteEventStore
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
        CommandKind.EVIDENCE: {"scout"},
        CommandKind.PEER_REQUEST: {"scout", "builder", "reviewer"},
        CommandKind.PEER_RESPONSE: {"scout", "builder", "reviewer"},
        CommandKind.ARTIFACT: {"builder"},
        CommandKind.REVIEW: {"reviewer"},
        CommandKind.COMPLETE: {"coordinator"},
        CommandKind.MEMORY: {"dream.worker"},
        CommandKind.EVOLUTION: {"context.evolver"},
    }

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
        formal_events = self._materialize(candidate, causation_id=accepted.id)
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
        if candidate.actor_id not in self._ACTORS[candidate.kind]:
            return "actor_not_authorized_for_command"

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

    def _materialize(self, candidate: CommandCandidate, *, causation_id: str) -> list[Event]:
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
            events = [
                emit(
                    "plan-patched",
                    "orchestration.plan.patched",
                    {
                        "revision": payload["revision"],
                        "stages": payload["stages"],
                        "reason": payload.get("reason", "supervisor proposal accepted"),
                    },
                    candidate.actor_id,
                )
            ]
            assignment = payload.get("next_assignment")
            if assignment:
                assignment_body = dict(assignment)
                assignment_body.setdefault("state", "running")
                created = emit(
                    f"assignment-{assignment_body['assignment_id']}-created",
                    "assignment.created",
                    assignment_body,
                )
                running = emit(
                    f"assignment-{assignment_body['assignment_id']}-running",
                    "assignment.running",
                    {"assignment_id": assignment_body["assignment_id"]},
                )
                events.extend([created, running])
            return events

        if candidate.kind is CommandKind.EVIDENCE:
            evidence = emit(
                "evidence-recorded",
                "evidence.recorded",
                {**payload, "agent_id": candidate.actor_id},
                candidate.actor_id,
            )
            finished = emit(
                "evidence-assignment-succeeded",
                "assignment.succeeded",
                {"assignment_id": payload["assignment_id"]},
            )
            result = emit(
                "evidence-result",
                "agent.result.submitted",
                {
                    "assignment_id": payload["assignment_id"],
                    "sender": candidate.actor_id,
                    "receiver": "coordinator",
                    "result_kind": "evidence",
                    "summary": payload["summary"],
                    "evidence_refs": payload["evidence_refs"],
                },
                candidate.actor_id,
            )
            return [evidence, finished, result]

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
            artifact = emit(
                "artifact-recorded",
                "artifact.recorded",
                {**payload, "agent_id": candidate.actor_id},
                candidate.actor_id,
            )
            finished = emit(
                "artifact-assignment-succeeded",
                "assignment.succeeded",
                {"assignment_id": payload["assignment_id"]},
            )
            result = emit(
                "artifact-result",
                "agent.result.submitted",
                {
                    "assignment_id": payload["assignment_id"],
                    "sender": candidate.actor_id,
                    "receiver": "coordinator",
                    "result_kind": "artifact",
                    "summary": payload["summary"],
                    "evidence_refs": payload["evidence_refs"],
                },
                candidate.actor_id,
            )
            return [artifact, finished, result]

        if candidate.kind is CommandKind.REVIEW:
            review = emit(
                "review-recorded",
                "review.recorded",
                {**payload, "agent_id": candidate.actor_id},
                candidate.actor_id,
            )
            finished = emit(
                "review-assignment-succeeded",
                "assignment.succeeded",
                {"assignment_id": payload["assignment_id"]},
            )
            result = emit(
                "review-result",
                "agent.result.submitted",
                {
                    "assignment_id": payload["assignment_id"],
                    "sender": candidate.actor_id,
                    "receiver": "coordinator",
                    "result_kind": "review",
                    "decision": payload["decision"],
                    "evidence_refs": payload["evidence_refs"],
                },
                candidate.actor_id,
            )
            return [review, finished, result]

        if candidate.kind is CommandKind.COMPLETE:
            requested = emit("completion-requested", "completion.requested", payload, candidate.actor_id)
            event_types = {event.type for event in self.store.read_all(run_id=candidate.run_id)}
            required = {"evidence.recorded", "artifact.recorded", "review.recorded"}
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
