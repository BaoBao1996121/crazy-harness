from crazy_harness.core.capabilities import CapabilityCatalog, CapabilityCompileRequest, CapabilityCompiler, CapabilityDefinition, CapabilityKind

catalog = CapabilityCatalog()
catalog.register(CapabilityDefinition(
    name="mcp.docs.search",
    kind=CapabilityKind.MCP,
    description="search remote documentation",
    provider="mcp:docs",
))
compiled = CapabilityCompiler(catalog, inline_limit=0, search_limit=1).compile(
    CapabilityCompileRequest(
        agent_id="researcher", assignment_id="t1", mode="scripted",
        query="search remote documentation", allowed_names=frozenset({"mcp.docs.search"}),
    )
)
assert compiled.manifest.disclosed_names == ("mcp.docs.search",)
assert compiled.definitions[0].kind is CapabilityKind.MCP
assert compiled.definitions[0].provider == "mcp:docs"
print("MCP capability compiler bridge: ok")
