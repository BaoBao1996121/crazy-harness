from crazy_harness.core.events import Event

events = [Event(run_id="r", task_id="a", type="agent.waiting", source="builder", payload={"correlation_id": "c"})]
waiting_index = len(events) - 1
assert not any(event.payload.get("correlation_id") == "c" for event in events[waiting_index + 1 :])
events.append(Event(run_id="r", task_id="a", type="a2a.peer.responded", source="scout", payload={"correlation_id": "c"}))
assert any(event.payload.get("correlation_id") == "c" for event in events[waiting_index + 1 :])
print("wait-resume-signal-ok")
