from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventType(StrEnum):
    LOOP_PHASE_CHANGED = "loop.phase.changed"
    MODEL_REQUESTED = "model.requested"
    MODEL_COMPLETED = "model.completed"
    MODEL_VALIDATION_FAILED = "model.validation_failed"
    COMMAND_VALIDATED = "agent.command.validated"
    ACTION_DENIED = "agent.action.denied"
    OPERATION_STARTED = "operation.started"
    OPERATION_COMPLETED = "operation.completed"
    OPERATION_UNKNOWN = "operation.unknown"
    TOOL_REQUESTED = "tool.requested"
    TOOL_COMPLETED = "tool.completed"
    AGENT_STOPPED = "agent.stopped"


class Event(BaseModel):
    """A durable event in a run trajectory."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    task_id: str
    type: str
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list)
    causation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
