import json

import pytest

from crazy_harness.core.capabilities import (
    CapabilityCatalog,
    CapabilityKind,
    MCPCallResult,
    MCPToolDescriptor,
    MCPToolGrant,
    MCPToolMount,
)
from crazy_harness.core.tools import ToolCall, ToolRegistry, ToolResult, ToolSpec


class _FakeMCPClient:
    server_name = "docs"

    def __init__(self, tools: list[MCPToolDescriptor]) -> None:
        self.tools = tools
        self.calls: list[tuple[str, dict]] = []
        self.result = MCPCallResult(
            content=({"type": "text", "text": "remote proof"},),
            structured_content={"answer": 42},
        )

    def snapshot_tools(self) -> tuple[MCPToolDescriptor, ...]:
        return tuple(self.tools)

    def invoke_tool(self, name: str, arguments: dict) -> MCPCallResult:
        self.calls.append((name, arguments))
        return self.result


def _descriptor(name: str, description: str) -> MCPToolDescriptor:
    return MCPToolDescriptor(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def _read_grant() -> MCPToolGrant:
    return MCPToolGrant(
        side_effect_level="none",
        approval_required=False,
        is_read_only=True,
        is_concurrency_safe=True,
    )


def test_mount_namespaces_only_granted_tools_and_preserves_mcp_identity():
    client = _FakeMCPClient(
        [
            _descriptor("lookup", "lookup remote dossier"),
            _descriptor("admin.delete", "delete remote tenant"),
        ]
    )
    registry = ToolRegistry()
    catalog = CapabilityCatalog()
    mount = MCPToolMount(client, grants={"lookup": _read_grant()})

    snapshot = mount.refresh(registry, catalog)

    assert snapshot.mounted_names == ("mcp.docs.lookup",)
    assert snapshot.excluded_remote_names == ("admin.delete",)
    assert registry.has("mcp.docs.lookup")
    assert not registry.has("mcp.docs.admin.delete")
    definition = catalog.disclose(["mcp.docs.lookup"])[0]
    assert definition.kind is CapabilityKind.MCP
    assert definition.provider == "mcp:docs"

    result = registry.call(
        ToolCall(name="mcp.docs.lookup", args={"query": "recovery"})
    )
    payload = json.loads(result.output)
    assert result.status == "ok"
    assert payload["structured_content"] == {"answer": 42}
    assert client.calls == [("lookup", {"query": "recovery"})]


def test_refresh_replaces_the_mount_snapshot_and_removes_stale_tools():
    client = _FakeMCPClient([_descriptor("lookup", "lookup remote dossier")])
    registry = ToolRegistry()
    catalog = CapabilityCatalog()
    mount = MCPToolMount(
        client,
        grants={"lookup": _read_grant(), "summarize": _read_grant()},
    )
    mount.refresh(registry, catalog)
    client.tools = [_descriptor("summarize", "summarize remote dossier")]

    snapshot = mount.refresh(registry, catalog)

    assert snapshot.mounted_names == ("mcp.docs.summarize",)
    assert snapshot.removed_names == ("mcp.docs.lookup",)
    assert not registry.has("mcp.docs.lookup")
    assert not catalog.has("mcp.docs.lookup")
    assert registry.has("mcp.docs.summarize")
    assert catalog.has("mcp.docs.summarize")


def test_refresh_rejects_a_local_name_collision_before_mutating_registry():
    client = _FakeMCPClient([_descriptor("lookup", "lookup remote dossier")])
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="mcp.docs.lookup", description="local protected tool"),
        lambda _: ToolResult(name="mcp.docs.lookup", status="ok", output="local"),
    )
    catalog = CapabilityCatalog()
    mount = MCPToolMount(client, grants={"lookup": _read_grant()})

    with pytest.raises(ValueError, match="collision"):
        mount.refresh(registry, catalog)

    assert registry.call(ToolCall(name="mcp.docs.lookup")).output == "local"
    assert catalog.stubs() == []


def test_remote_error_becomes_a_tool_observation_instead_of_an_exception():
    client = _FakeMCPClient([_descriptor("lookup", "lookup remote dossier")])
    client.result = MCPCallResult(
        content=({"type": "text", "text": "remote unavailable"},),
        is_error=True,
    )
    registry = ToolRegistry()
    catalog = CapabilityCatalog()
    MCPToolMount(client, grants={"lookup": _read_grant()}).refresh(registry, catalog)

    result = registry.call(ToolCall(name="mcp.docs.lookup", args={"query": "x"}))

    assert result.status == "error"
    assert result.error == "MCP tool reported an error"
    assert "remote unavailable" in result.output
