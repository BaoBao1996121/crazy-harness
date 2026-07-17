from __future__ import annotations

import json
import os
from pathlib import Path
from collections.abc import Callable

from crazy_harness.core.events.schemas import Event


class EventLog:
    """Append-only JSONL event log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: Event) -> Event:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def read_all(self, *, task_id: str | None = None) -> list[Event]:
        if not self.path.exists():
            return []
        events: list[Event] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    event = Event.model_validate(json.loads(line))
                    if task_id is None or event.task_id == task_id:
                        events.append(event)
        return events

    def last(self, *, task_id: str | None = None) -> Event | None:
        events = self.read_all(task_id=task_id)
        return events[-1] if events else None

    def find(self, predicate: Callable[[Event], bool], *, task_id: str | None = None) -> list[Event]:
        return [event for event in self.read_all(task_id=task_id) if predicate(event)]
