from datetime import datetime, timedelta, timezone

import pytest

from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.runtime.mailbox import DurableMailbox
from crazy_harness.core.runtime.scheduler import CooperativeScheduler, WaitCondition
from crazy_harness.core.runtime.state import (
    AgentStatus,
    AssignmentState,
    OperationState,
    reduce_agent_status,
    reduce_assignment_state,
    reduce_operation_state,
)


def make_event(event_type: str = "approval.granted", *, correlation_id: str = "release-1") -> Event:
    return Event(
        run_id="run-1",
        task_id="task-1",
        type=event_type,
        source="human",
        payload={"correlation_id": correlation_id},
    )


def test_unacked_delivery_survives_mailbox_reopen(tmp_path):
    path = tmp_path / "events.jsonl"
    mailbox = DurableMailbox(mailbox_id="builder", event_log=EventLog(path))

    sent = mailbox.send(make_event())
    assert mailbox.peek() == sent

    reopened = DurableMailbox(mailbox_id="builder", event_log=EventLog(path))
    assert reopened.peek() == sent

    reopened.ack(sent.delivery_id)
    assert DurableMailbox(mailbox_id="builder", event_log=EventLog(path)).peek() is None


def test_redelivery_id_makes_business_handling_idempotent(tmp_path):
    mailbox = DurableMailbox(mailbox_id="builder", event_log=EventLog(tmp_path / "events.jsonl"))
    mailbox.send(make_event("noise"))
    mailbox.send(make_event("approval.granted"))
    applied: set[str] = set()
    business_effects: list[str] = []

    def apply_once(delivery):
        if delivery.delivery_id not in applied:
            applied.add(delivery.delivery_id)
            business_effects.append(delivery.event.type)

    def is_approval(event):
        return event.type == "approval.granted"
    first = mailbox.peek(is_approval)
    assert first is not None
    apply_once(first)

    redelivered = mailbox.peek(is_approval)
    assert redelivered is not None
    assert redelivered.delivery_id == first.delivery_id
    apply_once(redelivered)

    mailbox.ack(first.delivery_id)
    assert mailbox.peek(is_approval) is None
    assert business_effects == ["approval.granted"]


def test_delivery_id_is_idempotent_and_cannot_alias_another_event(tmp_path):
    mailbox = DurableMailbox(mailbox_id="builder", event_log=EventLog(tmp_path / "events.jsonl"))
    event = make_event()

    first = mailbox.send(event, delivery_id="delivery-1")
    assert mailbox.send(event, delivery_id="delivery-1") == first
    mailbox.ack("delivery-1")
    mailbox.ack("delivery-1")

    with pytest.raises(ValueError, match="delivery id already belongs"):
        mailbox.send(make_event("approval.denied"), delivery_id="delivery-1")


def test_runtime_states_have_independent_reducers():
    events = [
        Event(
            run_id="run-1",
            task_id="task-1",
            type="runtime.agent.waiting",
            source="scheduler",
            payload={"agent_id": "builder"},
        ),
        Event(
            run_id="run-1",
            task_id="task-1",
            type="assignment.reviewing",
            source="reviewer",
            payload={"assignment_id": "assignment-1"},
        ),
        Event(
            run_id="run-1",
            task_id="task-1",
            type="operation.unknown",
            source="tool-runtime",
            payload={"operation_id": "operation-1"},
        ),
    ]
    agent = AgentStatus.BUSY
    assignment = AssignmentState.RUNNING
    operation = OperationState.STARTED

    for event in events:
        agent = reduce_agent_status(agent, event, agent_id="builder")
        assignment = reduce_assignment_state(assignment, event, assignment_id="assignment-1")
        operation = reduce_operation_state(operation, event, operation_id="operation-1")

    assert agent is AgentStatus.WAITING
    assert assignment is AssignmentState.REVIEWING
    assert operation is OperationState.UNKNOWN


def test_existing_event_is_seen_before_wait_registration(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    mailbox = DurableMailbox(mailbox_id="builder", event_log=event_log)
    mailbox.send(make_event())
    calls: list[str | None] = []

    def step(delivery):
        calls.append(delivery.event.type if delivery else None)
        if delivery is None:
            return WaitCondition(
                event_type="approval.granted",
                correlation_id="release-1",
                source="human",
                deadline=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
        return None

    scheduler = CooperativeScheduler(event_log)
    scheduler.register("builder", mailbox, step)
    scheduler.schedule("builder")

    assert scheduler.run_once() is True
    assert calls == [None]
    assert scheduler.run_once() is True
    assert calls == [None, "approval.granted"]
    assert mailbox.peek() is None


def test_wait_condition_requires_timezone_aware_deadline():
    with pytest.raises(ValueError, match="timezone-aware"):
        WaitCondition("approval.granted", deadline=datetime(2030, 1, 1))


def test_future_event_wakes_waiter_without_model_polling_and_releases_slot(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(make_event("task.started"))
    builder_mailbox = DurableMailbox(mailbox_id="builder", event_log=event_log)
    reviewer_mailbox = DurableMailbox(mailbox_id="reviewer", event_log=event_log)
    builder_calls: list[str | None] = []
    run_order: list[str] = []

    def builder_step(delivery):
        run_order.append("builder")
        builder_calls.append(delivery.event.type if delivery else None)
        if delivery is None:
            return WaitCondition(
                event_type="approval.granted",
                correlation_id="release-1",
                source="human",
                deadline=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
        return None

    def reviewer_step(_delivery):
        run_order.append("reviewer")
        return None

    scheduler = CooperativeScheduler(event_log)
    scheduler.register("builder", builder_mailbox, builder_step)
    scheduler.register("reviewer", reviewer_mailbox, reviewer_step)
    scheduler.schedule("builder")
    scheduler.schedule("reviewer")

    assert scheduler.run_once() is True
    with pytest.raises(RuntimeError, match="agent is waiting"):
        scheduler.schedule("builder")
    assert scheduler.run_once() is True
    assert run_order == ["builder", "reviewer"]
    assert scheduler.run_once() is False
    assert builder_calls == [None]

    reopened_log = EventLog(tmp_path / "events.jsonl")
    reopened_mailbox = DurableMailbox(mailbox_id="builder", event_log=reopened_log)
    reopened = CooperativeScheduler(reopened_log)
    reopened.register("builder", reopened_mailbox, builder_step)
    assert reopened.run_once() is False

    reopened_mailbox.send(make_event())
    assert reopened.run_once() is True
    assert builder_calls == [None, "approval.granted"]


def test_deadline_emits_timeout_event_and_wakes_waiter(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(make_event("task.started"))
    mailbox = DurableMailbox(mailbox_id="builder", event_log=event_log)
    now = [datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc)]
    deadline = now[0] + timedelta(minutes=5)
    calls: list[str | None] = []

    def step(delivery):
        calls.append(delivery.event.type if delivery else None)
        if delivery is None:
            return WaitCondition("approval.granted", "release-1", "human", deadline)
        return None

    scheduler = CooperativeScheduler(event_log, clock=lambda: now[0])
    scheduler.register("builder", mailbox, step)
    scheduler.schedule("builder")
    assert scheduler.run_once() is True

    now[0] = deadline
    assert scheduler.run_once() is True
    assert calls == [None, "runtime.wait.timed_out"]
    assert any(event.type == "runtime.wait.timed_out" for event in event_log.read_all())


def test_scheduler_ignores_foreign_step_events_without_ready_id(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(make_event("task.started"))
    event_log.append(
        Event(
            run_id="run-1",
            task_id="task-1",
            type="runtime.agent.step.completed",
            source="resident.scheduler",
            payload={"agent_id": "other", "delivery_id": "foreign"},
        )
    )
    mailbox = DurableMailbox("builder", event_log)
    mailbox.send(make_event("work.requested"))
    calls: list[str] = []
    scheduler = CooperativeScheduler(event_log)
    scheduler.register(
        "builder",
        mailbox,
        lambda delivery: calls.append(delivery.event.type) or None,
    )

    assert scheduler.wake("builder") is True
    assert scheduler.run_once() is True
    assert calls == ["work.requested"]
