from crazy_harness.core.tools import ToolCall, ToolRegistry, ToolResult, ToolSpec


def test_tool_registry_calls_handler():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="echo", description="echo args"),
        lambda args: ToolResult(name="echo", status="ok", output=args["text"]),
    )

    result = registry.call(ToolCall(name="echo", args={"text": "hi"}))

    assert result.status == "ok"
    assert result.output == "hi"
