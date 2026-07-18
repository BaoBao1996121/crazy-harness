from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterator, Mapping


class DispatchCancelled(RuntimeError):
    """Cooperative stop signal for one claimed delivery."""


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str) -> None:
        with self._lock:
            if self._cancelled.is_set():
                return
            self._reason = reason
            self._cancelled.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._cancelled.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise DispatchCancelled(self.reason or "dispatch cancelled")


@dataclass(frozen=True)
class DispatchContext:
    worker_id: str
    delivery_id: str
    claim_owner_id: str
    claim_tokens: Mapping[str, int]
    cancellation: CancellationToken

    @classmethod
    def create(
        cls,
        *,
        worker_id: str,
        delivery_id: str,
        claim_owner_id: str,
        claim_tokens: dict[str, int],
    ) -> "DispatchContext":
        return cls(
            worker_id=worker_id,
            delivery_id=delivery_id,
            claim_owner_id=claim_owner_id,
            claim_tokens=MappingProxyType(dict(claim_tokens)),
            cancellation=CancellationToken(),
        )


_CURRENT_DISPATCH: ContextVar[DispatchContext | None] = ContextVar(
    "crazy_current_dispatch",
    default=None,
)


def current_dispatch_context() -> DispatchContext | None:
    return _CURRENT_DISPATCH.get()


@contextmanager
def activate_dispatch_context(context: DispatchContext) -> Iterator[DispatchContext]:
    token = _CURRENT_DISPATCH.set(context)
    try:
        yield context
    finally:
        _CURRENT_DISPATCH.reset(token)


def raise_if_dispatch_cancelled() -> None:
    context = current_dispatch_context()
    if context is not None:
        context.cancellation.raise_if_cancelled()
