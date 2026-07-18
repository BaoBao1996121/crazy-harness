from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from crazy_harness.control_plane.store import (
    CommandPreconditionFailed,
    SQLiteEventStore,
)
from crazy_harness.core.a2a.orchestration import PlanPatch, TeamContract
from crazy_harness.core.agents.contracts import AssignmentContract
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


class _PeerRequestPayload(BaseModel):
    """Shape validation only; authority and limits still come from durable facts."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str = Field(min_length=1)
    receiver: str = Field(min_length=1)
    brief: str = ""
    scope: list[str]
    permissions: list[str]
    depth: int = Field(default=1, ge=1, strict=True)
    peer_budget: int = Field(default=1, ge=1, strict=True)
    correlation_id: str | None = None


class _PeerResponsePayload(BaseModel):
    """A local peer response must identify the child run that produced it."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str = Field(min_length=1)
    receiver: str = Field(min_length=1)
    brief: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    agent_run_id: str = Field(min_length=1)
    submission_event_id: str = Field(min_length=1)


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
        "during_command_commit",
        "after_command_finalized",
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
        CommandKind.PEER_RESPONSE: (
            "assignment_id",
            "receiver",
            "brief",
            "evidence_refs",
            "correlation_id",
        ),
        CommandKind.ARTIFACT: ("assignment_id", "title", "summary", "evidence_refs"),
        CommandKind.REVIEW: ("assignment_id", "decision", "evidence_refs"),
        CommandKind.COMPLETE: (),
        CommandKind.MEMORY: (
            "candidate_id",
            "slot",
            "content",
            "scope",
            "evidence_refs",
        ),
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
    _EVIDENCE_REFERENCE_KINDS = frozenset(
        {
            CommandKind.EVIDENCE,
            CommandKind.ARTIFACT,
            CommandKind.REVIEW,
            CommandKind.PEER_RESPONSE,
            CommandKind.MEMORY,
            CommandKind.EVOLUTION,
        }
    )
    _PEER_REQUEST_LIMIT = 1
    _FORMAL_EVIDENCE_TYPES = frozenset(
        {"evidence.recorded", "artifact.recorded", "review.recorded"}
    )
    _ALLOWED_EVIDENCE_TYPES: dict[CommandKind, frozenset[str]] = {
        CommandKind.EVIDENCE: frozenset({"tool.completed"}),
        CommandKind.ARTIFACT: frozenset(
            {
                "tool.completed",
                "a2a.peer.responded",
                "evidence.recorded",
            }
        ),
        CommandKind.REVIEW: frozenset(
            {
                "tool.completed",
                "a2a.peer.responded",
                "evidence.recorded",
                "artifact.recorded",
            }
        ),
        CommandKind.PEER_RESPONSE: frozenset(
            {
                "tool.completed",
                "evidence.recorded",
                "artifact.recorded",
                "review.recorded",
            }
        ),
        CommandKind.MEMORY: _FORMAL_EVIDENCE_TYPES | {"dream.evidence.frozen"},
        CommandKind.EVOLUTION: _FORMAL_EVIDENCE_TYPES | {"dream.evidence.frozen"},
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
                raise ValueError(
                    "candidate changed while recovering an idempotent command"
                )
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
            return self._commit_rejection(candidate, rejection, recovered=recovered)

        accepted = self._event(
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
        try:
            self.store.commit_command(
                candidate.idempotency_key,
                state="accepted",
                decision_json=decision.model_dump_json(),
                events=[accepted, *formal_events],
                after_event=self._trip_during_command_commit,
                precondition=lambda: self._validate(candidate),
            )
        except CommandPreconditionFailed as exc:
            return self._commit_rejection(
                candidate,
                exc.reason,
                recovered=recovered,
            )
        return decision

    def _commit_rejection(
        self,
        candidate: CommandCandidate,
        reason: str,
        *,
        recovered: bool,
    ) -> KernelDecision:
        events = self._reject(candidate, reason)
        decision = KernelDecision(
            candidate_id=candidate.candidate_id,
            accepted=False,
            reason=reason,
            event_ids=[event.id for event in events],
            recovered=recovered,
        )
        self.store.commit_command(
            candidate.idempotency_key,
            state="rejected",
            decision_json=decision.model_dump_json(),
            events=events,
            after_event=self._trip_during_command_commit,
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

        schema_rejection = self._validate_payload_schema(candidate)
        if schema_rejection is not None:
            return schema_rejection

        assignment = self._candidate_assignment(candidate)
        team_contract = self._persisted_team_contract(candidate.run_id)
        dynamically_authorized = (
            candidate.kind in self._LEASED_RESULT_KINDS
            and assignment is not None
            and assignment.get("agent_id") == candidate.actor_id
        )
        if (
            candidate.actor_id not in self._ACTORS[candidate.kind]
            and not dynamically_authorized
        ):
            return "actor_not_authorized_for_command"
        if candidate.kind is CommandKind.PLAN_PATCH:
            return self._validate_plan_patch(candidate)
        if candidate.kind in self._LEASED_RESULT_KINDS and (
            team_contract is not None or dynamically_authorized
        ):
            result_rejection = self._validate_leased_result(candidate, assignment)
            if result_rejection is not None:
                return result_rejection
            if team_contract is not None:
                reference_rejection = self._validate_evidence_references(candidate)
                if reference_rejection is not None:
                    return reference_rejection
                provenance_rejection = self._validate_assignment_agent_run(
                    candidate, assignment
                )
                if provenance_rejection is not None:
                    return provenance_rejection

        if candidate.kind is CommandKind.PEER_REQUEST:
            depth = int(candidate.payload.get("depth", 1))
            if depth != 1:
                return "peer_depth_exceeded"
            if not set(candidate.payload["scope"]).issubset(
                {"repo", "evidence", "task"}
            ):
                return "scope_escalation"
            if not set(candidate.payload["permissions"]).issubset({"read"}):
                return "permission_escalation"
            if int(candidate.payload.get("peer_budget", 1)) != self._PEER_REQUEST_LIMIT:
                return "peer_budget_escalation"
            if team_contract is not None:
                authority_rejection = self._validate_peer_request_authority(
                    candidate,
                    assignment,
                )
                if authority_rejection is not None:
                    return authority_rejection
            spent = sum(
                event.type == "a2a.peer.requested"
                and event.payload.get("assignment_id")
                == candidate.payload["assignment_id"]
                and event.payload.get("sender") == candidate.actor_id
                for event in self.store.read_all(run_id=candidate.run_id)
            )
            if spent >= self._PEER_REQUEST_LIMIT:
                return "peer_budget_exhausted"
        if candidate.kind is CommandKind.PEER_RESPONSE and team_contract is not None:
            authority_rejection = self._validate_peer_response_authority(candidate)
            if authority_rejection is not None:
                return authority_rejection
        if (
            team_contract is not None
            and candidate.kind in self._EVIDENCE_REFERENCE_KINDS
            and candidate.kind not in self._LEASED_RESULT_KINDS
        ):
            return self._validate_evidence_references(candidate)
        return None

    @staticmethod
    def _validate_payload_schema(candidate: CommandCandidate) -> str | None:
        try:
            if candidate.kind is CommandKind.PEER_REQUEST:
                _PeerRequestPayload.model_validate(candidate.payload)
            elif candidate.kind is CommandKind.PEER_RESPONSE:
                _PeerResponsePayload.model_validate(candidate.payload)
        except ValidationError:
            return f"invalid_{candidate.kind.value}_schema"
        return None

    def _candidate_assignment(
        self, candidate: CommandCandidate
    ) -> dict[str, Any] | None:
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
        if assignment.get("task_id") != candidate.task_id:
            return "assignment_task_mismatch"
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

    def _validate_peer_request_authority(
        self,
        candidate: CommandCandidate,
        assignment: dict[str, Any] | None,
    ) -> str | None:
        """A one-hop request still spends authority owned by one active Assignment."""

        if assignment is None or assignment.get("run_id") != candidate.run_id:
            return "peer_assignment_not_found_in_run"
        if assignment.get("agent_id") != candidate.actor_id:
            return "peer_assignment_not_held_by_actor"
        if assignment.get("task_id") != candidate.task_id:
            return "peer_assignment_task_mismatch"
        lease = self.store.projection(
            "lease",
            str(candidate.payload["assignment_id"]),
        )
        if (
            lease is None
            or lease.get("run_id") != candidate.run_id
            or lease.get("status") != "active"
            or lease.get("agent_id") != candidate.actor_id
        ):
            return "peer_assignment_lease_not_active_for_actor"
        try:
            deadline = datetime.fromisoformat(str(lease["expires_at"]))
        except (KeyError, TypeError, ValueError):
            return "peer_assignment_lease_not_active_for_actor"
        if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
            return "peer_assignment_lease_not_active_for_actor"
        if "peer.request" not in set(assignment.get("required_capabilities", [])):
            return "peer_request_not_allowed_by_assignment"
        receiver_id = str(candidate.payload["receiver"])
        if receiver_id == candidate.actor_id:
            return "peer_self_request_denied"
        receiver = self.store.projection("agent", receiver_id)
        if receiver is None:
            return "peer_receiver_not_found"
        if "peer.respond" not in set(receiver.get("capabilities", [])):
            return "peer_receiver_not_capable"
        if receiver.get("status") == "offline":
            return "peer_receiver_unavailable"
        return None

    def _validate_peer_response_authority(
        self,
        candidate: CommandCandidate,
    ) -> str | None:
        """Bind a response to the exact persisted request and its participants."""

        correlation_id = candidate.payload["correlation_id"]
        request = next(
            (
                event
                for event in reversed(self.store.read_all(run_id=candidate.run_id))
                if event.type == "a2a.peer.requested"
                and event.payload.get("correlation_id") == correlation_id
            ),
            None,
        )
        if request is None:
            return "peer_request_not_found"
        if request.payload.get("assignment_id") != candidate.payload["assignment_id"]:
            return "peer_response_assignment_mismatch"
        if request.payload.get("receiver") != candidate.actor_id:
            return "peer_response_actor_mismatch"
        if request.task_id != candidate.task_id:
            return "peer_response_task_mismatch"
        if request.payload.get("sender") != candidate.payload["receiver"]:
            return "peer_response_receiver_mismatch"
        lease = self.store.projection(
            "lease",
            str(request.payload["assignment_id"]),
        )
        if (
            lease is None
            or lease.get("run_id") != candidate.run_id
            or lease.get("status") != "active"
            or lease.get("agent_id") != request.payload.get("sender")
        ):
            return "peer_response_request_assignment_not_active"
        try:
            deadline = datetime.fromisoformat(str(lease["expires_at"]))
        except (KeyError, TypeError, ValueError):
            return "peer_response_request_assignment_not_active"
        if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
            return "peer_response_request_assignment_not_active"
        if any(
            event.type == "a2a.peer.responded"
            and event.payload.get("correlation_id") == correlation_id
            for event in self.store.read_all(run_id=candidate.run_id)
        ):
            return "peer_response_already_recorded"
        return self._validate_peer_agent_run(candidate, request)

    def _validate_assignment_agent_run(
        self,
        candidate: CommandCandidate,
        assignment: dict[str, Any],
    ) -> str | None:
        agent_run_id = candidate.payload.get("agent_run_id")
        submission_id = candidate.payload.get("submission_event_id")
        if not isinstance(agent_run_id, str) or not agent_run_id:
            return "assignment_agent_run_id_required"
        if not isinstance(submission_id, str) or not submission_id:
            return "assignment_submission_event_required"
        try:
            seed = next(
                event
                for event in self.store.read_all(task_id=agent_run_id)
                if event.type == "agent.run.created"
            )
            submission = self.store.get_event(submission_id)
            assignment_created = next(
                event
                for event in self.store.read_all(run_id=candidate.run_id)
                if event.type == "assignment.created"
                and event.payload.get("assignment_id")
                == assignment.get("assignment_id")
            )
        except (KeyError, StopIteration):
            return "assignment_agent_run_provenance_not_found"
        try:
            contract = AssignmentContract.model_validate(assignment.get("contract"))
        except ValidationError:
            return "assignment_agent_run_contract_mismatch"
        if (
            seed.run_id != candidate.run_id
            or seed.source != "runtime.team"
            or seed.task_id != agent_run_id
            or seed.payload.get("agent_run_kind") != "assignment"
            or seed.payload.get("assignment_id") != assignment.get("assignment_id")
            or seed.payload.get("agent_id") != candidate.actor_id
            or seed.payload.get("root_task_id") != candidate.task_id
            or seed.causation_id != assignment_created.id
            or submission.run_id != candidate.run_id
            or submission.task_id != agent_run_id
            or submission.type != "agent.submitted"
            or submission.source != candidate.actor_id
        ):
            return "assignment_agent_run_provenance_mismatch"
        if seed.payload.get("contract") != contract.model_dump(mode="json"):
            return "assignment_agent_run_contract_mismatch"
        artifact, chain_rejection = self._validated_submission_artifact(
            candidate,
            seed=seed,
            submission=submission,
            agent_run_id=agent_run_id,
            reason_prefix="assignment",
            contract=contract,
        )
        if chain_rejection is not None:
            return chain_rejection
        fields_by_kind = {
            CommandKind.EVIDENCE: ("summary",),
            CommandKind.ARTIFACT: ("title", "summary", "content"),
            CommandKind.REVIEW: ("decision", "summary"),
        }
        if any(
            candidate.payload.get(field) != artifact.get(field)
            for field in fields_by_kind[candidate.kind]
        ):
            return "assignment_submission_payload_mismatch"
        return None

    def _validate_peer_agent_run(
        self,
        candidate: CommandCandidate,
        request: Event,
    ) -> str | None:
        agent_run_id = candidate.payload.get("agent_run_id")
        submission_id = candidate.payload.get("submission_event_id")
        if not isinstance(agent_run_id, str) or not agent_run_id:
            return "peer_agent_run_id_required"
        if not isinstance(submission_id, str) or not submission_id:
            return "peer_submission_event_required"
        try:
            seed = next(
                event
                for event in self.store.read_all(task_id=agent_run_id)
                if event.type == "agent.run.created"
            )
            submission = self.store.get_event(submission_id)
        except (KeyError, StopIteration):
            return "peer_agent_run_provenance_not_found"
        team_contract = self._persisted_team_contract(candidate.run_id)
        contract = team_contract.peer_contract if team_contract is not None else None
        if contract is None:
            return "peer_agent_run_contract_mismatch"
        if (
            seed.run_id != candidate.run_id
            or seed.source != "runtime.team"
            or seed.task_id != agent_run_id
            or seed.payload.get("agent_run_kind") != "peer"
            or seed.payload.get("assignment_id") != request.payload.get("assignment_id")
            or seed.payload.get("correlation_id")
            != candidate.payload.get("correlation_id")
            or seed.payload.get("agent_id") != candidate.actor_id
            or seed.payload.get("root_task_id") != candidate.task_id
            or seed.causation_id != request.id
            or submission.run_id != candidate.run_id
            or submission.task_id != agent_run_id
            or submission.type != "agent.submitted"
            or submission.source != candidate.actor_id
        ):
            return "peer_agent_run_provenance_mismatch"
        if seed.payload.get("contract") != contract.model_dump(mode="json"):
            return "peer_agent_run_contract_mismatch"
        artifact, chain_rejection = self._validated_submission_artifact(
            candidate,
            seed=seed,
            submission=submission,
            agent_run_id=agent_run_id,
            reason_prefix="peer",
            contract=contract,
        )
        if chain_rejection is not None:
            return chain_rejection
        if candidate.payload.get("brief") != artifact.get("brief"):
            return "peer_submission_payload_mismatch"
        return None

    def _validated_submission_artifact(
        self,
        candidate: CommandCandidate,
        *,
        seed: Event,
        submission: Event,
        agent_run_id: str,
        reason_prefix: str,
        contract: AssignmentContract,
    ) -> tuple[dict[str, Any], str | None]:
        child_events = self.store.read_all(task_id=agent_run_id)
        positions = {event.id: index for index, event in enumerate(child_events)}
        try:
            command = self.store.get_event(str(submission.causation_id))
            gate = next(
                event
                for event in child_events
                if event.type == "completion.gate.passed"
                and event.causation_id == command.id
                and event.payload.get("turn_id") == submission.payload.get("turn_id")
            )
        except (KeyError, StopIteration):
            return {}, f"{reason_prefix}_submission_chain_invalid"

        artifact = submission.payload.get("artifact")
        persisted_command = command.payload.get("command")
        turn_id = submission.payload.get("turn_id")
        if (
            not isinstance(artifact, dict)
            or not isinstance(persisted_command, dict)
            or not isinstance(turn_id, str)
            or not turn_id
            or command.run_id != candidate.run_id
            or command.task_id != agent_run_id
            or command.type != "agent.command.validated"
            or command.source != candidate.actor_id
            or command.payload.get("turn_id") != turn_id
            or persisted_command.get("type") != "submit_output"
            or persisted_command.get("artifact") != artifact
            or gate.run_id != candidate.run_id
            or gate.task_id != agent_run_id
            or gate.source != candidate.actor_id
            or seed.id not in positions
            or command.id not in positions
            or gate.id not in positions
            or submission.id not in positions
            or not (
                positions[seed.id]
                < positions[command.id]
                < positions[gate.id]
                < positions[submission.id]
            )
        ):
            return {}, f"{reason_prefix}_submission_chain_invalid"

        if not self._has_valid_model_command_chain(
            candidate,
            seed=seed,
            command=command,
            positions=positions,
            turn_id=turn_id,
        ):
            return {}, f"{reason_prefix}_submission_model_chain_invalid"

        evidence_rejection = self._validate_agent_run_tool_evidence(
            candidate,
            contract=contract,
            seed=seed,
            submission=submission,
            child_events=child_events,
            positions=positions,
            reason_prefix=reason_prefix,
        )
        if evidence_rejection is not None:
            return {}, evidence_rejection

        for raw_ref in candidate.payload.get("evidence_refs", []):
            try:
                evidence = self.store.get_event(str(raw_ref))
            except KeyError:
                continue
            if (
                evidence.task_id == agent_run_id
                and (
                    evidence.id not in positions
                    or positions[evidence.id] >= positions[submission.id]
                )
            ):
                return {}, f"{reason_prefix}_submission_chain_invalid"
        return artifact, None

    def _has_valid_model_command_chain(
        self,
        candidate: CommandCandidate,
        *,
        seed: Event,
        command: Event,
        positions: dict[str, int],
        turn_id: str,
    ) -> bool:
        try:
            response = self.store.get_event(str(command.causation_id))
            request = self.store.get_event(str(response.causation_id))
            request_trigger = self.store.get_event(str(request.causation_id))
        except KeyError:
            return False
        events = (request, response, command)
        if any(
            event.run_id != candidate.run_id
            or event.task_id != command.task_id
            or event.source != candidate.actor_id
            or event.payload.get("turn_id") != turn_id
            or event.id not in positions
            for event in events
        ):
            return False
        if (
            request_trigger.run_id != candidate.run_id
            or request_trigger.task_id != command.task_id
            or request_trigger.id not in positions
            or positions[request_trigger.id] >= positions[request.id]
            or not self._causal_chain_reaches_seed(
                request_trigger,
                seed=seed,
                positions=positions,
            )
        ):
            return False
        return (
            request.type == "model.requested"
            and response.type == "model.completed"
            and response.causation_id == request.id
            and command.type == "agent.command.validated"
            and command.causation_id == response.id
            and positions[seed.id]
            < positions[request.id]
            < positions[response.id]
            < positions[command.id]
        )

    def _causal_chain_reaches_seed(
        self,
        event: Event,
        *,
        seed: Event,
        positions: dict[str, int],
    ) -> bool:
        current = event
        visited: set[str] = set()
        while current.id != seed.id:
            if self._is_trusted_agent_run_observation(current, seed=seed):
                return True
            if current.id in visited or current.causation_id is None:
                return False
            visited.add(current.id)
            try:
                parent = self.store.get_event(str(current.causation_id))
            except KeyError:
                return False
            if (
                parent.run_id != seed.run_id
                or parent.task_id != seed.task_id
                or parent.id not in positions
                or current.id not in positions
                or positions[parent.id] >= positions[current.id]
            ):
                return False
            current = parent
        return True

    def _is_trusted_agent_run_observation(
        self,
        event: Event,
        *,
        seed: Event,
    ) -> bool:
        """Allow a verified root-task fact to enter a private AgentRun as Observation."""

        if event.type != "a2a.peer.responded" or event.source != "runtime.a2a.bridge":
            return False
        root_event_id = event.payload.get("root_event_id")
        if not isinstance(root_event_id, str) or event.causation_id != root_event_id:
            return False
        try:
            root = self.store.get_event(root_event_id)
        except KeyError:
            return False
        fields = ("assignment_id", "correlation_id", "sender", "brief", "evidence_refs")
        return (
            root.run_id == seed.run_id
            and root.task_id == seed.payload.get("root_task_id")
            and root.type == "a2a.peer.responded"
            and root.payload.get("receiver") == seed.payload.get("agent_id")
            and all(event.payload.get(field) == root.payload.get(field) for field in fields)
        )

    def _validate_agent_run_tool_evidence(
        self,
        candidate: CommandCandidate,
        *,
        contract: AssignmentContract,
        seed: Event,
        submission: Event,
        child_events: list[Event],
        positions: dict[str, int],
        reason_prefix: str,
    ) -> str | None:
        referenced_tools: set[str] = set()
        for raw_ref in candidate.payload.get("evidence_refs", []):
            try:
                tool = self.store.get_event(str(raw_ref))
            except KeyError:
                continue
            if tool.type != "tool.completed":
                continue
            result = tool.payload.get("result")
            tool_name = result.get("name") if isinstance(result, dict) else None
            turn_id = tool.payload.get("turn_id")
            operation_id = tool.payload.get("operation_id")
            if (
                not isinstance(tool_name, str)
                or not tool_name
                or not isinstance(turn_id, str)
                or not turn_id
                or not isinstance(operation_id, str)
                or not operation_id
                or tool.id not in positions
                or positions[tool.id] >= positions[submission.id]
            ):
                return f"{reason_prefix}_tool_evidence_chain_invalid"
            operation = next(
                (
                    event
                    for event in child_events
                    if event.type == "operation.started"
                    and event.payload.get("operation_id") == operation_id
                    and event.payload.get("turn_id") == turn_id
                    and event.payload.get("tool_name") == tool_name
                ),
                None,
            )
            requested = next(
                (
                    event
                    for event in child_events
                    if event.type == "tool.requested"
                    and event.payload.get("operation_id") == operation_id
                    and event.payload.get("turn_id") == turn_id
                    and event.payload.get("tool_name") == tool_name
                    and operation is not None
                    and event.causation_id == operation.id
                ),
                None,
            )
            settled = next(
                (
                    event
                    for event in child_events
                    if event.type == "operation.completed"
                    and event.payload.get("operation_id") == operation_id
                    and event.payload.get("turn_id") == turn_id
                    and event.payload.get("result_event_id") == tool.id
                    and event.causation_id == tool.id
                ),
                None,
            )
            try:
                command = self.store.get_event(str(operation.causation_id))
            except (AttributeError, KeyError):
                return f"{reason_prefix}_tool_evidence_chain_invalid"
            persisted = command.payload.get("command")
            if (
                operation is None
                or requested is None
                or settled is None
                or not isinstance(persisted, dict)
                or persisted.get("type") != "call_tool"
                or persisted.get("tool_name") != tool_name
                or command.id not in positions
                or operation.id not in positions
                or requested.id not in positions
                or settled.id not in positions
                or any(
                    event.run_id != candidate.run_id
                    or event.task_id != submission.task_id
                    or event.source != candidate.actor_id
                    for event in (operation, requested, tool, settled)
                )
                or not (
                    positions[seed.id]
                    < positions[command.id]
                    < positions[operation.id]
                    < positions[requested.id]
                    < positions[tool.id]
                    < positions[settled.id]
                    < positions[submission.id]
                )
                or not self._has_valid_model_command_chain(
                    candidate,
                    seed=seed,
                    command=command,
                    positions=positions,
                    turn_id=turn_id,
                )
            ):
                return f"{reason_prefix}_tool_evidence_chain_invalid"
            referenced_tools.add(tool_name)

        missing = sorted(set(contract.evidence_requirements) - referenced_tools)
        if missing:
            return f"{reason_prefix}_evidence_requirements_missing:{','.join(missing)}"
        return None

    def _validate_evidence_references(
        self,
        candidate: CommandCandidate,
    ) -> str | None:
        """Evidence references are facts only when they resolve inside the same run."""

        refs = candidate.payload.get("evidence_refs")
        allowed_types = self._ALLOWED_EVIDENCE_TYPES[candidate.kind]
        agent_run_id = candidate.payload.get("agent_run_id")
        has_agent_tool_evidence = False
        if not isinstance(refs, (list, tuple)):
            return "evidence_refs_must_be_a_list"
        if not refs:
            return "evidence_refs_empty"
        for raw_ref in refs:
            ref = str(raw_ref).strip()
            if not ref:
                return "evidence_ref_empty"
            try:
                event = self.store.get_event(ref)
            except KeyError:
                return f"evidence_ref_not_found:{ref}"
            if event.run_id != candidate.run_id:
                return f"evidence_ref_cross_run:{ref}"
            if event.type not in allowed_types:
                return f"evidence_ref_type_not_allowed:{event.type}"
            if event.type == "tool.completed":
                result = event.payload.get("result")
                if not isinstance(result, dict) or result.get("status") != "ok":
                    return f"evidence_ref_tool_not_successful:{ref}"
                if isinstance(agent_run_id, str):
                    if event.task_id != agent_run_id:
                        return f"evidence_ref_agent_run_mismatch:{ref}"
                    if event.source != candidate.actor_id:
                        return f"evidence_ref_actor_mismatch:{ref}"
                    has_agent_tool_evidence = True
            elif event.type == "a2a.peer.responded" and isinstance(agent_run_id, str):
                if event.task_id != agent_run_id:
                    return f"evidence_ref_agent_run_mismatch:{ref}"
            elif (
                event.type in self._FORMAL_EVIDENCE_TYPES
                and event.task_id != candidate.task_id
            ):
                return f"evidence_ref_task_mismatch:{ref}"
        if (
            candidate.kind in self._LEASED_RESULT_KINDS
            or candidate.kind is CommandKind.PEER_RESPONSE
        ) and not has_agent_tool_evidence:
            return "agent_tool_evidence_required"
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
            if event.type == "orchestration.plan.patched"
            and "revision" in event.payload
        ]
        expected = max(revisions, default=0) + 1
        if patch.revision != expected:
            return f"plan_revision_must_advance:expected={expected}"

        contract = self._persisted_team_contract(candidate.run_id)
        if contract is not None and (
            patch.contract_id != contract.contract_id
            or patch.contract_version != contract.version
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
            if (
                len(run_active_leases) + len(patch.assignments)
                > contract.max_parallel_assignments
            ):
                return (
                    f"team_parallelism_exceeded:max={contract.max_parallel_assignments}"
                )

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
                    return (
                        f"assignment_stage_dependencies_unsatisfied:{proposal.stage_id}"
                    )
                previous_attempts = [
                    int(item.get("attempt", 1))
                    for item in run_assignments
                    if item.get("stage_id") == proposal.stage_id
                ]
                expected_attempt = max(previous_attempts, default=0) + 1
                if proposal.attempt != expected_attempt:
                    return (
                        f"assignment_attempt_must_advance:expected={expected_attempt}"
                    )
                if (
                    self.store.projection("assignment", proposal.assignment_id)
                    is not None
                ):
                    return f"assignment_id_already_exists:{proposal.assignment_id}"
                stage_view = patch_stages[proposal.stage_id]
                if (
                    stage_view.state != "active"
                    or stage_view.agent_id != proposal.agent_id
                ):
                    return f"assignment_plan_view_mismatch:{proposal.stage_id}"
                matches_contract = (
                    proposal.goal == persisted.goal
                    and proposal.required_capabilities
                    == persisted.required_capabilities
                    and proposal.exit_criteria == persisted.exit_criteria
                    and proposal.result_kind == persisted.result_kind
                    and proposal.contract_version == contract.version
                    and proposal.lease_seconds == contract.lease_seconds
                    and proposal.contract == persisted.assignment_contract
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
            self._event(
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
                self._event(
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
        emit = lambda suffix, event_type, body, source="control.kernel": self._event(  # noqa: E731
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
                        "stages": [
                            stage.model_dump(mode="json") for stage in patch.stages
                        ],
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
                {
                    **payload,
                    "agent_id": candidate.actor_id,
                    "stage_id": assignment.get("stage_id"),
                },
                candidate.actor_id,
            )
            finished = emit(
                "evidence-assignment-succeeded",
                "assignment.succeeded",
                {
                    "assignment_id": payload["assignment_id"],
                    "stage_id": assignment.get("stage_id"),
                },
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
                    "remaining_budget": self._PEER_REQUEST_LIMIT - 1,
                },
            )
            request = emit(
                "peer-requested",
                "a2a.peer.requested",
                {
                    **payload,
                    "sender": candidate.actor_id,
                    "correlation_id": payload.get(
                        "correlation_id", candidate.candidate_id
                    ),
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
                {
                    **payload,
                    "agent_id": candidate.actor_id,
                    "stage_id": assignment.get("stage_id"),
                },
                candidate.actor_id,
            )
            finished = emit(
                "artifact-assignment-succeeded",
                "assignment.succeeded",
                {
                    "assignment_id": payload["assignment_id"],
                    "stage_id": assignment.get("stage_id"),
                },
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
                {
                    **payload,
                    "agent_id": candidate.actor_id,
                    "stage_id": assignment.get("stage_id"),
                },
                candidate.actor_id,
            )
            finished = emit(
                "review-assignment-succeeded",
                "assignment.succeeded",
                {
                    "assignment_id": payload["assignment_id"],
                    "stage_id": assignment.get("stage_id"),
                },
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
            requested = emit(
                "completion-requested",
                "completion.requested",
                payload,
                candidate.actor_id,
            )
            event_types = {
                event.type for event in self.store.read_all(run_id=candidate.run_id)
            }
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
            proposed = emit(
                "memory-proposed",
                "memory.candidate.proposed",
                payload,
                candidate.actor_id,
            )
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
        return self.store.append(
            self._event(
                candidate,
                suffix,
                event_type,
                payload,
                source=source,
                causation_id=causation_id,
            )
        )

    def _event(
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
        return event

    def _trip_during_command_commit(self, _: Event) -> None:
        self.fault_controller.trip("during_command_commit")
