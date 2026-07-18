from crazy_harness.core.agents import AgentLoop
from crazy_harness.core.events import Event


def event(event_type: str, correlation_id: str) -> Event:
    return Event(
        run_id="run",
        task_id="assignment:agent-run",
        type=event_type,
        source="spike",
        payload={"correlation_id": correlation_id},
    )


events = [event("agent.waiting", "peer-1")]
assert AgentLoop._has_active_wait(events) is True
assert (
    AgentLoop._has_active_wait([*events, event("a2a.peer.responded", "peer-1")])
    is False
)
print("persisted peer response resumes child loop: ok")
