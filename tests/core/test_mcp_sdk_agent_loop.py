import json

import pytest

pytest.importorskip("mcp")
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from crazy_harness.core.agents import AgentLoop, AssignmentContract
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.capabilities import (
    CAPABILITY_SEARCH_TOOL_NAME,
    CapabilityCatalog,
    CapabilityCompiler,
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySearchService,
    MCPToolGrant,
    MCPToolMount,
    SDKSessionMCPClient,
)
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.models import ModelResponse
from crazy_harness.core.tools import ToolRegistry
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


def _server(remote_calls: list[str]) -> FastMCP:
    server = FastMCP("docs")

    @server.tool()
    def lookup(query: str) -> CallToolResult:
        remote_calls.append(query)
        return CallToolResult(
            content=[TextContent(type="text", text=f"found:{query}")],
            structuredContent={"answer": 42, "query": query},
            _meta={"secret": "client-only-secret"},
        )

    return server


def test_official_sdk_adapter_drops_private_meta_from_model_visible_result():
    remote_calls: list[str] = []
    server = _server(remote_calls)
    client = SDKSessionMCPClient(
        server_name="docs",
        session_factory=lambda: create_connected_server_and_client_session(server),
    )

    tools = client.snapshot_tools()
    result = client.invoke_tool("lookup", {"query": "leases"})

    assert [tool.name for tool in tools] == ["lookup"]
    assert result.structured_content == {"answer": 42, "query": "leases"}
    assert "client-only-secret" not in result.model_dump_json()
    assert remote_calls == ["leases"]


def test_agent_searches_then_calls_an_official_mcp_tool_through_the_normal_pipeline(
    tmp_path,
):
    remote_calls: list[str] = []
    server = _server(remote_calls)
    client = SDKSessionMCPClient(
        server_name="docs",
        session_factory=lambda: create_connected_server_and_client_session(server),
    )
    tools = ToolRegistry()
    catalog = CapabilityCatalog()
    mount = MCPToolMount(
        client,
        grants={
            "lookup": MCPToolGrant(
                side_effect_level="none",
                approval_required=False,
                is_read_only=True,
                is_concurrency_safe=True,
            )
        },
    )
    mount.refresh(tools, catalog)
    remote_name = "mcp.docs.lookup"
    search = CapabilitySearchService(
        catalog,
        allowed_names={remote_name},
        max_results=2,
    )
    search.install(tools)
    search_spec = tools.spec(CAPABILITY_SEARCH_TOOL_NAME)
    catalog.register(
        CapabilityDefinition(
            name=search_spec.name,
            kind=CapabilityKind.FUNCTION,
            description=search_spec.description,
            input_schema=search_spec.input_schema,
        )
    )

    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(
        Event(
            run_id="run-mcp",
            task_id="task-mcp",
            type="assignment.created",
            source="coordinator",
        )
    )
    model = _SequenceModel(
        [
            json.dumps(
                {
                    "type": "call_tool",
                    "reason": "discover the remote dossier lookup",
                    "tool_name": CAPABILITY_SEARCH_TOOL_NAME,
                    "tool_args": {"query": "remote dossier lookup"},
                }
            ),
            json.dumps(
                {
                    "type": "call_tool",
                    "reason": "read the remote dossier",
                    "tool_name": remote_name,
                    "tool_args": {"query": "leases"},
                }
            ),
            json.dumps({"type": "stop", "reason": "remote evidence collected"}),
        ]
    )
    all_names = frozenset(spec.name for spec in tools.specs())
    loop = AgentLoop(
        agent_id="researcher",
        task_id="task-mcp",
        model=model,
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
        assignment_contract=AssignmentContract(
            goal="inspect the assigned record",
            exit_criteria=("collect remote evidence",),
            output_schema={"type": "object"},
        ),
        tool_pipeline=ToolPipeline(tools),
        policy_context=PolicyContext(
            agent_id="researcher",
            assignment_id="task-mcp",
            mode="scripted",
            allowed_tools=all_names,
        ),
        capability_compiler=CapabilityCompiler(
            catalog,
            inline_limit=1,
            search_limit=1,
        ),
        capability_always_include=(CAPABILITY_SEARCH_TOOL_NAME,),
    )

    loop.run_until_stop(max_steps=3)

    events = event_log.read_all()
    manifests = [
        event.payload["manifest"]
        for event in events
        if event.type == "capability.manifest.compiled"
    ]
    remote_result = next(
        event.payload["result"]
        for event in events
        if event.type == "tool.completed"
        and event.payload["result"]["name"] == remote_name
    )
    assert remote_name not in manifests[0]["disclosed_names"]
    assert remote_name in manifests[1]["disclosed_names"]
    assert manifests[1]["kinds"][remote_name] == "mcp"
    assert manifests[1]["providers"][remote_name] == "mcp:docs"
    assert remote_name not in model.tools_by_call[0]
    assert remote_name in model.tools_by_call[1]
    assert "client-only-secret" not in json.dumps(remote_result)
    assert remote_calls == ["leases"]
