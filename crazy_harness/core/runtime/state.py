from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from crazy_harness.core.events import Event


class AgentStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    WAITING = "waiting"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class AssignmentState(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    SUBMITTED = "submitted"
    REVIEWING = "reviewing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OperationState(StrEnum):
    PLANNED = "planned"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


_AGENT_EVENTS = {
    "runtime.agent.idle": AgentStatus.IDLE,
    "runtime.agent.busy": AgentStatus.BUSY,
    "runtime.agent.waiting": AgentStatus.WAITING,
    "runtime.agent.degraded": AgentStatus.DEGRADED,
    "runtime.agent.offline": AgentStatus.OFFLINE,
}
_ASSIGNMENT_EVENTS = {
    "assignment.running": AssignmentState.RUNNING,
    "assignment.waiting": AssignmentState.WAITING,
    "assignment.submitted": AssignmentState.SUBMITTED,
    "assignment.reviewing": AssignmentState.REVIEWING,
    "assignment.succeeded": AssignmentState.SUCCEEDED,
    "assignment.failed": AssignmentState.FAILED,
}
_OPERATION_EVENTS = {
    "operation.planned": OperationState.PLANNED,
    "operation.started": OperationState.STARTED,
    "operation.completed": OperationState.SUCCEEDED,
    "operation.failed": OperationState.FAILED,
    "operation.unknown": OperationState.UNKNOWN,
}

State = TypeVar("State", AgentStatus, AssignmentState, OperationState)


def _reduce(current: State, event: Event, *, key: str, identity: str, events: dict[str, State]) -> State:
    if event.payload.get(key) != identity:
        return current
    return events.get(event.type, current)


def reduce_agent_status(current: AgentStatus, event: Event, *, agent_id: str) -> AgentStatus:
    return _reduce(current, event, key="agent_id", identity=agent_id, events=_AGENT_EVENTS)


def reduce_assignment_state(
    current: AssignmentState, event: Event, *, assignment_id: str
) -> AssignmentState:
    return _reduce(current, event, key="assignment_id", identity=assignment_id, events=_ASSIGNMENT_EVENTS)


def reduce_operation_state(current: OperationState, event: Event, *, operation_id: str) -> OperationState:
    return _reduce(current, event, key="operation_id", identity=operation_id, events=_OPERATION_EVENTS)
