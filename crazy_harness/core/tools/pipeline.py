from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from crazy_harness.core.hooks import HookManager
from crazy_harness.core.dispatch import DispatchCancelled, raise_if_dispatch_cancelled
from crazy_harness.core.tools.concurrency import (
    BatchExecutionResult,
    ConsecutiveSafePlanner,
    SettledToolResult,
    ToolInvocation,
    execute_all_settled,
)
from crazy_harness.core.tools.policy import PolicyContext, PolicyDecision, PolicyDenied, ToolPolicy
from crazy_harness.core.tools.registry import ToolRegistry
from crazy_harness.core.tools.schemas import ToolCall, ToolResult


class ToolValidationError(ValueError):
    pass


class OperationOutcomeUnknown(RuntimeError):
    """The external effect may have happened, so automatic retry is unsafe."""


class OperationTransitionError(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class OperationState(StrEnum):
    PLANNED = "planned"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"


_ALLOWED_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.PLANNED: frozenset({OperationState.STARTED}),
    OperationState.STARTED: frozenset(
        {OperationState.SUCCEEDED, OperationState.FAILED, OperationState.UNKNOWN}
    ),
    OperationState.UNKNOWN: frozenset({OperationState.RECONCILING}),
    OperationState.RECONCILING: frozenset(
        {OperationState.SUCCEEDED, OperationState.FAILED, OperationState.UNKNOWN}
    ),
    OperationState.SUCCEEDED: frozenset(),
    OperationState.FAILED: frozenset(),
}


@dataclass(frozen=True)
class ToolRequest:
    call: ToolCall
    call_id: str = field(default_factory=lambda: f"call_{uuid4().hex}")
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.call, ToolCall):
            object.__setattr__(self, "call", ToolCall.model_validate(self.call))
        if not self.call_id:
            raise ValueError("call_id must not be empty")
        if self.idempotency_key is None:
            object.__setattr__(self, "idempotency_key", self.call_id)
        elif not self.idempotency_key:
            raise ValueError("idempotency_key must not be empty")


class PreToolHookPayload(BaseModel):
    """Typed envelope; a pre-hook may patch only args."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str
    tool_name: str
    args: dict[str, Any]
    agent_id: str
    assignment_id: str
    mode: str


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    idempotency_key: str
    call_id: str
    tool_name: str
    request_fingerprint: str
    state: OperationState
    result: ToolResult | None = None
    error: str | None = None


class OperationLedger:
    """Append-backed operation state with strict transitions and idempotency."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._records: dict[str, OperationRecord] = {}
        self._operation_ids_by_key: dict[str, str] = {}
        self._lock = RLock()
        if self.path is not None and self.path.exists():
            self._load()
            self.recover_incomplete("ledger reopened after an incomplete external operation")

    def plan(self, invocation: ToolInvocation) -> OperationRecord:
        fingerprint = _call_fingerprint(invocation.call)
        with self._lock:
            existing = self.by_idempotency_key(invocation.idempotency_key)
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency key {invocation.idempotency_key!r} was reused for a different call"
                    )
                return existing
            record = OperationRecord(
                operation_id=f"op_{uuid4().hex}",
                idempotency_key=invocation.idempotency_key,
                call_id=invocation.call_id,
                tool_name=invocation.call.name,
                request_fingerprint=fingerprint,
                state=OperationState.PLANNED,
            )
            return self._store(record)

    def get(self, operation_id: str) -> OperationRecord:
        with self._lock:
            try:
                return self._records[operation_id]
            except KeyError as exc:
                raise KeyError(f"unknown operation: {operation_id}") from exc

    def by_idempotency_key(self, idempotency_key: str) -> OperationRecord | None:
        with self._lock:
            operation_id = self._operation_ids_by_key.get(idempotency_key)
            return self._records.get(operation_id) if operation_id is not None else None

    def start(self, operation_id: str) -> OperationRecord:
        return self._transition(operation_id, OperationState.STARTED)

    def succeed(self, operation_id: str, result: ToolResult) -> OperationRecord:
        return self._transition(operation_id, OperationState.SUCCEEDED, result=result)

    def fail(self, operation_id: str, error: str, result: ToolResult | None = None) -> OperationRecord:
        return self._transition(operation_id, OperationState.FAILED, result=result, error=error)

    def mark_unknown(self, operation_id: str, reason: str) -> OperationRecord:
        return self._transition(operation_id, OperationState.UNKNOWN, error=reason)

    def begin_reconcile(self, operation_id: str) -> OperationRecord:
        return self._transition(operation_id, OperationState.RECONCILING)

    def reconcile(
        self,
        operation_id: str,
        outcome: OperationState | str,
        *,
        result: ToolResult | None = None,
        error: str | None = None,
    ) -> OperationRecord:
        target = OperationState(outcome)
        if target not in {OperationState.SUCCEEDED, OperationState.FAILED, OperationState.UNKNOWN}:
            raise ValueError("reconciliation outcome must be succeeded, failed, or unknown")
        current = self.get(operation_id)
        if current.state is target:
            return current
        if current.state is OperationState.UNKNOWN:
            self.begin_reconcile(operation_id)
        elif current.state is not OperationState.RECONCILING:
            raise OperationTransitionError(
                f"cannot reconcile operation in state {current.state.value!r}"
            )
        return self._transition(operation_id, target, result=result, error=error)

    def can_retry(self, operation_id: str) -> bool:
        return self.get(operation_id).state is OperationState.PLANNED

    def recover_incomplete(self, reason: str = "operation outcome is unknown after recovery") -> None:
        started_ids = [
            record.operation_id
            for record in tuple(self._records.values())
            if record.state is OperationState.STARTED
        ]
        for operation_id in started_ids:
            self.mark_unknown(operation_id, reason)

    def _transition(
        self,
        operation_id: str,
        target: OperationState,
        *,
        result: ToolResult | None = None,
        error: str | None = None,
    ) -> OperationRecord:
        with self._lock:
            current = self.get(operation_id)
            if current.state is target:
                return current
            if target not in _ALLOWED_TRANSITIONS[current.state]:
                raise OperationTransitionError(
                    f"illegal operation transition: {current.state.value} -> {target.value}"
                )
            return self._store(replace(current, state=target, result=result, error=error))

    def _store(self, record: OperationRecord) -> OperationRecord:
        self._records[record.operation_id] = record
        self._operation_ids_by_key[record.idempotency_key] = record.operation_id
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "operation_id": record.operation_id,
                "idempotency_key": record.idempotency_key,
                "call_id": record.call_id,
                "tool_name": record.tool_name,
                "request_fingerprint": record.request_fingerprint,
                "state": record.state.value,
                "result": record.result.model_dump(mode="json") if record.result is not None else None,
                "error": record.error,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def _load(self) -> None:
        assert self.path is not None
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    result = (
                        ToolResult.model_validate(payload["result"])
                        if payload.get("result") is not None
                        else None
                    )
                    record = OperationRecord(
                        operation_id=payload["operation_id"],
                        idempotency_key=payload["idempotency_key"],
                        call_id=payload["call_id"],
                        tool_name=payload["tool_name"],
                        request_fingerprint=payload["request_fingerprint"],
                        state=OperationState(payload["state"]),
                        result=result,
                        error=payload.get("error"),
                    )
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    raise ValueError(f"invalid operation ledger line {line_number}: {exc}") from exc
                self._records[record.operation_id] = record
                self._operation_ids_by_key[record.idempotency_key] = record.operation_id


@dataclass(frozen=True)
class PipelineExecution:
    batches: tuple[BatchExecutionResult, ...]

    @property
    def results(self) -> tuple[SettledToolResult, ...]:
        return tuple(result for batch in self.batches for result in batch.results)


class ToolPipeline:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policy: ToolPolicy | None = None,
        hooks: HookManager | None = None,
        ledger: OperationLedger | None = None,
        planner: ConsecutiveSafePlanner | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy or ToolPolicy()
        self.hooks = hooks
        self.ledger = ledger or OperationLedger()
        self.planner = planner or ConsecutiveSafePlanner()

    def prepare(self, request: ToolRequest, context: PolicyContext) -> ToolInvocation:
        try:
            spec = self.registry.spec(request.call.name)
        except KeyError as exc:
            raise ToolValidationError(f"unknown tool: {request.call.name}") from exc

        validate_tool_args(request.call.args, getattr(spec, "input_schema", {}))
        patched_call = request.call
        if self.hooks is not None:
            original = PreToolHookPayload(
                call_id=request.call_id,
                tool_name=request.call.name,
                args=dict(request.call.args),
                agent_id=context.agent_id,
                assignment_id=context.assignment_id,
                mode=context.mode,
            )
            try:
                emitted = self.hooks.emit("pre_tool_use", original.model_dump(mode="python"))
                patched = PreToolHookPayload.model_validate(emitted)
            except Exception as exc:
                raise ToolValidationError(f"pre-tool hook failed closed: {exc}") from exc
            if (
                patched.call_id,
                patched.tool_name,
                patched.agent_id,
                patched.assignment_id,
                patched.mode,
            ) != (
                original.call_id,
                original.tool_name,
                original.agent_id,
                original.assignment_id,
                original.mode,
            ):
                raise PolicyDenied(
                    PolicyDecision(allowed=False, reason="pre-tool hook may patch args but cannot expand authority")
                )
            patched_call = ToolCall(name=patched.tool_name, args=patched.args)

        validate_tool_args(patched_call.args, getattr(spec, "input_schema", {}))
        self.policy.require(patched_call, spec, context)
        return ToolInvocation(
            call_id=request.call_id,
            call=patched_call,
            spec=spec,
            idempotency_key=str(request.idempotency_key),
        )

    def execute(
        self,
        requests: list[ToolRequest] | tuple[ToolRequest, ...],
        context: PolicyContext,
        *,
        on_started: Callable[[OperationRecord, ToolInvocation], None] | None = None,
    ) -> PipelineExecution:
        invocations = tuple(self.prepare(request, context) for request in requests)
        if len({item.call_id for item in invocations}) != len(invocations):
            raise ToolValidationError("call_id values must be unique within one pipeline execution")

        records: dict[str, OperationRecord] = {}
        primary: dict[str, bool] = {}
        seen_operation_ids: set[str] = set()
        for invocation in invocations:
            record = self.ledger.plan(invocation)
            records[invocation.call_id] = record
            primary[invocation.call_id] = record.operation_id not in seen_operation_ids
            seen_operation_ids.add(record.operation_id)

        batch_results = []
        for batch in self.planner.plan(invocations):
            batch_results.append(
                execute_all_settled(
                    batch,
                    lambda invocation: self._execute_one(
                        invocation,
                        records[invocation.call_id],
                        primary[invocation.call_id],
                        on_started,
                    ),
                )
            )
        return PipelineExecution(batches=tuple(batch_results))

    def _execute_one(
        self,
        invocation: ToolInvocation,
        planned: OperationRecord,
        is_primary: bool,
        on_started: Callable[[OperationRecord, ToolInvocation], None] | None,
    ) -> SettledToolResult:
        raise_if_dispatch_cancelled()
        record = self.ledger.get(planned.operation_id)
        if not is_primary:
            return self._existing_result(invocation, record, duplicate=True)
        if record.state is not OperationState.PLANNED:
            if record.state is OperationState.STARTED:
                record = self.ledger.mark_unknown(
                    record.operation_id, "existing started operation has no confirmed result"
                )
            return self._existing_result(invocation, record)

        try:
            record = self.ledger.start(record.operation_id)
            if on_started is not None:
                on_started(record, invocation)
        except OperationTransitionError:
            return self._existing_result(invocation, self.ledger.get(record.operation_id))

        try:
            raise_if_dispatch_cancelled()
            result = self.registry.call(invocation.call)
        except DispatchCancelled:
            raise
        except OperationOutcomeUnknown as exc:
            self.ledger.mark_unknown(record.operation_id, str(exc))
            return SettledToolResult(
                call_id=invocation.call_id,
                operation_id=record.operation_id,
                status="unknown",
                error=str(exc),
            )
        except Exception as exc:
            self.ledger.fail(record.operation_id, str(exc))
            return SettledToolResult(
                call_id=invocation.call_id,
                operation_id=record.operation_id,
                status="rejected",
                error=str(exc),
            )

        if result.status.casefold() in {"ok", "success", "succeeded"}:
            self.ledger.succeed(record.operation_id, result)
            return SettledToolResult(
                call_id=invocation.call_id,
                operation_id=record.operation_id,
                status="fulfilled",
                result=result,
            )

        error = result.error or f"tool returned status {result.status!r}"
        self.ledger.fail(record.operation_id, error, result)
        return SettledToolResult(
            call_id=invocation.call_id,
            operation_id=record.operation_id,
            status="rejected",
            result=result,
            error=error,
        )

    @staticmethod
    def _existing_result(
        invocation: ToolInvocation,
        record: OperationRecord,
        *,
        duplicate: bool = False,
    ) -> SettledToolResult:
        if record.state in {OperationState.UNKNOWN, OperationState.RECONCILING, OperationState.STARTED}:
            status = "unknown"
        elif record.state in {OperationState.SUCCEEDED, OperationState.FAILED}:
            status = "cached"
        else:
            status = "duplicate" if duplicate else "rejected"
        return SettledToolResult(
            call_id=invocation.call_id,
            operation_id=record.operation_id,
            status=status,
            result=record.result,
            error=record.error,
        )


def validate_tool_args(args: dict[str, Any], schema: dict[str, Any] | None) -> None:
    _validate_value(args, schema or {}, path="args")


def _validate_value(value: Any, schema: dict[str, Any], *, path: str) -> None:
    if not schema:
        return
    if "enum" in schema and value not in schema["enum"]:
        raise ToolValidationError(f"{path} must be one of {schema['enum']!r}")
    if "const" in schema and value != schema["const"]:
        raise ToolValidationError(f"{path} must equal {schema['const']!r}")

    expected = schema.get("type")
    if expected is not None:
        choices = [expected] if isinstance(expected, str) else list(expected)
        if not any(_matches_json_type(value, choice) for choice in choices):
            raise ToolValidationError(f"{path} must have JSON type {' or '.join(choices)}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ToolValidationError(f"{path} is missing required properties: {missing!r}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child_schema = properties.get(key)
            if child_schema is None:
                if additional is False:
                    raise ToolValidationError(f"{path}.{key} is not an allowed property")
                child_schema = additional if isinstance(additional, dict) else {}
            _validate_value(item, child_schema, path=f"{path}.{key}")
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_value(item, schema["items"], path=f"{path}[{index}]")
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ToolValidationError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolValidationError(f"{path} is longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ToolValidationError(f"{path} does not match the required pattern")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolValidationError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolValidationError(f"{path} is above maximum")


def _matches_json_type(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    try:
        return checks[expected](value)
    except KeyError as exc:
        raise ToolValidationError(f"unsupported JSON schema type: {expected!r}") from exc


def _call_fingerprint(call: ToolCall) -> str:
    canonical = json.dumps(
        call.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
