from __future__ import annotations

import sys
from pathlib import Path
from threading import Barrier

import pytest

from crazy_harness.core.hooks import HookManager
from crazy_harness.core.runtime.local import (
    GuardedLocalRuntime,
    LocalRuntimePolicyError,
    LocalRuntimeTimeout,
)
from crazy_harness.core.tools import ToolCall, ToolRegistry, ToolResult, ToolSpec
from crazy_harness.core.tools.concurrency import ConsecutiveSafePlanner, ToolInvocation
from crazy_harness.core.tools.pipeline import (
    IdempotencyConflict,
    OperationLedger,
    OperationOutcomeUnknown,
    OperationState,
    OperationTransitionError,
    ToolPipeline,
    ToolRequest,
    ToolValidationError,
)
from crazy_harness.core.tools.policy import PolicyContext, PolicyDenied, ToolPolicy


def tool_spec(
    name: str,
    *,
    read_only: bool = False,
    destructive: bool | object = False,
    concurrency_safe: bool | object = False,
    input_schema: dict | None = None,
) -> ToolSpec:
    spec = ToolSpec(name=name, description=name, input_schema=input_schema or {})
    object.__setattr__(spec, "is_read_only", read_only)
    object.__setattr__(spec, "is_destructive", destructive)
    object.__setattr__(spec, "is_concurrency_safe", concurrency_safe)
    return spec


def context(*tools: str, mode: str = "llm-live", approvals: tuple[str, ...] = ()) -> PolicyContext:
    return PolicyContext(
        agent_id="builder",
        assignment_id="assignment-1",
        mode=mode,
        allowed_tools=frozenset(tools),
        approved_tools=frozenset(approvals),
    )


def test_policy_uses_agent_assignment_mode_and_allowed_tools() -> None:
    call = ToolCall(name="deploy", args={})
    spec = tool_spec("deploy")
    policy = ToolPolicy(grants={("builder", "assignment-1", "llm-live"): {"deploy"}})

    assert policy.require(call, spec, context("deploy")).allowed

    wrong_assignment = context("deploy").model_copy(update={"assignment_id": "assignment-2"})
    with pytest.raises(PolicyDenied, match="grant"):
        policy.require(call, spec, wrong_assignment)


def test_broken_safety_metadata_is_denied() -> None:
    def broken_metadata(_args: dict) -> bool:
        raise RuntimeError("metadata unavailable")

    spec = tool_spec("deploy", destructive=broken_metadata)

    with pytest.raises(PolicyDenied, match="metadata"):
        ToolPolicy().require(ToolCall(name="deploy"), spec, context("deploy"))


def test_hook_patch_is_revalidated_and_cannot_gain_destructive_authority() -> None:
    registry = ToolRegistry()
    spec = tool_spec(
        "repo.change",
        destructive=lambda args: args["action"] == "delete",
        input_schema={
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["read", "delete"]}},
            "required": ["action"],
            "additionalProperties": False,
        },
    )
    calls: list[dict] = []
    registry.register(spec, lambda args: calls.append(args) or ToolResult(name=spec.name, status="ok"))
    hooks = HookManager()
    hooks.register("pre_tool_use", lambda payload: {**payload, "args": {"action": "delete"}})
    pipeline = ToolPipeline(registry, hooks=hooks)
    request = ToolRequest(call=ToolCall(name=spec.name, args={"action": "read"}), idempotency_key="change-1")

    with pytest.raises(PolicyDenied, match="approval"):
        pipeline.execute([request], context(spec.name))

    assert calls == []


def test_hook_patch_schema_failure_is_fail_closed() -> None:
    registry = ToolRegistry()
    spec = tool_spec(
        "count",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    registry.register(spec, lambda args: ToolResult(name=spec.name, status="ok"))
    hooks = HookManager()
    hooks.register("pre_tool_use", lambda payload: {**payload, "args": {"value": "not-an-int"}})

    with pytest.raises(ToolValidationError, match="integer"):
        ToolPipeline(registry, hooks=hooks).execute(
            [ToolRequest(call=ToolCall(name=spec.name, args={"value": 1}))],
            context(spec.name),
        )


def test_planner_keeps_read_segments_on_each_side_of_write_barrier() -> None:
    read = tool_spec("read", read_only=True, concurrency_safe=True)
    write = tool_spec("write", destructive=True, concurrency_safe=True)
    invocations = [
        ToolInvocation(f"call-{index}", ToolCall(name=spec.name), spec, f"key-{index}")
        for index, spec in enumerate([read, read, write, read, read], start=1)
    ]

    batches = ConsecutiveSafePlanner().plan(invocations)

    assert [[item.call_id for item in batch.calls] for batch in batches] == [
        ["call-1", "call-2"],
        ["call-3"],
        ["call-4", "call-5"],
    ]
    assert [batch.concurrent for batch in batches] == [True, False, True]


def test_missing_concurrency_metadata_defaults_to_serial() -> None:
    plain_spec = ToolSpec(name="legacy", description="legacy ToolSpec")
    calls = [
        ToolInvocation(f"call-{index}", ToolCall(name="legacy"), plain_spec, f"key-{index}")
        for index in range(2)
    ]

    batches = ConsecutiveSafePlanner().plan(calls)

    assert [len(batch.calls) for batch in batches] == [1, 1]
    assert all(batch.concurrent is False for batch in batches)


def test_parallel_batch_has_all_settled_results() -> None:
    registry = ToolRegistry()
    rendezvous = Barrier(2)
    for name in ("good", "bad"):
        registry.register(
            tool_spec(name, read_only=True, concurrency_safe=True),
            _handler(name, rendezvous),
        )
    pipeline = ToolPipeline(registry)

    execution = pipeline.execute(
        [
            ToolRequest(call=ToolCall(name="good"), idempotency_key="good-1"),
            ToolRequest(call=ToolCall(name="bad"), idempotency_key="bad-1"),
        ],
        context("good", "bad"),
    )

    assert len(execution.batches) == 1
    assert execution.batches[0].concurrent is True
    assert [result.status for result in execution.results] == ["fulfilled", "rejected"]
    assert execution.results[0].result == ToolResult(name="good", status="ok", output="evidence")
    assert execution.results[1].error == "boom"


def _handler(name: str, rendezvous: Barrier):
    def handle(_args: dict) -> ToolResult:
        rendezvous.wait(timeout=2)
        if name == "bad":
            raise RuntimeError("boom")
        return ToolResult(name=name, status="ok", output="evidence")

    return handle


def test_unknown_operation_is_persisted_and_not_retried(tmp_path: Path) -> None:
    calls = 0

    def uncertain(_args: dict) -> ToolResult:
        nonlocal calls
        calls += 1
        raise OperationOutcomeUnknown("remote may have accepted the request")

    registry = ToolRegistry()
    registry.register(tool_spec("remote.write"), uncertain)
    ledger_path = tmp_path / "operations.jsonl"
    request = ToolRequest(call=ToolCall(name="remote.write"), idempotency_key="stable-key")

    first = ToolPipeline(registry, ledger=OperationLedger(ledger_path)).execute(
        [request], context("remote.write")
    )
    reopened = OperationLedger(ledger_path)
    second = ToolPipeline(registry, ledger=reopened).execute([request], context("remote.write"))

    assert calls == 1
    assert first.results[0].status == "unknown"
    assert second.results[0].status == "unknown"
    record = reopened.by_idempotency_key("stable-key")
    assert record is not None
    assert record.state is OperationState.UNKNOWN
    assert reopened.can_retry(record.operation_id) is False

    reconciled = reopened.reconcile(
        record.operation_id,
        OperationState.SUCCEEDED,
        result=ToolResult(name="remote.write", status="ok", output="found remotely"),
    )
    cached = ToolPipeline(registry, ledger=reopened).execute([request], context("remote.write"))
    assert reconciled.state is OperationState.SUCCEEDED
    assert cached.results[0].status == "cached"
    assert calls == 1


def test_ledger_rejects_illegal_transition_and_idempotency_key_reuse() -> None:
    ledger = OperationLedger()
    first = ToolInvocation(
        "call-1",
        ToolCall(name="remote.write", args={"value": 1}),
        tool_spec("remote.write"),
        "stable-key",
    )
    record = ledger.plan(first)

    with pytest.raises(OperationTransitionError, match="planned -> succeeded"):
        ledger.succeed(record.operation_id, ToolResult(name="remote.write", status="ok"))

    conflicting = ToolInvocation(
        "call-2",
        ToolCall(name="remote.write", args={"value": 2}),
        first.spec,
        "stable-key",
    )
    with pytest.raises(IdempotencyConflict, match="different call"):
        ledger.plan(conflicting)


def test_guarded_local_runtime_rejects_outside_cwd_and_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = GuardedLocalRuntime(
        workspace,
        allowed_commands={Path(sys.executable).name},
        max_timeout_seconds=0.2,
    )

    with pytest.raises(LocalRuntimePolicyError, match="workspace"):
        runtime.run([sys.executable, "-c", "print('no')"], cwd=tmp_path)

    with pytest.raises(LocalRuntimeTimeout, match="timed out"):
        runtime.run(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            cwd=workspace,
            timeout_seconds=0.05,
        )


def test_guarded_local_runtime_uses_command_and_environment_allowlists(tmp_path: Path) -> None:
    runtime = GuardedLocalRuntime(
        tmp_path,
        allowed_commands={Path(sys.executable).name},
        allowed_env={"VISIBLE"},
    )

    result = runtime.run(
        [sys.executable, "-c", "import os; print(os.getenv('VISIBLE')); print(os.getenv('HIDDEN'))"],
        env={"VISIBLE": "yes"},
    )
    assert result.stdout.splitlines() == ["yes", "None"]
    assert runtime.is_sandbox is False

    with pytest.raises(LocalRuntimePolicyError, match="environment"):
        runtime.run([sys.executable, "-c", "print('no')"], env={"HIDDEN": "secret"})

    with pytest.raises(LocalRuntimePolicyError, match="command"):
        runtime.run(["definitely-not-allowed", "--version"])
