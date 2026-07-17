from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.runtime.mailbox import Delivery, DurableMailbox


@dataclass(frozen=True)
class WaitCondition:
    event_type: str
    correlation_id: str | None = None
    source: str | None = None
    deadline: datetime | None = None

    def __post_init__(self) -> None:
        if self.deadline is not None and self.deadline.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")

    def matches(self, event: Event) -> bool:
        return (
            event.type == self.event_type
            and (self.correlation_id is None or event.payload.get("correlation_id") == self.correlation_id)
            and (self.source is None or event.source == self.source)
        )


Step = Callable[[Delivery | None], WaitCondition | None]


@dataclass(frozen=True)
class _Agent:
    mailbox: DurableMailbox
    step: Step


class CooperativeScheduler:
    """One-step-at-a-time scheduler whose queue and waits are durable facts."""

    def __init__(self, event_log: EventLog, *, clock: Callable[[], datetime] | None = None) -> None:
        self.event_log = event_log
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._agents: dict[str, _Agent] = {}

    def register(self, agent_id: str, mailbox: DurableMailbox, step: Step) -> None:
        if agent_id in self._agents:
            raise ValueError(f"agent already registered: {agent_id}")
        self._agents[agent_id] = _Agent(mailbox, step)

    def schedule(self, agent_id: str) -> None:
        self._agent(agent_id)
        if any(event.payload.get("agent_id") == agent_id for event, _ in self._active_waits()):
            raise RuntimeError(f"agent is waiting: {agent_id}")
        self._queue(agent_id)

    def wake(self, agent_id: str) -> bool:
        """Queue one pending mailbox delivery for an idle resident AgentInstance."""

        agent = self._agent(agent_id)
        if any(event.payload.get("agent_id") == agent_id for event, _ in self._active_waits()):
            self._promote_waiters()
            return True
        delivery = agent.mailbox.peek()
        if delivery is None:
            return False
        self._queue(agent_id, delivery)
        return True

    def run_once(self) -> bool:
        self._promote_waiters()
        ready = next((event for event in self._pending_ready() if event.payload["agent_id"] in self._agents), None)
        if ready is None:
            return False

        agent_id = str(ready.payload["agent_id"])
        agent = self._agent(agent_id)
        delivery = self._ready_delivery(agent, ready)
        self._append("runtime.agent.busy", {"agent_id": agent_id, "ready_id": ready.payload["ready_id"]})

        next_wait = agent.step(delivery)
        if delivery is not None:
            agent.mailbox.ack(delivery.delivery_id)
        self._append(
            "runtime.agent.step.completed",
            {"agent_id": agent_id, "ready_id": ready.payload["ready_id"]},
        )

        if next_wait is None:
            self._append("runtime.agent.idle", {"agent_id": agent_id})
        else:
            self._begin_wait(agent_id, agent.mailbox, next_wait)
        return True

    def _begin_wait(self, agent_id: str, mailbox: DurableMailbox, condition: WaitCondition) -> None:
        wait_id = uuid4().hex
        existing = mailbox.peek(condition.matches)
        if existing is not None:
            self._append(
                "runtime.wait.satisfied",
                {
                    "agent_id": agent_id,
                    "wait_id": wait_id,
                    "delivery_id": existing.delivery_id,
                    "registered": False,
                },
            )
            self._queue(agent_id, existing)
            self._append("runtime.agent.idle", {"agent_id": agent_id})
            return

        self._append(
            "runtime.wait.registered",
            {"agent_id": agent_id, "wait_id": wait_id, "condition": self._dump_condition(condition)},
        )
        self._append("runtime.agent.waiting", {"agent_id": agent_id, "wait_id": wait_id})

    def _promote_waiters(self) -> None:
        for wait_event, condition in self._active_waits():
            agent_id = str(wait_event.payload["agent_id"])
            agent = self._agents.get(agent_id)
            if agent is None:
                continue
            delivery = agent.mailbox.peek(condition.matches)
            if delivery is None:
                if condition.deadline is None or self.clock() < condition.deadline:
                    continue
                timeout = self._append(
                    "runtime.wait.timed_out",
                    {
                        "agent_id": agent_id,
                        "wait_id": wait_event.payload["wait_id"],
                        "event_type": condition.event_type,
                        "correlation_id": condition.correlation_id,
                        "deadline": condition.deadline.isoformat(),
                    },
                )
                delivery = agent.mailbox.send(timeout)
            else:
                self._append(
                    "runtime.wait.satisfied",
                    {
                        "agent_id": agent_id,
                        "wait_id": wait_event.payload["wait_id"],
                        "delivery_id": delivery.delivery_id,
                        "registered": True,
                    },
                )
            self._queue(agent_id, delivery)
            self._append("runtime.agent.idle", {"agent_id": agent_id})

    def _active_waits(self) -> list[tuple[Event, WaitCondition]]:
        events = self.event_log.read_all()
        resolved = {
            str(event.payload["wait_id"])
            for event in events
            if event.type in {"runtime.wait.satisfied", "runtime.wait.timed_out"}
        }
        return [
            (event, self._load_condition(event.payload["condition"]))
            for event in events
            if event.type == "runtime.wait.registered" and str(event.payload["wait_id"]) not in resolved
        ]

    def _queue(self, agent_id: str, delivery: Delivery | None = None) -> None:
        payload: dict[str, str] = {"agent_id": agent_id, "ready_id": uuid4().hex}
        if delivery is not None:
            payload.update({"delivery_id": delivery.delivery_id, "event_id": delivery.event.id})
        self._append("runtime.agent.ready", payload)

    def _pending_ready(self) -> list[Event]:
        events = self.event_log.read_all()
        completed = {
            str(event.payload["ready_id"])
            for event in events
            if event.type == "runtime.agent.step.completed"
            and "ready_id" in event.payload
        }
        return [
            event
            for event in events
            if event.type == "runtime.agent.ready"
            and "ready_id" in event.payload
            and "agent_id" in event.payload
            and str(event.payload["ready_id"]) not in completed
        ]

    @staticmethod
    def _dump_condition(condition: WaitCondition) -> dict[str, str | None]:
        return {
            "event_type": condition.event_type,
            "correlation_id": condition.correlation_id,
            "source": condition.source,
            "deadline": condition.deadline.isoformat() if condition.deadline else None,
        }

    @staticmethod
    def _load_condition(payload: dict) -> WaitCondition:
        deadline = payload.get("deadline")
        return WaitCondition(
            event_type=str(payload["event_type"]),
            correlation_id=payload.get("correlation_id"),
            source=payload.get("source"),
            deadline=datetime.fromisoformat(deadline) if deadline else None,
        )

    @staticmethod
    def _ready_delivery(agent: _Agent, ready: Event) -> Delivery | None:
        event_id = ready.payload.get("event_id")
        if event_id is None:
            return None
        delivery = agent.mailbox.peek(lambda event: event.id == event_id)
        if delivery is None or delivery.delivery_id != ready.payload.get("delivery_id"):
            raise RuntimeError(f"ready delivery is no longer pending: {ready.payload.get('delivery_id')}")
        return delivery

    def _agent(self, agent_id: str) -> _Agent:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"agent is not registered: {agent_id}") from exc

    def _append(self, event_type: str, payload: dict) -> Event:
        identity = self.event_log.last()
        if identity is None:
            raise RuntimeError("scheduler requires a seed event before work is scheduled")
        return self.event_log.append(
            Event(
                run_id=identity.run_id,
                task_id=identity.task_id,
                type=event_type,
                source="runtime.scheduler",
                payload=payload,
            )
        )
