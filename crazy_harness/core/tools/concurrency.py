from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any

from crazy_harness.core.dispatch import DispatchCancelled
from crazy_harness.core.tools.schemas import ToolCall, ToolResult


@dataclass(frozen=True)
class ToolInvocation:
    call_id: str
    call: ToolCall
    spec: Any
    idempotency_key: str


@dataclass(frozen=True)
class ExecutionBatch:
    index: int
    calls: tuple[ToolInvocation, ...]
    concurrent: bool


@dataclass(frozen=True)
class SettledToolResult:
    call_id: str
    status: str
    operation_id: str | None = None
    result: ToolResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class BatchExecutionResult:
    batch_index: int
    concurrent: bool
    results: tuple[SettledToolResult, ...]


class ConsecutiveSafePlanner:
    """Group adjacent safe reads without moving calls across a barrier."""

    def plan(self, calls: Sequence[ToolInvocation]) -> tuple[ExecutionBatch, ...]:
        batches: list[ExecutionBatch] = []
        safe_segment: list[ToolInvocation] = []

        def flush_safe_segment() -> None:
            if not safe_segment:
                return
            batches.append(
                ExecutionBatch(
                    index=len(batches),
                    calls=tuple(safe_segment),
                    concurrent=len(safe_segment) > 1,
                )
            )
            safe_segment.clear()

        for call in calls:
            if _is_parallel_safe(call):
                safe_segment.append(call)
                continue
            flush_safe_segment()
            batches.append(ExecutionBatch(index=len(batches), calls=(call,), concurrent=False))

        flush_safe_segment()
        return tuple(batches)


def execute_all_settled(
    batch: ExecutionBatch,
    execute: Callable[[ToolInvocation], SettledToolResult],
) -> BatchExecutionResult:
    """Run every sibling and preserve input order even when one fails."""

    def settle(call: ToolInvocation) -> SettledToolResult:
        try:
            return execute(call)
        except DispatchCancelled:
            raise
        except Exception as exc:
            return SettledToolResult(call_id=call.call_id, status="rejected", error=str(exc))

    if batch.concurrent:
        with ThreadPoolExecutor(max_workers=len(batch.calls), thread_name_prefix="crazy-tool") as pool:
            parent_context = copy_context()
            futures = [
                pool.submit(parent_context.copy().run, settle, call)
                for call in batch.calls
            ]
            results = tuple(future.result() for future in futures)
    else:
        results = tuple(settle(call) for call in batch.calls)

    return BatchExecutionResult(
        batch_index=batch.index,
        concurrent=batch.concurrent,
        results=results,
    )


def _is_parallel_safe(invocation: ToolInvocation) -> bool:
    args = invocation.call.args
    read_only = _metadata_flag(invocation.spec, "is_read_only", args)
    destructive = _metadata_flag(invocation.spec, "is_destructive", args)
    concurrency_safe = _metadata_flag(invocation.spec, "is_concurrency_safe", args)
    return read_only and not destructive and concurrency_safe


def _metadata_flag(spec: Any, attribute: str, args: dict[str, Any]) -> bool:
    value = getattr(spec, attribute, False)
    if callable(value):
        try:
            value = value(args)
        except Exception:
            return False
    return bool(value)
