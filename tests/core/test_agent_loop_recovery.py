import json

import pytest

from crazy_harness.core.agents import AgentLoop, InjectedCrash
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.models import FakeModelProvider
from crazy_harness.core.tools import (
    OperationLedger,
    PolicyContext,
    ToolPipeline,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class CrashOnce:
    def __init__(self, target: str) -> None:
        self.target = target
        self.triggered = False

    def __call__(self, marker: str) -> None:
        if marker == self.target and not self.triggered:
            self.triggered = True
            raise InjectedCrash(marker)


def build_loop(tmp_path, responses, *, fault_injector=None, handler=None):
    event_log = EventLog(tmp_path / "events.jsonl")
    if not event_log.read_all():
        event_log.append(Event(run_id="r1", task_id="t1", type="seed", source="test"))
    tools = ToolRegistry()
    tools.register(
        ToolSpec(name="echo", description="echo text"),
        handler or (lambda args: ToolResult(name="echo", status="ok", output=args["text"])),
    )
    model = responses if isinstance(responses, FakeModelProvider) else FakeModelProvider(responses)
    loop = AgentLoop(
        agent_id="coordinator",
        model=model,
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
        fault_injector=fault_injector,
    )
    return loop, model, event_log


def test_invalid_command_never_calls_tool(tmp_path):
    calls = []
    loop, _, event_log = build_loop(
        tmp_path,
        [json.dumps({"type": "call_tool", "reason": "missing name"})],
        handler=lambda args: calls.append(args) or ToolResult(name="echo", status="ok"),
    )

    loop.run_once()

    assert calls == []
    assert event_log.read_all()[-1].type == "model.validation_failed"


def test_command_with_unknown_fields_fails_closed(tmp_path):
    calls = []
    loop, _, event_log = build_loop(
        tmp_path,
        [
            json.dumps(
                {
                    "type": "call_tool",
                    "reason": "evidence",
                    "tool_name": "echo",
                    "tool_args": {"text": "ok"},
                    "bypass_policy": True,
                }
            )
        ],
        handler=lambda args: calls.append(args) or ToolResult(name="echo", status="ok"),
    )

    loop.run_once()

    assert calls == []
    assert event_log.read_all()[-1].type == "model.validation_failed"


def test_recovery_reuses_persisted_model_response(tmp_path):
    responses = FakeModelProvider(
        [json.dumps({"type": "call_tool", "reason": "evidence", "tool_name": "echo", "tool_args": {"text": "ok"}})]
    )
    crash = CrashOnce("after_model_persisted")
    loop, model, _ = build_loop(tmp_path, responses, fault_injector=crash)

    with pytest.raises(InjectedCrash):
        loop.run_once()

    recovered, _, event_log = build_loop(tmp_path, model)
    recovered.run_once()

    assert model.call_count == 1
    assert any(event.type == "tool.completed" for event in event_log.read_all())


def test_unknown_external_effect_is_not_retried(tmp_path):
    calls = []

    def handler(args):
        calls.append(args)
        return ToolResult(name="echo", status="ok", output="ok")

    response = json.dumps({"type": "call_tool", "reason": "evidence", "tool_name": "echo", "tool_args": {"text": "ok"}})
    loop, model, _ = build_loop(tmp_path, [response], fault_injector=CrashOnce("after_tool_effect"), handler=handler)

    with pytest.raises(InjectedCrash):
        loop.run_once()

    recovered, _, event_log = build_loop(tmp_path, model, handler=handler)
    recovered.run_once()

    assert calls == [{"text": "ok"}]
    assert event_log.read_all()[-1].type == "operation.unknown"


def test_reopened_ledger_recovers_confirmed_effect_without_executing_it_twice(tmp_path):
    counter = tmp_path / "effect-count.txt"

    def effect(_args):
        count = int(counter.read_text() if counter.exists() else "0") + 1
        counter.write_text(str(count))
        return ToolResult(name="effect", status="ok", output="confirmed")

    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(Event(run_id="r1", task_id="t1", type="seed", source="test"))
    tools = ToolRegistry()
    tools.register(ToolSpec(name="effect", description="one external effect"), effect)
    context = PolicyContext(
        agent_id="builder",
        assignment_id="t1",
        mode="mock",
        allowed_tools=frozenset({"effect"}),
    )
    first = AgentLoop(
        agent_id="builder",
        task_id="t1",
        model=FakeModelProvider(
            [json.dumps({"type": "call_tool", "reason": "effect", "tool_name": "effect", "tool_args": {}})]
        ),
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
        tool_pipeline=ToolPipeline(tools, ledger=OperationLedger(tmp_path / "operations.jsonl")),
        policy_context=context,
        fault_injector=CrashOnce("after_tool_effect"),
    )

    with pytest.raises(InjectedCrash):
        first.run_once()

    resumed_model = FakeModelProvider([json.dumps({"type": "stop", "reason": "recovered"})])
    resumed = AgentLoop(
        agent_id="builder",
        task_id="t1",
        model=resumed_model,
        event_log=EventLog(tmp_path / "events.jsonl"),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
        tool_pipeline=ToolPipeline(tools, ledger=OperationLedger(tmp_path / "operations.jsonl")),
        policy_context=context,
    )
    resumed.run_until_stop(max_steps=2)

    events = event_log.read_all(task_id="t1")
    assert counter.read_text() == "1"
    assert any(event.payload.get("recovered_from_ledger") for event in events if event.type == "tool.completed")
    assert events[-1].type == "agent.stopped"
    assert resumed_model.call_count == 1
