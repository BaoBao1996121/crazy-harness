from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from pydantic import BaseModel, Field


class A2AMessage(BaseModel):
    task_id: str
    context_id: str
    sender: str
    receiver: str
    performative: str
    instruction: str
    brief: str = ""
    context_refs: list[str] = Field(default_factory=list)
    expected_output: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)


class A2ABus:
    def __init__(self) -> None:
        self._queues: dict[str, deque[A2AMessage]] = defaultdict(deque)

    def send(self, message: A2AMessage) -> None:
        self._queues[message.receiver].append(message)

    def receive(self, agent_id: str) -> list[A2AMessage]:
        queue = self._queues[agent_id]
        messages = list(queue)
        queue.clear()
        return messages
