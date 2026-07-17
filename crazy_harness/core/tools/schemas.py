from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    use_when: str = ""
    do_not_use_when: str = ""
    side_effect_level: str = "none"
    approval_required: bool = False
    output_offload_policy: str = "auto"
    is_read_only: bool = False
    is_destructive: bool = False
    is_concurrency_safe: bool = False
    allowed_agents: set[str] | None = None
    allowed_assignments: set[str] | None = None
    allowed_modes: set[str] | None = None


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    name: str
    status: str
    output: str = ""
    error: str | None = None
    artifact_ref: str | None = None
