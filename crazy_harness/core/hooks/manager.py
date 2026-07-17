from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

HookHandler = Callable[[dict[str, Any]], dict[str, Any] | None]


class HookManager:
    def __init__(self) -> None:
        self._handlers: dict[str, list[HookHandler]] = defaultdict(list)

    def register(self, event_name: str, handler: HookHandler) -> None:
        self._handlers[event_name].append(handler)

    def emit(self, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = dict(payload)
        for handler in self._handlers[event_name]:
            updated = handler(current)
            if updated is not None:
                current = updated
        return current
