from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentAction(BaseModel):
    """Structured action returned by a model call."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "call_tool",
        "send_message",
        "emit_artifact",
        "continue",
        "wait_for_event",
        "submit_output",
        "report_blocked",
        "stop",
        "request_human",
    ]
    reason: str
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    receiver: str | None = None
    message: dict[str, Any] = Field(default_factory=dict)
    artifact: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_action_fields(self) -> "AgentAction":
        if self.type == "call_tool" and not self.tool_name:
            raise ValueError("tool_name is required for call_tool")
        if self.type == "send_message" and not self.receiver:
            raise ValueError("receiver is required for send_message")
        if self.type == "emit_artifact" and not self.artifact:
            raise ValueError("artifact is required for emit_artifact")
        return self
