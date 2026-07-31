from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from crazy_harness.core.agents.loop import AgentLoop
from crazy_harness.core.agents.state import LoopPhase
from crazy_harness.core.events import Event


class AgentRunKind(StrEnum):
    """The control-plane role played by one isolated AgentRun."""

    SINGLE = "single"
    ASSIGNMENT = "assignment"
    PEER = "peer"


class AgentRunStatus(StrEnum):
    """A durable projection of an AgentRun, never a second source of truth."""

    READY = "ready"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    WAITING = "waiting"
    BLOCKED = "blocked"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class AgentRunSessionIdentity:
    run_id: str
    task_id: str
    agent_id: str
    kind: AgentRunKind

    def __post_init__(self) -> None:
        for field_name in ("run_id", "task_id", "agent_id"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")


@dataclass(frozen=True)
class AgentRunSessionView:
    identity: AgentRunSessionIdentity
    status: AgentRunStatus
    completed_turns: int
    latest_phase: LoopPhase | None
    latest_event_id: str
    latest_event_type: str
    capability_manifest_hash: str | None
    fork_supported: bool
    fork_ready: bool
    fork_blocker: str | None


@dataclass
class AgentRunSession:
    """Product boundary around one AgentLoop, reconstructed from durable facts."""

    identity: AgentRunSessionIdentity
    loop: AgentLoop

    def __post_init__(self) -> None:
        if self.loop.task_id != self.identity.task_id:
            raise ValueError("AgentRunSession task_id does not match its AgentLoop")
        if self.loop.agent_id != self.identity.agent_id:
            raise ValueError("AgentRunSession agent_id does not match its AgentLoop")

    def step(self) -> AgentRunSessionView:
        """Advance at most one canonical AgentLoop turn and return its new projection."""

        self.loop.run_once()
        return self.view()

    def view(self) -> AgentRunSessionView:
        """Rebuild the user-visible state exclusively from this run's EventLog facts."""

        return self.project_view(self.identity, self._events())

    @classmethod
    def project_view(
        cls,
        identity: AgentRunSessionIdentity,
        events: list[Event],
    ) -> AgentRunSessionView:
        """Build a read-only view without constructing or advancing an AgentLoop."""

        if not events:
            raise RuntimeError("AgentRunSession requires a durable seed event")

        latest_phase = cls._latest_phase(events)
        created = next((event for event in events if event.type == "run.created"), None)
        fork_supported = bool(
            identity.kind is AgentRunKind.SINGLE
            and created is not None
            and created.payload.get("task_pack") == "repo-maintainer"
        )
        return AgentRunSessionView(
            identity=identity,
            status=cls._status(events),
            completed_turns=cls._completed_turns(events),
            latest_phase=latest_phase,
            latest_event_id=events[-1].id,
            latest_event_type=events[-1].type,
            capability_manifest_hash=cls._latest_capability_manifest_hash(events),
            fork_supported=fork_supported,
            fork_ready=fork_supported,
            fork_blocker=(
                None
                if fork_supported
                else (
                    "当前任务类型不支持工作区分支 / Verified Fork currently requires "
                    "a repo-maintainer single-Agent Run."
                )
            ),
        )

    def _events(self) -> list[Event]:
        return [
            event
            for event in self.loop.event_log.read_all(task_id=self.identity.task_id)
            if event.run_id == self.identity.run_id
        ]

    @staticmethod
    def _latest_phase(events: list[Event]) -> LoopPhase | None:
        for event in reversed(events):
            if event.type == "loop.phase.changed":
                return LoopPhase(str(event.payload["phase"]))
        return None

    @staticmethod
    def _latest_capability_manifest_hash(events: list[Event]) -> str | None:
        for event in reversed(events):
            if event.type != "capability.manifest.compiled":
                continue
            manifest = event.payload.get("manifest")
            if isinstance(manifest, dict) and manifest.get("manifest_hash"):
                return str(manifest["manifest_hash"])
        return None

    @staticmethod
    def _completed_turns(events: list[Event]) -> int:
        turn_terminal_types = {
            "model.validation_failed",
            "agent.action.denied",
            "tool.completed",
            "tool.failed",
            "agent.stopped",
            "agent.submitted",
            "agent.waiting",
            "agent.nudged",
            "agent.continued",
            "a2a.message.sent",
            "a2a.message.denied",
            "artifact.created",
            "operation.unknown",
        }
        return len(
            {
                str(event.payload["turn_id"])
                for event in events
                if event.type in turn_terminal_types and event.payload.get("turn_id")
            }
        )

    @classmethod
    def _status(cls, events: list[Event]) -> AgentRunStatus:
        event_types = {event.type for event in events}
        if "run.cancelled" in event_types:
            return AgentRunStatus.CANCELLED
        if "run.cancel.requested" in event_types:
            return AgentRunStatus.CANCELLING
        if event_types & {"agent.failed", "assignment.failed", "run.failed"}:
            return AgentRunStatus.FAILED
        if event_types & {"agent.stopped", "agent.submitted", "assignment.completed"}:
            return AgentRunStatus.COMPLETED
        pause_request = cls._active_operator_pause(events)
        if pause_request is not None:
            if any(
                event.type == "run.paused" and event.causation_id == pause_request.id
                for event in events
            ):
                return AgentRunStatus.PAUSED
            return AgentRunStatus.PAUSING
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
        if unknown_ids - reconciled_ids:
            return AgentRunStatus.BLOCKED
        if cls._has_active_wait(events):
            return AgentRunStatus.WAITING
        if any(
            event.type == "loop.phase.changed" or "turn_id" in event.payload
            for event in events
        ):
            return AgentRunStatus.RUNNING
        return AgentRunStatus.READY

    @staticmethod
    def _active_operator_pause(events: list[Event]) -> Event | None:
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

    @staticmethod
    def _has_active_wait(events: list[Event]) -> bool:
        return AgentLoop._has_active_wait(events)
