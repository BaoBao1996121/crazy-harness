from crazy_harness.core.capabilities import CapabilityCatalog, CapabilityDefinition, CapabilityKind, InProcessMCPAdapter, SkillDefinition
from crazy_harness.core.tools import ToolRegistry, ToolResult, ToolSpec


def test_progressive_disclosure_keeps_full_schema_out_of_stubs():
    catalog = CapabilityCatalog()
    catalog.register(
        CapabilityDefinition(
            name="repo.read",
            kind=CapabilityKind.FUNCTION,
            description="read repository file",
            input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    )

    stub = catalog.stubs()[0]
    full = catalog.disclose(["repo.read"])[0]

    assert not hasattr(stub, "input_schema")
    assert full.input_schema["properties"]["path"]["type"] == "string"


def test_skill_can_reference_mcp_without_granting_permission():
    skill = SkillDefinition(
        name="release-check",
        description="collect release evidence",
        steps=["read source", "run tests"],
        capability_aliases=["repo.read", "mcp.test.run"],
    )

    assert "mcp.test.run" in skill.capability_aliases
    assert not hasattr(skill, "allowed_tools")


def test_in_process_mcp_adapter_executes_registered_tool():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="echo", description="echo"),
        lambda args: ToolResult(name="echo", status="ok", output=args["text"]),
    )
    adapter = InProcessMCPAdapter(server_name="lab", registry=registry)

    result = adapter.call("echo", {"text": "ok"})

    assert result.output == "ok"
    assert adapter.list_tools()[0].name == "echo"


def test_tool_search_uses_metadata_when_catalog_is_large():
    catalog = CapabilityCatalog()
    for name, description in [("repo.read", "read source file"), ("test.run", "run unit tests"), ("mail.send", "send email")]:
        catalog.register(CapabilityDefinition(name=name, kind=CapabilityKind.FUNCTION, description=description))

    matches = catalog.search("source read", limit=1)

    assert [item.name for item in matches] == ["repo.read"]
