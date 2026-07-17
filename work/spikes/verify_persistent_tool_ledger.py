from tempfile import TemporaryDirectory
from pathlib import Path

from crazy_harness.core.tools import ToolCall, ToolRegistry, ToolResult, ToolSpec
from crazy_harness.core.tools.pipeline import OperationLedger, OperationState, ToolPipeline, ToolRequest
from crazy_harness.core.tools.policy import PolicyContext

with TemporaryDirectory() as tmp:
    path = Path(tmp) / "operations.jsonl"
    tools = ToolRegistry()
    tools.register(ToolSpec(name="echo", description="echo"), lambda args: ToolResult(name="echo", status="ok", output="proof"))
    context = PolicyContext(agent_id="worker", assignment_id="t1", mode="mock", allowed_tools=frozenset({"echo"}))
    request = ToolRequest(ToolCall(name="echo"), call_id="c1", idempotency_key="stable-key")
    result = ToolPipeline(tools, ledger=OperationLedger(path)).execute([request], context).results[0]
    recovered = OperationLedger(path).by_idempotency_key("stable-key")
    assert result.status == "fulfilled" and recovered.state is OperationState.SUCCEEDED
    print("PASS: ToolPipeline result survives ledger reopen")
