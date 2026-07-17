from __future__ import annotations

import json

from crazy_harness.core.models import ModelMessage, ModelProvider
from crazy_harness.core.tools import ToolCall, ToolRegistry


def run_naive_loop(model: ModelProvider, tools: ToolRegistry, *, max_steps: int = 10) -> list[dict]:
    """Deliberately fragile loop used only as a learning baseline."""

    messages = [ModelMessage(role="user", content="Inspect the repository and collect release evidence.")]
    trace: list[dict] = []
    for step in range(1, max_steps + 1):
        response = model.complete(messages)
        action = json.loads(response.content)
        trace.append({"step": step, "kind": "model", "action": action})
        if action["type"] == "stop":
            trace.append({"step": step, "kind": "stop", "reason": action["reason"]})
            break
        if action["type"] == "call_tool":
            result = tools.call(ToolCall(name=action["tool_name"], args=action.get("tool_args", {})))
            trace.append({"step": step, "kind": "tool", "result": result.model_dump(mode="json")})
            messages.append(ModelMessage(role="user", content=result.model_dump_json()))
    return trace
