import os

import pytest

from crazy_harness.core.models import DeepSeekOpenAIProvider, ModelMessage


@pytest.mark.llm
def test_deepseek_provider_live_smoke():
    if os.getenv("CRAZY_RUN_LLM_TESTS") != "1" or not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("Set DEEPSEEK_API_KEY and CRAZY_RUN_LLM_TESTS=1 to run the live DeepSeek smoke test.")

    response = DeepSeekOpenAIProvider().complete(
        [
            ModelMessage(role="system", content="Return one JSON object."),
            ModelMessage(role="user", content='Return exactly {"status":"ok"}.'),
        ],
        response_schema={"type": "object", "properties": {"status": {"type": "string"}}},
    )

    assert response.content
    assert response.raw.get("choices")
