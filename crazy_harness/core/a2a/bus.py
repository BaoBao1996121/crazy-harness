from __future__ import annotations

from collections import defaultdict, deque

from crazy_harness.core.a2a.messages import A2AMessage


class A2ABus:
    """In-process transport with per-receiver isolation and idempotent delivery."""

    def __init__(self) -> None:
        self._queues: dict[str, deque[A2AMessage]] = defaultdict(deque)
        self._published_ids: set[str] = set()

    def send(self, message: A2AMessage) -> None:
        if message.message_id in self._published_ids:
            return
        self._queues[message.receiver].append(message)
        self._published_ids.add(message.message_id)

    def receive(self, agent_id: str) -> list[A2AMessage]:
        queue = self._queues[agent_id]
        messages = list(queue)
        queue.clear()
        return messages

    def pending(self, agent_id: str) -> int:
        return len(self._queues[agent_id])
