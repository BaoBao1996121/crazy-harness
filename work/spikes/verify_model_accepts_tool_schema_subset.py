from crazy_harness.core.models import FakeModelProvider

model = FakeModelProvider(['{"type":"stop","reason":"done"}'])
subset = [{"type": "function", "function": {"name": "repo.read", "description": "read", "parameters": {}}}]
response = model.complete([], tools=subset)
assert response.content.startswith('{"type":"stop"')
assert model.call_count == 1
print("model provider accepts a compiled tool-schema subset: ok")
