from __future__ import annotations

from collections.abc import Callable
from typing import Any

from crazy_harness.core.tools.schemas import ToolCall, ToolResult, ToolSpec

ToolHandler = Callable[[dict[str, Any]], ToolResult]


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)
        self._handlers.pop(name, None)

    def spec(self, name: str) -> ToolSpec:
        return self._specs[name]

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def has(self, name: str) -> bool:
        return name in self._handlers

    def call(self, call: ToolCall) -> ToolResult:
        handler = self._handlers.get(call.name)
        if handler is None:
            return ToolResult(name=call.name, status="error", error=f"unknown tool: {call.name}")
        return handler(call.args)
