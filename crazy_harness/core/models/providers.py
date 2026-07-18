from __future__ import annotations

import json
import os
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from crazy_harness.core.events import Event


class ModelMessage(BaseModel):
    role: str
    content: str


class ModelResponse(BaseModel):
    content: str
    raw: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    provider_response_id: str | None = None
    provider_model: str | None = None
    finish_reason: str | None = None
    system_fingerprint: str | None = None

    def audit_payload(self) -> dict[str, Any]:
        """Return the durable response envelope without private provider internals."""

        return {
            "provider_response_id": self.provider_response_id,
            "provider_model": self.provider_model,
            "finish_reason": self.finish_reason,
            "system_fingerprint": self.system_fingerprint,
            "tool_calls": self.tool_calls,
        }


class ModelProvider(Protocol):
    def complete(
        self,
        messages: list[ModelMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResponse:
        ...


class ModelCallAuthority(Protocol):
    def recover_unresolved(self, *, request_event: Event) -> None: ...

    def complete(
        self,
        *,
        request_event: Event,
        provider: ModelProvider,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
        response_schema: dict[str, Any] | None,
    ) -> ModelResponse: ...

    def reconcile(
        self, *, request_event: Event, completion_event: Event
    ) -> bool: ...


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
        thinking_mode: Literal["enabled", "disabled"] = "disabled",
        max_tokens: int = 4096,
        user_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if max_tokens < 1:
            raise ValueError("DeepSeek max_tokens must be positive")
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        self.timeout_seconds = timeout_seconds
        self.thinking_mode = thinking_mode
        self.max_tokens = max_tokens
        self.user_id = user_id
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
            "thinking": {"type": self.thinking_mode},
            "max_tokens": self.max_tokens,
        }
        if self.user_id:
            payload["user_id"] = self.user_id
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

        choice = raw["choices"][0]
        content, tool_calls = normalize_openai_message(choice["message"])
        return ModelResponse(
            content=content,
            raw=raw,
            usage=raw.get("usage", {}),
            tool_calls=tool_calls,
            provider_response_id=raw.get("id"),
            provider_model=raw.get("model", self.model),
            finish_reason=choice.get("finish_reason"),
            system_fingerprint=raw.get("system_fingerprint"),
        )


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
