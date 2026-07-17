from __future__ import annotations


def __init__(self) -> None:
    self._queue = []
    self._history = []


def publish(self, message) -> None:
    self._queue.append(message)
    self._history.append(message)


def drain(self, receiver: str):
    matched = [m for m in self._queue if m.receiver == receiver]
    self._queue = [m for m in self._queue if m.receiver != receiver]
    return matched


def history(self):
    return list(self._history)
