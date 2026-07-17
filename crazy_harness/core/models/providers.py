from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field


class ModelMessage(BaseModel):
    role: str
    content: str


class ModelResponse(BaseModel):
    content: str
    raw: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ModelProvider(Protocol):
    def complete(
        self,
        messages: list[ModelMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResponse:
        ...


class FakeModelProvider:
    """Deterministic provider for unit tests only."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def complete(
        self,
        messages: list[ModelMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResponse:
        if not self._responses:
            raise RuntimeError("FakeModelProvider has no responses left")
        self.call_count += 1
        return ModelResponse(content=self._responses.pop(0))


class DeepSeekOpenAIProvider:
    """OpenAI-compatible DeepSeek chat completions provider."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def complete(
        self,
        messages: list[ModelMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResponse:
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required for DeepSeekOpenAIProvider")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.model_dump() for message in messages],
        }
        if tools:
            payload["tools"] = tools
        if response_schema and not tools:
            payload["response_format"] = {"type": "json_object"}

        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            raw = response.json()

        content, tool_calls = normalize_openai_message(raw["choices"][0]["message"])
        return ModelResponse(content=content, raw=raw, usage=raw.get("usage", {}), tool_calls=tool_calls)


ScriptedModelProvider = FakeModelProvider


def normalize_openai_message(message: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Normalize one OpenAI-compatible message into an internal JSON action."""

    tool_calls = list(message.get("tool_calls") or [])
    if not tool_calls:
        return message.get("content") or "", []
    if len(tool_calls) != 1:
        invalid = {
            "type": "invalid_tool_call_batch",
            "reason": f"expected exactly one tool call, received {len(tool_calls)}",
        }
        return json.dumps(invalid, ensure_ascii=False), tool_calls

    function = tool_calls[0].get("function") or {}
    arguments = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass
    action = {
        "type": "call_tool",
        "reason": message.get("content") or "provider native tool call",
        "tool_name": function.get("name"),
        "tool_args": arguments,
    }
    return json.dumps(action, ensure_ascii=False), tool_calls
