import json

from crazy_harness.core.agents import AgentLoop, AssignmentContract, CompletionGate, NudgeBudget
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.capabilities import (
    CapabilityCatalog,
    CapabilityCompiler,
    CapabilityDefinition,
    CapabilityKind,
)
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.models import FakeModelProvider, ModelResponse
from crazy_harness.core.prompts import PromptPack, RuntimeManifest
from crazy_harness.core.tools import ToolRegistry, ToolResult, ToolSpec
from crazy_harness.core.tools.policy import PolicyContext


def test_agent_loop_calls_tool_and_records_result(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    tools = ToolRegistry()
    tools.register(
        ToolSpec(name="echo", description="echo text"),
        lambda args: ToolResult(name="echo", status="ok", output=args["text"]),
    )
    model = FakeModelProvider(
        [
            json.dumps(
                {
                    "type": "call_tool",
                    "reason": "Need evidence",
                    "tool_name": "echo",
                    "tool_args": {"text": "ok"},
                }
            ),
            json.dumps({"type": "stop", "reason": "Done"}),
        ]
    )
    event_log.append(Event(run_id="r1", task_id="t1", type="seed", source="test"))
    loop = AgentLoop(
        agent_id="coordinator",
        model=model,
        event_log=event_log,
        artifact_store=artifact_store,
        tool_registry=tools,
    )

    loop.run_until_stop(max_steps=2)

    event_types = [event.type for event in event_log.read_all()]
    assert event_types[0] == "seed"
    assert event_types.count("model.completed") == 2
    assert "agent.command.validated" in event_types
    assert "tool.completed" in event_types
    assert event_types[-1] == "agent.stopped"


def test_agent_loops_are_isolated_by_assignment_task_id(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(Event(run_id="r1", task_id="scout-a1", type="assignment.created", source="coordinator"))
    event_log.append(Event(run_id="r1", task_id="builder-a2", type="assignment.created", source="coordinator"))
    tools = ToolRegistry()
    tools.register(
        ToolSpec(name="echo", description="echo"),
        lambda args: ToolResult(name="echo", status="ok", output=args["text"]),
    )
    scout = AgentLoop(
        agent_id="scout",
        task_id="scout-a1",
        model=FakeModelProvider([json.dumps({"type": "stop", "reason": "scout done"})]),
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
    )
    builder = AgentLoop(
        agent_id="builder",
        task_id="builder-a2",
        model=FakeModelProvider(
            [
                json.dumps({"type": "call_tool", "reason": "build evidence", "tool_name": "echo", "tool_args": {"text": "ok"}}),
                json.dumps({"type": "stop", "reason": "builder done"}),
            ]
        ),
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
    )

    scout.run_until_stop()
    builder.run_until_stop()

    assert event_log.read_all(task_id="scout-a1")[-1].type == "agent.stopped"
    assert any(event.type == "tool.completed" for event in event_log.read_all(task_id="builder-a2"))


def test_completion_gate_turns_premature_stop_into_bounded_nudge(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(Event(run_id="r1", task_id="t1", type="assignment.created", source="coordinator"))
    tools = ToolRegistry()
    tools.register(
        ToolSpec(name="echo", description="collect evidence"),
        lambda args: ToolResult(name="echo", status="ok", output="proof"),
    )
    model = FakeModelProvider(
        [
            json.dumps({"type": "stop", "reason": "done too early"}),
            json.dumps({"type": "call_tool", "reason": "repair evidence", "tool_name": "echo", "tool_args": {}}),
            json.dumps({"type": "stop", "reason": "done with evidence"}),
        ]
    )
    loop = AgentLoop(
        agent_id="builder",
        task_id="t1",
        model=model,
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
        assignment_contract=AssignmentContract(
            goal="collect proof",
            exit_criteria=("proof exists",),
            output_schema={"type": "object"},
            evidence_requirements=("echo",),
        ),
        completion_gate=CompletionGate(),
        nudge_budget=NudgeBudget(missing_evidence=1),
    )

    loop.run_until_stop(max_steps=3)

    events = event_log.read_all(task_id="t1")
    assert [event.type for event in events].count("agent.nudged") == 1
    assert events[-1].type == "agent.stopped"
    assert model.call_count == 3


def test_continue_action_completes_its_turn_and_advances_to_a_new_model_call(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(Event(run_id="r1", task_id="t1", type="seed", source="test"))
    model = FakeModelProvider(
        [
            json.dumps({"type": "continue", "reason": "revise the plan"}),
            json.dumps({"type": "stop", "reason": "done"}),
        ]
    )
    loop = AgentLoop(
        agent_id="generalist",
        task_id="t1",
        model=model,
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=ToolRegistry(),
    )

    loop.run_until_stop(max_steps=2)

    event_types = [event.type for event in event_log.read_all(task_id="t1")]
    assert "agent.continued" in event_types
    assert event_types[-1] == "agent.stopped"
    assert model.call_count == 2


def test_assignment_tool_call_budget_denies_the_next_external_effect(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(Event(run_id="r1", task_id="t1", type="assignment.created", source="coordinator"))
    executed: list[str] = []
    tools = ToolRegistry()
    tools.register(
        ToolSpec(name="effect", description="observable external effect"),
        lambda args: (
            executed.append(args["value"])
            or ToolResult(name="effect", status="ok", output=args["value"])
        ),
    )
    model = FakeModelProvider(
        [
            json.dumps(
                {
                    "type": "call_tool",
                    "reason": "first allowed effect",
                    "tool_name": "effect",
                    "tool_args": {"value": "first"},
                }
            ),
            json.dumps(
                {
                    "type": "call_tool",
                    "reason": "second effect exceeds the contract",
                    "tool_name": "effect",
                    "tool_args": {"value": "second"},
                }
            ),
        ]
    )
    loop = AgentLoop(
        agent_id="generalist",
        task_id="t1",
        model=model,
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
        assignment_contract=AssignmentContract(
            goal="perform one effect",
            exit_criteria=("first effect exists",),
            output_schema={"type": "object"},
            budgets={"tool_calls": 1},
        ),
    )

    loop.run_until_stop(max_steps=2)

    events = event_log.read_all(task_id="t1")
    denied = next(event for event in events if event.type == "agent.action.denied")
    assert executed == ["first"]
    assert sum(event.type == "operation.started" for event in events) == 1
    assert denied.payload == {
        "turn_id": "turn_2",
        "reason": "tool_call_budget_exhausted",
        "tool_name": "effect",
        "used": 1,
        "limit": 1,
    }


class _RecordingModel:
    def __init__(self, response: str) -> None:
        self.response = response
        self.received_tools: list[dict] | None = None
        self.received_messages = None

    def complete(self, messages, *, tools=None, response_schema=None):
        self.received_messages = messages
        self.received_tools = tools
        return ModelResponse(content=self.response)


def _capability_compiler_for(tools: ToolRegistry) -> CapabilityCompiler:
    catalog = CapabilityCatalog()
    for spec in tools.specs():
        catalog.register(
            CapabilityDefinition(
                name=spec.name,
                kind=CapabilityKind.FUNCTION,
                description=spec.description,
                input_schema=spec.input_schema,
            )
        )
    return CapabilityCompiler(catalog)


def test_agent_loop_passes_only_compiled_capability_schemas_to_the_model(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(Event(run_id="r1", task_id="t1", type="assignment.created", source="coordinator"))
    tools = ToolRegistry()
    tools.register(ToolSpec(name="repo.read", description="read source"), lambda _: ToolResult(name="repo.read", status="ok"))
    tools.register(ToolSpec(name="shell.admin", description="admin host"), lambda _: ToolResult(name="shell.admin", status="ok"))
    model = _RecordingModel(json.dumps({"type": "stop", "reason": "done"}))
    loop = AgentLoop(
        agent_id="generalist",
        task_id="t1",
        model=model,
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
        prompt_pack=PromptPack(
            role_section="Repository reader",
            agent_card_section="Read only",
            task_brief_section="Read source",
            runtime_manifest=RuntimeManifest(
                agent_id="generalist",
                task_id="t1",
                mode="scripted",
                available_tools=tools.specs(),
            ),
        ),
        assignment_contract=AssignmentContract(
            goal="read source",
            exit_criteria=("source inspected",),
            output_schema={"type": "object"},
        ),
        policy_context=PolicyContext(
            agent_id="generalist",
            assignment_id="t1",
            mode="scripted",
            allowed_tools={"repo.read"},
        ),
        capability_compiler=_capability_compiler_for(tools),
    )

    loop.run_once()

    names = [tool["function"]["name"] for tool in model.received_tools or []]
    manifest_event = next(event for event in event_log.read_all() if event.type == "capability.manifest.compiled")
    system_prompt = next(message.content for message in model.received_messages if message.role == "system")
    assert names == ["repo.read"]
    assert "repo.read" in system_prompt
    assert "shell.admin" not in system_prompt
    assert manifest_event.payload["manifest"]["disclosed_names"] == ["repo.read"]
    assert manifest_event.payload["manifest"]["excluded_names"] == ["shell.admin"]


def test_agent_loop_denies_a_registered_tool_that_was_not_disclosed_this_turn(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(Event(run_id="r1", task_id="t1", type="assignment.created", source="coordinator"))
    effects: list[str] = []
    tools = ToolRegistry()
    tools.register(ToolSpec(name="repo.read", description="read source"), lambda _: ToolResult(name="repo.read", status="ok"))
    tools.register(
        ToolSpec(name="shell.admin", description="admin host"),
        lambda _: effects.append("admin") or ToolResult(name="shell.admin", status="ok"),
    )
    model = _RecordingModel(
        json.dumps(
            {
                "type": "call_tool",
                "reason": "attempt hidden capability",
                "tool_name": "shell.admin",
                "tool_args": {},
            }
        )
    )
    loop = AgentLoop(
        agent_id="generalist",
        task_id="t1",
        model=model,
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
        policy_context=PolicyContext(
            agent_id="generalist",
            assignment_id="t1",
            mode="scripted",
            allowed_tools={"repo.read"},
        ),
        capability_compiler=_capability_compiler_for(tools),
    )

    loop.run_once()

    denied = next(event for event in event_log.read_all() if event.type == "agent.action.denied")
    assert effects == []
    assert denied.payload["reason"] == "tool_not_disclosed"
    assert denied.payload["tool_name"] == "shell.admin"
