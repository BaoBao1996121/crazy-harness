import json

import httpx

from crazy_harness.core.models.providers import normalize_openai_message

seen = {}
def respond(request: httpx.Request) -> httpx.Response:
    seen.update(json.loads(request.content))
    message = {"content": "inspect", "tool_calls": [{"function": {"name": "repo.read", "arguments": "{\"path\":\"app.py\"}"}}]}
    return httpx.Response(200, json={"choices": [{"message": message}], "usage": {"total_tokens": 8}})
with httpx.Client(transport=httpx.MockTransport(respond)) as client:
    raw = client.post("https://api.deepseek.com/chat/completions", json={"model": "deepseek-v4-flash", "thinking": {"type": "disabled"}, "messages": []}).json()
content, calls = normalize_openai_message(raw["choices"][0]["message"])
assert seen["thinking"] == {"type": "disabled"} and len(calls) == 1
assert json.loads(content)["tool_name"] == "repo.read"
print("deepseek_non_thinking_tool_call=PASS")
