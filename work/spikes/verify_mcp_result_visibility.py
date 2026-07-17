from mcp.types import CallToolResult, TextContent

result = CallToolResult(
    content=[TextContent(type="text", text="visible")],
    structuredContent={"answer": 42},
    _meta={"secret": "client-only"},
)
payload = result.model_dump(by_alias=True)
assert payload["content"][0]["text"] == "visible"
assert payload["structuredContent"] == {"answer": 42}
assert payload["_meta"] == {"secret": "client-only"}
print("MCP visible content and client-only metadata are separable: ok")
