import json

from crazy_harness.core.agents import AgentLoop, AssignmentContract
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.capabilities import (
    CAPABILITY_SEARCH_TOOL_NAME,
    CapabilityCatalog,
    CapabilityCompiler,
    CapabilitySearchResult,
    CapabilitySearchService,
)
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.models import ModelResponse
from crazy_harness.core.tools import ToolRegistry, ToolResult, ToolSpec
from crazy_harness.core.tools.pipeline import ToolPipeline
from crazy_harness.core.tools.policy import PolicyContext


class _SequenceModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.tools_by_call: list[list[str]] = []

    def complete(self, messages, *, tools=None, response_schema=None):
        self.tools_by_call.append(
            [item["function"]["name"] for item in tools or []]
        )
        return ModelResponse(content=self.responses.pop(0))


def test_capability_search_returns_only_authorized_stubs_without_full_schema():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="repo.read",
            description="read repository source",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        ),
        lambda _: ToolResult(name="repo.read", status="ok"),
    )
    registry.register(
        ToolSpec(
            name="shell.admin",
            description="administer the production host",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        ),
        lambda _: ToolResult(name="shell.admin", status="ok"),
    )
    service = CapabilitySearchService(
        CapabilityCatalog.from_tool_specs(registry.specs()),
        allowed_names={"repo.read"},
        max_results=3,
    )

    allowed = CapabilitySearchResult.model_validate_json(
        service.handle({"query": "repository source"}).output
    )
    denied = CapabilitySearchResult.model_validate_json(
        service.handle({"query": "production admin"}).output
    )

    assert [match.name for match in allowed.matches] == ["repo.read"]
    assert denied.matches == ()
    assert allowed.total_authorized == 1
    assert "input_schema" not in service.handle({"query": "source"}).output
    assert "shell.admin" not in service.handle({"query": "admin"}).output


def test_agent_searches_then_receives_and_executes_a_deferred_tool(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(
        Event(
            run_id="run-search",
            task_id="task-search",
            type="assignment.created",
            source="coordinator",
        )
    )
    effects: list[str] = []
    tools = ToolRegistry()
    tools.register(
        ToolSpec(name="repo.read", description="inspect repository source"),
        lambda _: ToolResult(name="repo.read", status="ok"),
    )
    tools.register(
        ToolSpec(name="deploy.audit", description="audit deployment evidence"),
        lambda _: effects.append("deploy.audit")
        or ToolResult(name="deploy.audit", status="ok", output="audit complete"),
    )
    tools.register(
        ToolSpec(name="mail.send", description="send a status message"),
        lambda _: ToolResult(name="mail.send", status="ok"),
    )

    base_names = frozenset(spec.name for spec in tools.specs())
    search = CapabilitySearchService(
        CapabilityCatalog.from_tool_specs(tools.specs()),
        allowed_names=base_names,
        max_results=2,
    )
    search.install(tools)
    all_names = frozenset(spec.name for spec in tools.specs())
    compiler = CapabilityCompiler(
        CapabilityCatalog.from_tool_specs(tools.specs()),
        inline_limit=1,
        search_limit=1,
    )
    model = _SequenceModel(
        [
            json.dumps(
                {
                    "type": "call_tool",
                    "reason": "find a deployment evidence tool",
                    "tool_name": CAPABILITY_SEARCH_TOOL_NAME,
                    "tool_args": {"query": "deployment audit"},
                }
            ),
            json.dumps(
                {
                    "type": "call_tool",
                    "reason": "collect the discovered evidence",
                    "tool_name": "deploy.audit",
                    "tool_args": {},
                }
            ),
            json.dumps({"type": "stop", "reason": "evidence collected"}),
        ]
    )
    loop = AgentLoop(
        agent_id="generalist",
        task_id="task-search",
        model=model,
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
        assignment_contract=AssignmentContract(
            goal="inspect the repository",
            exit_criteria=("collect evidence",),
            output_schema={"type": "object"},
        ),
        tool_pipeline=ToolPipeline(tools),
        policy_context=PolicyContext(
            agent_id="generalist",
            assignment_id="task-search",
            mode="scripted",
            allowed_tools=all_names,
        ),
        capability_compiler=compiler,
        capability_always_include=(CAPABILITY_SEARCH_TOOL_NAME,),
    )

    loop.run_until_stop(max_steps=3)

    events = event_log.read_all()
    manifests = [
        event for event in events if event.type == "capability.manifest.compiled"
    ]
    search_result = next(
        event
        for event in events
        if event.type == "tool.completed"
        and event.payload["result"]["name"] == CAPABILITY_SEARCH_TOOL_NAME
    )

    assert "deploy.audit" not in manifests[0].payload["manifest"]["disclosed_names"]
    assert "deploy.audit" in manifests[1].payload["manifest"]["disclosed_names"]
    assert (
        manifests[1].payload["manifest"]["reasons"]["deploy.audit"]
        == "explicit_recall"
    )
    assert (
        manifests[1].payload["manifest"]["recall_sources"]["deploy.audit"]
        == search_result.id
    )
    assert "deploy.audit" not in model.tools_by_call[0]
    assert "deploy.audit" in model.tools_by_call[1]
    assert effects == ["deploy.audit"]
