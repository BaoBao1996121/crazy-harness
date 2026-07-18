import json

import httpx

from crazy_harness.core.models import ModelMessage, ModelResponse
from crazy_harness.core.models.providers import DeepSeekOpenAIProvider, normalize_openai_message


def test_native_tool_call_is_normalized_to_internal_action():
    message = {
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "repo.read", "arguments": '{"path":"app.py"}'},
            }
        ],
    }

    content, tool_calls = normalize_openai_message(message)
    action = json.loads(content)

    assert action["type"] == "call_tool"
    assert action["tool_name"] == "repo.read"
    assert action["tool_args"] == {"path": "app.py"}
    assert tool_calls[0]["id"] == "call_1"


def test_invalid_native_arguments_remain_invalid_for_command_validation():
    message = {
        "content": None,
        "tool_calls": [{"id": "call_2", "type": "function", "function": {"name": "repo.read", "arguments": "not-json"}}],
    }

    content, _ = normalize_openai_message(message)

    assert isinstance(json.loads(content)["tool_args"], str)


def test_multiple_native_tool_calls_are_rejected_instead_of_silently_truncated():
    message = {
        "content": "run both",
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "repo.read", "arguments": "{}"}},
            {"id": "call_2", "type": "function", "function": {"name": "test.run", "arguments": "{}"}},
        ],
    }

    content, tool_calls = normalize_openai_message(message)

    assert json.loads(content)["type"] == "invalid_tool_call_batch"
    assert len(tool_calls) == 2


def test_deepseek_provider_sends_openai_tool_contract_and_normalizes_response():
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer test-key"
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == 4096
        assert payload["user_id"] == "run-1:generalist"
        assert payload["tools"][0]["function"]["name"] == "repo.read"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "repo.read", "arguments": '{"path":"calculator.py"}'},
                                }
                            ],
                        }
                    }
                ],
                "id": "response-1",
                "model": "deepseek-v4-flash",
                "system_fingerprint": "fp-test",
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    provider = DeepSeekOpenAIProvider(
        api_key="test-key",
        base_url="https://deepseek.invalid",
        user_id="run-1:generalist",
        transport=httpx.MockTransport(respond),
    )
    response = provider.complete(
        [ModelMessage(role="user", content="inspect")],
        tools=[
            {
                "type": "function",
                "function": {"name": "repo.read", "description": "read", "parameters": {"type": "object"}},
            }
        ],
    )

    assert json.loads(response.content)["tool_name"] == "repo.read"
    assert response.usage["prompt_tokens"] == 10
    assert response.provider_response_id == "response-1"
    assert response.provider_model == "deepseek-v4-flash"
    assert response.finish_reason == "tool_calls"
    assert response.system_fingerprint == "fp-test"


def test_model_audit_payload_excludes_raw_reasoning_content():
    response = ModelResponse(
        content='{"type":"continue","reason":"safe"}',
        raw={"choices": [{"message": {"reasoning_content": "private chain"}}]},
        usage={"total_tokens": 7},
        provider_response_id="response-1",
        provider_model="deepseek-v4-flash",
        finish_reason="stop",
    )

    encoded = json.dumps(response.audit_payload())

    assert "private chain" not in encoded
    assert response.audit_payload()["provider_response_id"] == "response-1"
