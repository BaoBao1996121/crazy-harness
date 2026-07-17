from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from crazy_harness.core.events import Event, EventLog

_DELIVERY_SENT = "mailbox.delivery.sent"
_DELIVERY_ACKED = "mailbox.delivery.acked"


@dataclass(frozen=True)
class Delivery:
    delivery_id: str
    event: Event


class DurableMailbox:
    """An at-least-once inbox rebuilt from append-only delivery facts."""

    def __init__(self, mailbox_id: str, event_log: EventLog) -> None:
        self.mailbox_id = mailbox_id
        self.event_log = event_log

    def send(self, event: Event, *, delivery_id: str | None = None) -> Delivery:
        delivery = Delivery(delivery_id or uuid4().hex, event)
        existing = self._delivery(delivery.delivery_id)
        if existing is not None:
            if existing.event != event:
                raise ValueError(f"delivery id already belongs to another event: {delivery.delivery_id}")
            return existing
        self.event_log.append(
            Event(
                run_id=event.run_id,
                task_id=event.task_id,
                type=_DELIVERY_SENT,
                source=f"mailbox:{self.mailbox_id}",
                payload={
                    "mailbox_id": self.mailbox_id,
                    "delivery_id": delivery.delivery_id,
                    "event": event.model_dump(mode="json"),
                },
                causation_id=event.id,
            )
        )
        return delivery

    def peek(self, predicate: Callable[[Event], bool] | None = None) -> Delivery | None:
        for delivery in self._pending():
            if predicate is None or predicate(delivery.event):
                return delivery
        return None

    def ack(self, delivery_id: str) -> None:
        delivery = self._delivery(delivery_id)
        if delivery is None:
            raise KeyError(f"unknown delivery: {delivery_id}")
        if all(item.delivery_id != delivery_id for item in self._pending()):
            return
        self.event_log.append(
            Event(
                run_id=delivery.event.run_id,
                task_id=delivery.event.task_id,
                type=_DELIVERY_ACKED,
                source=f"mailbox:{self.mailbox_id}",
                payload={"mailbox_id": self.mailbox_id, "delivery_id": delivery_id},
                causation_id=delivery.event.id,
            )
        )

    def _pending(self) -> list[Delivery]:
        events = self.event_log.read_all()
        acked = {
            str(event.payload["delivery_id"])
            for event in events
            if event.type == _DELIVERY_ACKED and event.payload.get("mailbox_id") == self.mailbox_id
        }
        pending: list[Delivery] = []
        seen: set[str] = set()
        for event in events:
            if event.type != _DELIVERY_SENT or event.payload.get("mailbox_id") != self.mailbox_id:
                continue
            delivery_id = str(event.payload["delivery_id"])
            if delivery_id in acked or delivery_id in seen:
                continue
            seen.add(delivery_id)
            pending.append(Delivery(delivery_id, Event.model_validate(event.payload["event"])))
        return pending

    def _delivery(self, delivery_id: str) -> Delivery | None:
        for event in self.event_log.read_all():
            if (
                event.type == _DELIVERY_SENT
                and event.payload.get("mailbox_id") == self.mailbox_id
                and str(event.payload.get("delivery_id")) == delivery_id
            ):
                return Delivery(delivery_id, Event.model_validate(event.payload["event"]))
        return None
