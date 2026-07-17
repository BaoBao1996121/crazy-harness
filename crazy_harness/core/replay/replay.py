from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from crazy_harness.core.events import Event, EventLog


class ReplayMode(StrEnum):
    DRY = "dry"
    EXECUTE_EFFECTS = "execute_effects"


class ReplayReport(BaseModel):
    event_count: int
    provider_events: int
    command_events: int
    execution_events: int
    blocked_side_effects: int


def replay_events(
    path: Path,
    *,
    mode: ReplayMode = ReplayMode.DRY,
    side_effect_executor: Callable[[Event], None] | None = None,
) -> ReplayReport:
    """Replay a trace for diagnosis; effects stay disabled unless explicitly requested."""

    events = EventLog(path).read_all()
    provider = [event for event in events if event.type.startswith("model.")]
    command = [event for event in events if event.type.startswith("agent.command")]
    execution = [
        event
        for event in events
        if event.type.startswith(("operation.", "tool.", "agent.action", "agent.stopped", "agent.submitted"))
    ]
    effect_events = [event for event in events if event.type == "operation.started"]
    if mode == ReplayMode.EXECUTE_EFFECTS:
        if side_effect_executor is None:
            raise ValueError("side_effect_executor is required for effectful replay")
        for event in effect_events:
            side_effect_executor(event)

    report = ReplayReport(
        event_count=len(events),
        provider_events=len(provider),
        command_events=len(command),
        execution_events=len(execution),
        blocked_side_effects=len(effect_events) if mode == ReplayMode.DRY else 0,
    )
    print(report.model_dump_json(indent=2))
    return report
