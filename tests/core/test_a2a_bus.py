from crazy_harness.core.a2a import A2ABus, A2AMessage


def test_a2a_bus_routes_messages_by_receiver():
    bus = A2ABus()
    message = A2AMessage(
        task_id="t1",
        context_id="c1",
        sender="coordinator",
        receiver="scout",
        performative="request",
        instruction="Inspect diff",
    )

    bus.send(message)

    assert bus.receive("builder") == []
    assert bus.receive("scout") == [message]
    assert bus.receive("scout") == []
