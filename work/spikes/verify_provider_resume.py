from crazy_harness.core.models import FakeModelProvider, ModelMessage

provider = FakeModelProvider(['{"type":"send_message","reason":"ask","receiver":"scout"}', '{"type":"stop","reason":"done"}'])
messages = [ModelMessage(role="user", content="task")]
first = provider.complete(messages)
second = provider.complete(messages)
assert "send_message" in first.content
assert "stop" in second.content
assert provider.call_count == 2
print("provider-resume-ok")
