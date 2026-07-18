import sqlite3
from threading import Event as ThreadEvent
from datetime import datetime, timedelta, timezone

import pytest

from crazy_harness.control_plane.kernel import (
    FaultController,
    InjectedKernelCrash,
)
from crazy_harness.control_plane.runtime import ResidentRuntime, ResidentScheduler
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event
from crazy_harness.core.runtime import DurableMailbox


def _append_run(store: SQLiteEventStore, *, run_id: str, task_id: str) -> Event:
    return store.append(
        Event(
            run_id=run_id,
            task_id=task_id,
            type="run.created",
            source="test",
            payload={"title": "Scheduler resilience", "brief": "Exercise recovery."},
        )
    )


def _append_probe(store: SQLiteEventStore, created: Event, *, event_type: str) -> Event:
    return store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type=event_type,
            source="test",
            payload={"probe": True},
            causation_id=created.id,
        )
    )


def test_resident_thread_recovers_after_transient_mailbox_selection_error(
    tmp_path, monkeypatch
):
    runtime = ResidentRuntime(tmp_path)
    probe_mailbox = DurableMailbox("resilience-probe", runtime.store)
    probe = Event(
        run_id="control-plane",
        task_id="scheduler-resilience",
        type="test.scheduler.probe",
        source="test",
        payload={},
    )
    runtime.store.append(probe)
    probe_mailbox.send(probe, delivery_id="scheduler-resilience-probe")
    handled = ThreadEvent()
    runtime.scheduler.register(
        "resilience-probe", probe_mailbox, lambda _delivery: handled.set()
    )

    coordinator_mailbox = runtime.mailboxes["coordinator"]
    original_peek = coordinator_mailbox.peek
    peek_calls = 0

    def flaky_peek(predicate=None):
        nonlocal peek_calls
        peek_calls += 1
        if peek_calls == 1:
            raise RuntimeError("transient mailbox read failure")
        return original_peek(predicate)

    monkeypatch.setattr(coordinator_mailbox, "peek", flaky_peek)

    runtime.start()
    try:
        assert handled.wait(timeout=3), (
            "resident thread did not recover and dispatch work"
        )
        assert runtime._thread is not None and runtime._thread.is_alive()
    finally:
        runtime.stop()

    failures = [
        event
        for event in runtime.store.read_all()
        if event.type == "runtime.scheduler.cycle.failed"
    ]
    assert len(failures) == 1
    assert failures[0].payload == {
        "stage": "delivery_selection",
        "reason": "RuntimeError: transient mailbox read failure",
        "recovering": True,
    }
    assert peek_calls >= 2
    assert probe_mailbox.peek() is None


def test_resident_thread_buffers_a_cycle_failure_while_event_store_is_unavailable(
    tmp_path, monkeypatch
):
    runtime = ResidentRuntime(tmp_path)
    probe_mailbox = DurableMailbox("store-recovery-probe", runtime.store)
    probe = Event(
        run_id="control-plane",
        task_id="store-recovery",
        type="test.scheduler.store-recovery",
        source="test",
        payload={},
    )
    runtime.store.append(probe)
    probe_mailbox.send(probe, delivery_id="store-recovery-probe")
    handled = ThreadEvent()
    runtime.scheduler.register(
        "store-recovery-probe", probe_mailbox, lambda _delivery: handled.set()
    )

    original_reconcile = runtime._reconcile_routes
    reconcile_calls = 0

    def flaky_reconcile():
        nonlocal reconcile_calls
        reconcile_calls += 1
        if reconcile_calls == 1:
            raise sqlite3.OperationalError("event store read unavailable")
        return original_reconcile()

    original_append = runtime.store.append
    failure_append_calls = 0

    def flaky_append(event):
        nonlocal failure_append_calls
        if event.type == "runtime.scheduler.cycle.failed":
            failure_append_calls += 1
            if failure_append_calls == 1:
                raise sqlite3.OperationalError("event store write unavailable")
        return original_append(event)

    monkeypatch.setattr(runtime, "_reconcile_routes", flaky_reconcile)
    monkeypatch.setattr(runtime.store, "append", flaky_append)

    runtime.start()
    try:
        assert handled.wait(timeout=3), (
            "resident thread died when failure recording hit the same store outage"
        )
        assert runtime._thread is not None and runtime._thread.is_alive()
    finally:
        runtime.stop()

    failures = [
        event
        for event in runtime.store.read_all()
        if event.type == "runtime.scheduler.cycle.failed"
    ]
    assert len(failures) == 1
    assert failures[0].payload["stage"] == "route_reconciliation"
    assert failures[0].payload["reason"] == (
        "OperationalError: event store read unavailable"
    )
    assert failure_append_calls >= 2


def test_resident_loop_does_not_reclassify_injected_kernel_crash(tmp_path, monkeypatch):
    runtime = ResidentRuntime(tmp_path)

    def injected_crash():
        raise InjectedKernelCrash("intentional fault point")

    monkeypatch.setattr(runtime, "_reconcile_routes", injected_crash)

    with pytest.raises(InjectedKernelCrash, match="intentional fault point"):
        runtime._serve()

    assert not any(
        event.type == "runtime.scheduler.cycle.failed"
        for event in runtime.store.read_all()
    )


def test_dead_letter_fails_running_run_with_durable_causation(tmp_path):
    store = SQLiteEventStore(tmp_path / "control_plane.db")
    created = _append_run(store, run_id="run-poison", task_id="task-poison")
    poison = store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="assignment.created",
            source="control.kernel",
            payload={
                "assignment_id": "assignment-poison",
                "agent_id": "poison-worker",
                "stage_id": "poison-stage",
            },
            causation_id=created.id,
        )
    )
    store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="assignment.lease.acquired",
            source="control.kernel",
            payload={
                "lease_id": "lease:assignment-poison",
                "assignment_id": "assignment-poison",
                "agent_id": "poison-worker",
                "stage_id": "poison-stage",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=1)
                ).isoformat(),
            },
            causation_id=poison.id,
        )
    )
    mailbox = DurableMailbox("poison-worker", store)
    mailbox.send(poison, delivery_id="delivery-poison")
    scheduler = ResidentScheduler(store, FaultController())

    def poison_handler(_delivery):
        raise RuntimeError("permanent scheduler worker failure")

    scheduler.register("poison-worker", mailbox, poison_handler)

    for _ in range(ResidentScheduler.MAX_DELIVERY_FAILURES):
        assert scheduler.run_once() is True

    dead_letters = [
        event
        for event in store.read_all(run_id=created.run_id)
        if event.type == "mailbox.delivery.dead_lettered"
    ]
    run_failures = [
        event
        for event in store.read_all(run_id=created.run_id)
        if event.type == "run.failed"
    ]

    assert len(dead_letters) == 1
    assert len(run_failures) == 1
    assert run_failures[0].causation_id == dead_letters[0].id
    assert run_failures[0].payload == {
        "reason": "delivery_dead_lettered",
        "failure": "RuntimeError: permanent scheduler worker failure",
        "delivery_id": "delivery-poison",
        "dead_letter_event_id": dead_letters[0].id,
    }
    assert store.projection("run", created.run_id)["status"] == "failed"
    assert store.projection("assignment", "assignment-poison")["status"] == "failed"
    assert store.projection("lease", "assignment-poison")["status"] == "released"
    assert mailbox.peek() is None
    assert scheduler.has_pending() is False


def test_dead_letter_does_not_overwrite_an_already_terminal_run(tmp_path):
    store = SQLiteEventStore(tmp_path / "control_plane.db")
    created = _append_run(store, run_id="run-complete", task_id="task-complete")
    store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="run.succeeded",
            source="test",
            payload={"reason": "already complete"},
            causation_id=created.id,
        )
    )
    poison = _append_probe(store, created, event_type="test.late.poison")
    mailbox = DurableMailbox("late-poison-worker", store)
    mailbox.send(poison, delivery_id="delivery-late-poison")
    scheduler = ResidentScheduler(store, FaultController())
    scheduler.register(
        "late-poison-worker",
        mailbox,
        lambda _delivery: (_ for _ in ()).throw(RuntimeError("late poison")),
    )

    for _ in range(ResidentScheduler.MAX_DELIVERY_FAILURES):
        assert scheduler.run_once() is True

    assert store.projection("run", created.run_id)["status"] == "succeeded"
    assert not any(
        event.type == "run.failed" for event in store.read_all(run_id=created.run_id)
    )
    assert mailbox.peek() is None


def test_child_agent_run_dead_letter_writes_terminal_facts_to_the_root_task(tmp_path):
    store = SQLiteEventStore(tmp_path / "control_plane.db")
    created = _append_run(store, run_id="run-child-poison", task_id="root-task")
    assignment = store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="assignment.created",
            source="control.kernel",
            payload={
                "assignment_id": "assignment-child-poison",
                "agent_id": "child-worker",
                "stage_id": "child-stage",
            },
            causation_id=created.id,
        )
    )
    store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="assignment.lease.acquired",
            source="control.kernel",
            payload={
                "lease_id": "lease:assignment-child-poison",
                "assignment_id": "assignment-child-poison",
                "agent_id": "child-worker",
                "stage_id": "child-stage",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=1)
                ).isoformat(),
            },
            causation_id=assignment.id,
        )
    )
    child_turn = store.append(
        Event(
            run_id=created.run_id,
            task_id="assignment-child-poison:agent-run",
            type="runtime.turn.ready",
            source="runtime.scheduler",
            payload={
                "assignment_id": "assignment-child-poison",
                "agent_id": "child-worker",
            },
            causation_id=assignment.id,
        )
    )
    mailbox = DurableMailbox("child-worker", store)
    mailbox.send(child_turn, delivery_id="delivery-child-poison")
    scheduler = ResidentScheduler(store, FaultController())
    scheduler.register(
        "child-worker",
        mailbox,
        lambda _delivery: (_ for _ in ()).throw(RuntimeError("child poison")),
    )

    for _ in range(ResidentScheduler.MAX_DELIVERY_FAILURES):
        assert scheduler.run_once() is True

    root_types = {
        event.type for event in store.read_all(task_id=created.task_id)
    }
    child_types = {
        event.type
        for event in store.read_all(task_id="assignment-child-poison:agent-run")
    }
    assert {
        "assignment.failed",
        "assignment.lease.released",
        "run.failed",
    } <= root_types
    assert not {
        "assignment.failed",
        "assignment.lease.released",
        "run.failed",
    } & child_types
