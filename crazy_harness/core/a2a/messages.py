from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class AgentCard(BaseModel):
    agent_id: str
    role: str
    capabilities: list[str] = Field(default_factory=list)
    input_events: list[str] = Field(default_factory=list)
    output_artifacts: list[str] = Field(default_factory=list)
    max_concurrency: int = 1


class A2AMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: f"msg_{uuid4().hex}")
    task_id: str
    context_id: str
    sender: str
    receiver: str
    performative: str
    instruction: str
    brief: str = ""
    context_refs: list[str] = Field(default_factory=list)
    expected_output: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)
    contract_version: int = 1
    depth: int = 0
    intent: Literal["delegate", "evidence", "review", "revision", "block", "progress"] = "delegate"
