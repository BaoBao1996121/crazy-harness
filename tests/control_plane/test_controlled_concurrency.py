from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event as ThreadEvent, Lock
from time import monotonic, sleep

import pytest

from crazy_harness.control_plane.kernel import (
    CommandCandidate,
    CommandKind,
    ControlKernel,
    FaultController,
)
from crazy_harness.control_plane.runtime import (
    ResidentRuntime,
    ResidentScheduler,
    TaskRequest,
)
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event
from crazy_harness.core.dispatch import current_dispatch_context
from crazy_harness.core.runtime import DurableMailbox
from crazy_harness.taskpacks import ResidentDemoTeamTaskPack


def _send(store, mailbox, *, delivery_id: str, worker_id: str) -> None:
    trigger = store.append(
        Event(
            run_id=f"run-{delivery_id}",
            task_id=f"task-{delivery_id}",
            type="assignment.created",
            source="test",
            payload={"assignment_id": delivery_id, "agent_id": worker_id},
        )
    )
    mailbox.send(trigger, delivery_id=delivery_id)


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


@pytest.mark.smoke
def test_scheduler_runs_distinct_workers_at_the_same_time(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    first_box = DurableMailbox("first", store)
    second_box = DurableMailbox("second", store)
    _send(store, first_box, delivery_id="first-delivery", worker_id="first")
    _send(store, second_box, delivery_id="second-delivery", worker_id="second")
    scheduler = ResidentScheduler(store, FaultController(), max_workers=2)
    release = ThreadEvent()
    started = {"first": ThreadEvent(), "second": ThreadEvent()}

    def handler(worker_id: str):
        def run(_delivery):
            started[worker_id].set()
            assert release.wait(2)

        return run

    scheduler.register("first", first_box, handler("first"), max_concurrency=1)
    scheduler.register("second", second_box, handler("second"), max_concurrency=1)
    try:
        assert scheduler.dispatch_available() == 2
        assert started["first"].wait(1)
        assert started["second"].wait(1)
        assert scheduler.in_flight_count == 2
        assert {
            event.payload["agent_id"]
            for event in store.read_all()
            if event.type == "runtime.delivery.dispatched"
        } == {"first", "second"}
    finally:
        release.set()
        assert scheduler.wait_until_idle(timeout=2)
        scheduler.shutdown()


def test_two_schedulers_cannot_claim_the_same_durable_delivery(tmp_path):
    path = tmp_path / "events.db"
    first_store = SQLiteEventStore(path)
    first_box = DurableMailbox("worker", first_store)
    _send(first_store, first_box, delivery_id="shared-delivery", worker_id="worker")
    second_store = SQLiteEventStore(path)
    second_box = DurableMailbox("worker", second_store)
    first = ResidentScheduler(first_store, FaultController(), max_workers=1)
    second = ResidentScheduler(second_store, FaultController(), max_workers=1)
    release = ThreadEvent()
    started = ThreadEvent()
    duplicate_started = ThreadEvent()

    def owner(_delivery):
        started.set()
        assert release.wait(2)

    first.register("worker", first_box, owner)
    second.register("worker", second_box, lambda _: duplicate_started.set())
    try:
        assert first.dispatch_available() == 1
        assert started.wait(1)
        assert second.dispatch_available() == 0
        assert duplicate_started.wait(0.1) is False
    finally:
        release.set()
        assert first.wait_until_idle(timeout=2)
        first.shutdown()
        second.shutdown()


def test_remote_inflight_delivery_is_not_reported_as_locally_queued(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    owner_mailbox = DurableMailbox("worker", store)
    observer_mailbox = DurableMailbox("worker", store)
    _send(store, owner_mailbox, delivery_id="remote-inflight", worker_id="worker")
    owner = ResidentScheduler(store, FaultController(), max_workers=1)
    observer = ResidentScheduler(store, FaultController(), max_workers=1)
    started = ThreadEvent()
    release = ThreadEvent()

    def blocked(_delivery):
        started.set()
        assert release.wait(2)

    owner.register("worker", owner_mailbox, blocked)
    observer.register("worker", observer_mailbox, lambda _delivery: None)
    try:
        assert owner.dispatch_available() == 1
        assert started.wait(1)
        snapshot = observer.snapshot()
        assert snapshot["active"] == 0
        assert snapshot["queued"] == 0
        assert snapshot["workers"][0]["queued"] == 0
        assert observer.pending_count == 0
    finally:
        release.set()
        assert owner.wait_until_idle(timeout=2)
        owner.shutdown()
        observer.shutdown()


def test_worker_capacity_is_enforced_across_schedulers(tmp_path):
    path = tmp_path / "events.db"
    first_store = SQLiteEventStore(path)
    mailbox = DurableMailbox("worker", first_store)
    _send(first_store, mailbox, delivery_id="capacity-one", worker_id="worker")
    _send(first_store, mailbox, delivery_id="capacity-two", worker_id="worker")
    second_store = SQLiteEventStore(path)
    first = ResidentScheduler(first_store, FaultController(), max_workers=1)
    second = ResidentScheduler(second_store, FaultController(), max_workers=1)
    first_release = ThreadEvent()
    second_release = ThreadEvent()
    first_started = ThreadEvent()
    second_started = ThreadEvent()

    def first_handler(_delivery):
        first_started.set()
        assert first_release.wait(3)

    def second_handler(_delivery):
        second_started.set()
        assert second_release.wait(3)

    first.register("worker", mailbox, first_handler, max_concurrency=1)
    second.register(
        "worker",
        DurableMailbox("worker", second_store),
        second_handler,
        max_concurrency=1,
    )
    try:
        assert first.dispatch_available() == 1
        assert first_started.wait(1)
        assert second.dispatch_available() == 0
        assert second_started.wait(0.1) is False

        first_release.set()
        _wait_until(lambda: first.in_flight_count == 0)
        assert second.dispatch_available() == 1
        assert second_started.wait(1)
    finally:
        first_release.set()
        second_release.set()
        _wait_until(lambda: first.in_flight_count == 0)
        _wait_until(lambda: second.in_flight_count == 0)
        first.shutdown()
        second.shutdown()


def test_distinct_deliveries_for_one_agent_run_are_single_flight_across_schedulers(
    tmp_path,
):
    path = tmp_path / "events.db"
    first_store = SQLiteEventStore(path)
    first_box = DurableMailbox("first", first_store)
    second_box = DurableMailbox("second", first_store)
    for mailbox, delivery_id, worker_id in (
        (first_box, "delivery-one", "first"),
        (second_box, "delivery-two", "second"),
    ):
        trigger = first_store.append(
            Event(
                run_id="run-shared",
                task_id="shared-agent-run",
                type="runtime.turn.ready",
                source="test",
                payload={
                    "agent_id": worker_id,
                    "agent_run_id": "shared-agent-run",
                    "assignment_id": "shared-assignment",
                },
            )
        )
        mailbox.send(trigger, delivery_id=delivery_id)
    second_store = SQLiteEventStore(path)
    first = ResidentScheduler(first_store, FaultController(), max_workers=1)
    second = ResidentScheduler(second_store, FaultController(), max_workers=1)
    release = ThreadEvent()
    started = ThreadEvent()
    duplicate_started = ThreadEvent()
    first.register(
        "first",
        first_box,
        lambda _: (started.set(), release.wait(2)),
    )
    second.register(
        "second",
        DurableMailbox("second", second_store),
        lambda _: duplicate_started.set(),
    )
    try:
        assert first.dispatch_available() == 1
        assert started.wait(1)
        assert second.dispatch_available() == 0
        assert duplicate_started.wait(0.1) is False
    finally:
        release.set()
        assert first.wait_until_idle(timeout=2)
        first.shutdown()
        second.shutdown()


def test_coordinator_is_single_flight_per_run_across_schedulers(tmp_path):
    path = tmp_path / "events.db"
    first_store = SQLiteEventStore(path)
    first_box = DurableMailbox("coordinator-first", first_store)
    second_box = DurableMailbox("coordinator-second", first_store)
    for mailbox, delivery_id, assignment_id in (
        (first_box, "result-one", "assignment-one"),
        (second_box, "result-two", "assignment-two"),
    ):
        trigger = first_store.append(
            Event(
                run_id="run-shared",
                task_id="task-shared",
                type="agent.result.submitted",
                source="test",
                payload={"assignment_id": assignment_id},
            )
        )
        mailbox.send(trigger, delivery_id=delivery_id)
    second_store = SQLiteEventStore(path)
    first = ResidentScheduler(first_store, FaultController(), max_workers=1)
    second = ResidentScheduler(second_store, FaultController(), max_workers=1)
    release = ThreadEvent()
    started = ThreadEvent()
    duplicate_started = ThreadEvent()
    first.register(
        "coordinator",
        first_box,
        lambda _: (started.set(), release.wait(2)),
    )
    second.register(
        "coordinator",
        DurableMailbox("coordinator-second", second_store),
        lambda _: duplicate_started.set(),
    )
    try:
        assert first.dispatch_available() == 1
        assert started.wait(1)
        assert second.dispatch_available() == 0
        assert duplicate_started.wait(0.1) is False
    finally:
        release.set()
        assert first.wait_until_idle(timeout=2)
        first.shutdown()
        second.shutdown()


def test_scheduler_never_reenters_a_worker_past_its_capacity(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    mailbox = DurableMailbox("worker", store)
    _send(store, mailbox, delivery_id="delivery-1", worker_id="worker")
    _send(store, mailbox, delivery_id="delivery-2", worker_id="worker")
    scheduler = ResidentScheduler(store, FaultController(), max_workers=2)
    release = ThreadEvent()
    second_started = ThreadEvent()
    lock = Lock()
    active = 0
    peak = 0

    def handler(delivery):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        if delivery.delivery_id == "delivery-2":
            second_started.set()
        assert release.wait(2)
        with lock:
            active -= 1

    scheduler.register("worker", mailbox, handler, max_concurrency=1)
    try:
        assert scheduler.dispatch_available() == 1
        _wait_until(lambda: scheduler.in_flight_count == 1)
        assert scheduler.pending_count == 1
        assert second_started.wait(0.1) is False
        release.set()
        _wait_until(lambda: scheduler.in_flight_count == 0)
        release.clear()
        assert scheduler.dispatch_available() == 1
        assert second_started.wait(1)
        assert peak == 1
    finally:
        release.set()
        assert scheduler.wait_until_idle(timeout=2)
        scheduler.shutdown()


def test_pool_backpressure_keeps_excess_delivery_durable(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    first_box = DurableMailbox("first", store)
    second_box = DurableMailbox("second", store)
    _send(store, first_box, delivery_id="delivery-a", worker_id="first")
    _send(store, second_box, delivery_id="delivery-b", worker_id="second")
    scheduler = ResidentScheduler(store, FaultController(), max_workers=1)
    release = ThreadEvent()
    handled: list[str] = []

    def blocked(delivery):
        handled.append(delivery.delivery_id)
        assert release.wait(2)

    scheduler.register("first", first_box, blocked)
    scheduler.register("second", second_box, blocked)
    try:
        assert scheduler.dispatch_available() == 1
        _wait_until(lambda: scheduler.in_flight_count == 1)
        assert scheduler.pending_count == 1
        backpressure = [
            event
            for event in store.read_all()
            if event.type == "runtime.scheduler.backpressure"
        ]
        assert len(backpressure) == 1
        assert backpressure[0].payload == {
            "active": 1,
            "capacity": 1,
            "queued": 1,
        }
        assert second_box.peek() is not None
        release.set()
        _wait_until(lambda: scheduler.in_flight_count == 0)
        release.clear()
        assert scheduler.dispatch_available() == 1
    finally:
        release.set()
        assert scheduler.wait_until_idle(timeout=2)
        scheduler.shutdown()
    assert sorted(handled) == ["delivery-a", "delivery-b"]


def test_scheduler_renews_active_claim_before_ttl_expires(tmp_path):
    path = tmp_path / "events.db"
    store = SQLiteEventStore(path)
    mailbox = DurableMailbox("worker", store)
    _send(store, mailbox, delivery_id="renewed-delivery", worker_id="worker")
    scheduler = ResidentScheduler(
        store,
        FaultController(),
        max_workers=1,
        work_claim_seconds=1,
    )
    release = ThreadEvent()
    started = ThreadEvent()

    def blocked(_delivery):
        started.set()
        assert release.wait(3)

    scheduler.register("worker", mailbox, blocked)
    try:
        assert scheduler.dispatch_available() == 1
        assert started.wait(1)
        sleep(1.1)
        competitor = SQLiteEventStore(path)
        assert (
            competitor.claim_work(
                claim_keys=(
                    "delivery:worker:renewed-delivery",
                    "agent-run:assignment:run-renewed-delivery:renewed-delivery",
                ),
                owner_id="competing-scheduler",
                ttl_seconds=10,
            )
            is None
        )
    finally:
        release.set()
        assert scheduler.wait_until_idle(timeout=3)
        scheduler.shutdown()


def test_claim_renewer_survives_a_transient_store_error(tmp_path, monkeypatch):
    path = tmp_path / "events.db"
    store = SQLiteEventStore(path)
    mailbox = DurableMailbox("worker", store)
    _send(store, mailbox, delivery_id="renew-after-error", worker_id="worker")
    scheduler = ResidentScheduler(
        store,
        FaultController(),
        max_workers=1,
        work_claim_seconds=1,
    )
    release = ThreadEvent()
    started = ThreadEvent()
    renew_calls = 0
    original_renew = store.renew_work_claims

    def transient_renewal_failure(**kwargs):
        nonlocal renew_calls
        renew_calls += 1
        if renew_calls == 1:
            raise RuntimeError("transient claim store outage")
        return original_renew(**kwargs)

    monkeypatch.setattr(store, "renew_work_claims", transient_renewal_failure)

    def blocked(_delivery):
        started.set()
        assert release.wait(3)

    scheduler.register("worker", mailbox, blocked)
    try:
        assert scheduler.dispatch_available() == 1
        assert started.wait(1)
        sleep(1.1)
        assert renew_calls >= 2
        competitor = SQLiteEventStore(path)
        assert (
            competitor.claim_work(
                claim_keys=(
                    "delivery:worker:renew-after-error",
                    "agent-run:assignment:run-renew-after-error:renew-after-error",
                ),
                owner_id="competing-scheduler",
                ttl_seconds=10,
            )
            is None
        )
    finally:
        release.set()
        assert scheduler.wait_until_idle(timeout=3)
        scheduler.shutdown()


def test_unconfirmed_claim_renewal_releases_without_acking_delivery(
    tmp_path, monkeypatch
):
    store = SQLiteEventStore(tmp_path / "events.db")
    mailbox = DurableMailbox("worker", store)
    _send(store, mailbox, delivery_id="renewal-unknown", worker_id="worker")
    scheduler = ResidentScheduler(
        store,
        FaultController(),
        max_workers=1,
        work_claim_seconds=1,
    )
    started = ThreadEvent()

    def unavailable_renewal_store(**_kwargs):
        raise RuntimeError("claim store unavailable")

    monkeypatch.setattr(store, "renew_work_claims", unavailable_renewal_store)

    def cooperative_handler(_delivery):
        context = current_dispatch_context()
        assert context is not None
        started.set()
        assert context.cancellation.wait(2)
        context.cancellation.raise_if_cancelled()

    scheduler.register("worker", mailbox, cooperative_handler)
    try:
        assert scheduler.dispatch_available() == 1
        assert started.wait(1)
        _wait_until(lambda: scheduler.in_flight_count == 0, timeout=2)
        assert mailbox.peek().delivery_id == "renewal-unknown"
        assert not any(
            event.type == "mailbox.delivery.acked" for event in store.read_all()
        )
        failures = [
            event
            for event in store.read_all()
            if event.type == "runtime.delivery.claim.renewal.failed"
        ]
        assert failures
        assert failures[-1].payload["will_retry"] is False
    finally:
        scheduler.shutdown()


def test_stale_worker_cannot_ack_after_claim_is_taken_over(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    mailbox = DurableMailbox("worker", store)
    _send(store, mailbox, delivery_id="stale-delivery", worker_id="worker")
    scheduler = ResidentScheduler(
        store,
        FaultController(),
        max_workers=1,
        work_claim_seconds=1,
    )
    release = ThreadEvent()
    started = ThreadEvent()

    def blocked(_delivery):
        started.set()
        assert release.wait(2)

    scheduler.register("worker", mailbox, blocked)
    try:
        assert scheduler.dispatch_available() == 1
        assert started.wait(1)
        claims = store.claim_work(
            claim_keys=(
                "delivery:worker:stale-delivery",
                "agent-run:assignment:run-stale-delivery:stale-delivery",
            ),
            owner_id="takeover-owner",
            ttl_seconds=10,
            now=datetime.now(timezone.utc) + timedelta(seconds=2),
        )
        assert claims is not None
        release.set()
        _wait_until(lambda: scheduler.in_flight_count == 0)
        assert mailbox.peek().delivery_id == "stale-delivery"
        assert not any(
            event.type == "mailbox.delivery.acked"
            and event.payload.get("delivery_id") == "stale-delivery"
            for event in store.read_all()
        )
        assert any(
            event.type == "runtime.delivery.claim.lost"
            and event.payload.get("delivery_id") == "stale-delivery"
            for event in store.read_all()
        )
    finally:
        release.set()
        scheduler.shutdown()


def test_stale_worker_cannot_commit_a_formal_command_after_takeover(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    mailbox = DurableMailbox("coordinator", store)
    _send(
        store,
        mailbox,
        delivery_id="stale-command",
        worker_id="coordinator",
    )
    kernel = ControlKernel(store)
    scheduler = ResidentScheduler(
        store,
        FaultController(),
        max_workers=1,
        work_claim_seconds=1,
    )
    release = ThreadEvent()
    started = ThreadEvent()

    def blocked(delivery):
        started.set()
        assert release.wait(2)
        kernel.submit(
            CommandCandidate(
                idempotency_key="stale-formal-command",
                run_id=delivery.event.run_id,
                task_id=delivery.event.task_id,
                actor_id="coordinator",
                kind=CommandKind.PLAN_PATCH,
                payload={},
            )
        )

    scheduler.register("coordinator", mailbox, blocked)
    try:
        assert scheduler.dispatch_available() == 1
        assert started.wait(1)
        assert (
            store.claim_work(
                claim_keys=(
                    "delivery:coordinator:stale-command",
                    "agent-run:supervisor:run-stale-command",
                ),
                owner_id="takeover-owner",
                ttl_seconds=10,
                now=datetime.now(timezone.utc) + timedelta(seconds=2),
            )
            is not None
        )
        release.set()
        _wait_until(lambda: scheduler.in_flight_count == 0)
        event_types = [event.type for event in store.read_all()]
        assert "candidate.accepted" not in event_types
        assert "candidate.rejected" not in event_types
        assert "mailbox.delivery.acked" not in event_types
        assert "runtime.agent.crashed" not in event_types
        assert "runtime.delivery.claim.lost" in event_types
    finally:
        release.set()
        scheduler.shutdown()


def test_stale_worker_exception_cannot_record_failure_after_takeover(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    mailbox = DurableMailbox("worker", store)
    _send(store, mailbox, delivery_id="stale-failure", worker_id="worker")
    scheduler = ResidentScheduler(
        store,
        FaultController(),
        max_workers=1,
        work_claim_seconds=1,
    )
    release = ThreadEvent()
    started = ThreadEvent()

    def stale_failure(_delivery):
        started.set()
        assert release.wait(2)
        raise RuntimeError("failure from stale owner")

    scheduler.register("worker", mailbox, stale_failure)
    try:
        assert scheduler.dispatch_available() == 1
        assert started.wait(1)
        assert (
            store.claim_work(
                claim_keys=(
                    "delivery:worker:stale-failure",
                    "agent-run:assignment:run-stale-failure:stale-failure",
                ),
                owner_id="takeover-owner",
                ttl_seconds=10,
                now=datetime.now(timezone.utc) + timedelta(seconds=2),
            )
            is not None
        )
        release.set()
        _wait_until(lambda: scheduler.in_flight_count == 0)
        assert not any(
            event.type in {"runtime.agent.crashed", "mailbox.delivery.dead_lettered"}
            and event.payload.get("delivery_id") == "stale-failure"
            for event in store.read_all()
        )
        assert any(
            event.type == "runtime.delivery.claim.lost"
            and event.payload.get("delivery_id") == "stale-failure"
            for event in store.read_all()
        )
    finally:
        release.set()
        scheduler.shutdown()


def test_durable_run_cancellation_invalidates_a_remote_dispatch_owner(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    created = store.append(
        Event(
            run_id="run-remote-cancel",
            task_id="task-remote-cancel",
            type="run.created",
            source="test",
            payload={"title": "Remote cancellation"},
        )
    )
    assignment = store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="assignment.created",
            source="test",
            payload={"assignment_id": "remote-assignment", "agent_id": "worker"},
        )
    )
    mailbox = DurableMailbox("worker", store)
    mailbox.send(assignment, delivery_id="remote-cancel-delivery")
    scheduler = ResidentScheduler(store, FaultController(), max_workers=1)
    release = ThreadEvent()
    started = ThreadEvent()

    def late_success(delivery):
        started.set()
        assert release.wait(2)
        store.append(
            Event(
                run_id=delivery.event.run_id,
                task_id=delivery.event.task_id,
                type="run.succeeded",
                source="stale.remote-runtime",
            )
        )

    scheduler.register("worker", mailbox, late_success)
    try:
        assert scheduler.dispatch_available() == 1
        assert started.wait(1)
        store.append(
            Event(
                run_id=created.run_id,
                task_id=created.task_id,
                type="run.cancel.requested",
                source="other.runtime",
                payload={"reason": "operator_requested"},
            )
        )
        release.set()
        _wait_until(lambda: scheduler.in_flight_count == 0)
        assert store.projection("run", created.run_id)["status"] == "cancelling"
        assert not any(
            event.type == "run.succeeded"
            for event in store.read_all(run_id=created.run_id)
        )
        assert mailbox.peek() is not None
    finally:
        release.set()
        scheduler.shutdown()


def test_failed_run_persistently_fences_late_dispatch_writes(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    created = store.append(
        Event(
            run_id="run-remote-failure",
            task_id="task-remote-failure",
            type="run.created",
            source="test",
            payload={"title": "Remote failure fence"},
        )
    )
    assignment = store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="assignment.created",
            source="test",
            payload={"assignment_id": "late-assignment", "agent_id": "worker"},
        )
    )
    mailbox = DurableMailbox("worker", store)
    mailbox.send(assignment, delivery_id="late-after-failure")
    scheduler = ResidentScheduler(store, FaultController(), max_workers=1)
    started = ThreadEvent()
    release = ThreadEvent()

    def late_write(delivery):
        started.set()
        assert release.wait(2)
        store.append(
            Event(
                run_id=delivery.event.run_id,
                task_id=delivery.event.task_id,
                type="tool.completed",
                source="stale.remote-runtime",
                payload={"result": "must be fenced"},
            )
        )

    scheduler.register("worker", mailbox, late_write)
    try:
        assert scheduler.dispatch_available() == 1
        assert started.wait(1)
        store.append(
            Event(
                run_id=created.run_id,
                task_id=created.task_id,
                type="run.failed",
                source="other.runtime",
                payload={"reason": "terminal model call"},
            )
        )
        release.set()
        _wait_until(lambda: scheduler.in_flight_count == 0)

        assert not any(
            event.type == "tool.completed"
            for event in store.read_all(run_id=created.run_id)
        )
    finally:
        release.set()
        scheduler.shutdown()


@pytest.mark.smoke
def test_run_cancellation_propagates_to_inflight_handler_and_acks_delivery(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    mailbox = DurableMailbox("worker", store)
    _send(store, mailbox, delivery_id="cancel-me", worker_id="worker")
    scheduler = ResidentScheduler(store, FaultController(), max_workers=1)
    started = ThreadEvent()

    def cooperative_handler(_delivery):
        context = current_dispatch_context()
        assert context is not None
        started.set()
        assert context.cancellation.wait(2)
        context.cancellation.raise_if_cancelled()

    scheduler.register("worker", mailbox, cooperative_handler)
    try:
        assert scheduler.dispatch_available() == 1
        assert started.wait(1)
        assert scheduler.cancel_run("run-cancel-me", reason="operator_requested") == 1
        assert scheduler.wait_until_idle(timeout=2)
        assert mailbox.peek() is None
        event_types = [event.type for event in store.read_all()]
        assert "runtime.delivery.cancellation.requested" in event_types
        assert "runtime.delivery.cancelled" in event_types
        assert "runtime.agent.crashed" not in event_types
    finally:
        scheduler.shutdown()


def test_run_cancellation_reclaims_an_expired_delivery_claim(tmp_path):
    runtime = ResidentRuntime(tmp_path / "resident")
    created = runtime.submit_task(
        TaskRequest(title="Expired claim", brief="Cancellation must drain it.")
    )
    delivery_id = f"ingress:{created.run_id}"
    assert (
        runtime.store.claim_work(
            claim_keys=(
                f"delivery:coordinator:{delivery_id}",
                f"agent-run:supervisor:{created.run_id}",
            ),
            owner_id="dead-runtime",
            ttl_seconds=1,
            now=datetime.now(timezone.utc) - timedelta(seconds=2),
        )
        is not None
    )
    try:
        result = runtime.cancel_run(created.run_id)
        assert result["status"] == "cancelled"
        assert runtime.mailboxes["coordinator"].peek() is None
    finally:
        runtime.scheduler.shutdown()


def test_cancelled_run_rejects_a_late_delivery_after_runtime_restart(tmp_path):
    data_dir = tmp_path / "resident"
    runtime = ResidentRuntime(data_dir)
    created = runtime.submit_task(
        TaskRequest(title="Cancel persistently", brief="Reject work after restart.")
    )
    assert runtime.cancel_run(created.run_id)["status"] == "cancelled"
    late = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="event.external.received",
            source="test.late-producer",
            payload={"receiver": "coordinator", "brief": "late work"},
        )
    )
    runtime.mailboxes["coordinator"].send(late, delivery_id="late-after-cancel")
    runtime.scheduler.shutdown()

    recovered = ResidentRuntime(data_dir)
    try:
        recovered.run_until_idle(max_steps=20)
        events = recovered.store.read_all(run_id=created.run_id)
        assert any(
            event.type == "runtime.delivery.cancelled"
            and event.payload.get("delivery_id") == "late-after-cancel"
            for event in events
        )
        assert not any(
            event.type == "agent.command.proposed" and event.causation_id == late.id
            for event in events
        )
    finally:
        recovered.scheduler.shutdown()


def test_cancellation_recovery_closes_assignments_and_leases(tmp_path):
    data_dir = tmp_path / "resident"
    runtime = ResidentRuntime(data_dir)
    created = runtime.store.append(
        Event(
            run_id="run-recover-cancel",
            task_id="task-recover-cancel",
            type="run.created",
            source="test",
            payload={"title": "Recover cancellation"},
        )
    )
    assignment = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="assignment.created",
            source="test",
            payload={
                "assignment_id": "assignment-recover-cancel",
                "agent_id": "scout",
                "stage_id": "evidence",
                "receiver": "scout",
            },
        )
    )
    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="assignment.lease.acquired",
            source="test",
            payload={
                "lease_id": "lease:assignment-recover-cancel",
                "assignment_id": "assignment-recover-cancel",
                "agent_id": "scout",
                "stage_id": "evidence",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=1)
                ).isoformat(),
            },
            causation_id=assignment.id,
        )
    )
    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="run.cancel.requested",
            source="test",
            payload={"reason": "operator_requested"},
        )
    )
    runtime.scheduler.shutdown()

    recovered = ResidentRuntime(data_dir)
    try:
        recovered.run_until_idle(max_steps=20)
        snapshot = recovered.store.snapshot(run_id=created.run_id)
        assert snapshot["runs"][0]["status"] == "cancelled"
        assert snapshot["assignments"][0]["status"] == "cancelled"
        assert snapshot["leases"][0]["status"] == "released"
    finally:
        recovered.scheduler.shutdown()


def test_concurrent_mailbox_send_persists_one_semantic_delivery(tmp_path, monkeypatch):
    store = SQLiteEventStore(tmp_path / "events.db")
    first = DurableMailbox("worker", store)
    second = DurableMailbox("worker", store)
    trigger = store.append(
        Event(
            run_id="run-mailbox",
            task_id="task-mailbox",
            type="assignment.created",
            source="test",
            payload={"assignment_id": "assignment-mailbox", "agent_id": "worker"},
        )
    )
    gate = ThreadEvent()
    arrivals = 0
    lock = Lock()

    def synchronized_lookup(self, delivery_id):
        nonlocal arrivals
        with lock:
            arrivals += 1
            if arrivals == 2:
                gate.set()
        assert gate.wait(1)
        return None

    monkeypatch.setattr(DurableMailbox, "_delivery", synchronized_lookup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(mailbox.send, trigger, delivery_id="semantic-delivery")
            for mailbox in (first, second)
        ]
        [future.result(timeout=2) for future in futures]

    sent = [
        event
        for event in store.read_all()
        if event.type == "mailbox.delivery.sent"
        and event.payload.get("delivery_id") == "semantic-delivery"
    ]
    assert len(sent) == 1


def test_round_robin_prevents_a_hot_mailbox_from_starving_another_worker(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    hot = DurableMailbox("hot", store)
    quiet = DurableMailbox("quiet", store)
    for index in range(3):
        _send(store, hot, delivery_id=f"hot-{index}", worker_id="hot")
    _send(store, quiet, delivery_id="quiet-0", worker_id="quiet")
    scheduler = ResidentScheduler(store, FaultController(), max_workers=1)
    handled: list[str] = []
    scheduler.register(
        "hot", hot, lambda delivery: handled.append(delivery.delivery_id)
    )
    scheduler.register(
        "quiet", quiet, lambda delivery: handled.append(delivery.delivery_id)
    )
    try:
        assert scheduler.run_once()
        assert scheduler.run_once()
    finally:
        scheduler.shutdown()
    assert handled == ["hot-0", "quiet-0"]


def test_shutdown_waits_for_inflight_work_but_does_not_consume_queued_work(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    first_box = DurableMailbox("first", store)
    second_box = DurableMailbox("second", store)
    _send(store, first_box, delivery_id="delivery-active", worker_id="first")
    _send(store, second_box, delivery_id="delivery-queued", worker_id="second")
    scheduler = ResidentScheduler(store, FaultController(), max_workers=1)
    release = ThreadEvent()
    started = ThreadEvent()

    def blocked(_delivery):
        started.set()
        assert release.wait(2)

    scheduler.register("first", first_box, blocked)
    scheduler.register("second", second_box, blocked)
    assert scheduler.dispatch_available() == 1
    assert started.wait(1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        stopping = pool.submit(scheduler.shutdown)
        sleep(0.1)
        assert stopping.done() is False
        release.set()
        stopping.result(timeout=2)

    assert first_box.peek() is None
    assert second_box.peek().delivery_id == "delivery-queued"
    assert scheduler.dispatch_available() == 0


def test_nonblocking_shutdown_keeps_renewing_inflight_claims(tmp_path):
    path = tmp_path / "events.db"
    store = SQLiteEventStore(path)
    mailbox = DurableMailbox("worker", store)
    _send(store, mailbox, delivery_id="shutdown-active", worker_id="worker")
    scheduler = ResidentScheduler(
        store,
        FaultController(),
        max_workers=1,
        work_claim_seconds=1,
    )
    release = ThreadEvent()
    started = ThreadEvent()

    def blocked(_delivery):
        started.set()
        assert release.wait(3)

    scheduler.register("worker", mailbox, blocked)
    assert scheduler.dispatch_available() == 1
    assert started.wait(1)

    scheduler.shutdown(wait=False)
    sleep(1.1)
    competitor = SQLiteEventStore(path)
    assert (
        competitor.claim_work(
            claim_keys=(
                "delivery:worker:shutdown-active",
                "agent-run:assignment:run-shutdown-active:shutdown-active",
            ),
            owner_id="competing-scheduler",
            ttl_seconds=10,
        )
        is None
    )

    release.set()
    assert scheduler.wait_until_idle(timeout=3)
    scheduler.shutdown()


def test_resident_runtime_periodically_checks_for_cross_process_work(
    tmp_path, monkeypatch
):
    runtime = ResidentRuntime(tmp_path)
    original_dispatch = runtime.scheduler.dispatch_available
    calls = 0

    def observed_dispatch():
        nonlocal calls
        calls += 1
        return original_dispatch()

    monkeypatch.setattr(runtime.scheduler, "dispatch_available", observed_dispatch)
    runtime.start()
    try:
        _wait_until(lambda: calls >= 2, timeout=2)
    finally:
        runtime.stop()


def test_resident_team_contract_contains_a_real_parallel_fan_out_and_join():
    contract = ResidentDemoTeamTaskPack().team_contract()
    by_id = {stage.stage_id: stage for stage in contract.stages}

    assert contract.max_parallel_assignments == 2
    assert {stage.stage_id for stage in contract.stages if not stage.depends_on} == {
        "evidence",
        "risk",
    }
    assert set(by_id["artifact"].depends_on) == {"evidence", "risk"}


def test_background_runtime_overlaps_parallel_team_assignments(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    original_handle = runtime.team_workers.handle
    release = ThreadEvent()
    started = {"evidence": ThreadEvent(), "risk": ThreadEvent()}

    def observed_handle(delivery, *, agent_id: str):
        stage_id = str(delivery.event.payload.get("stage_id", ""))
        if delivery.event.type == "assignment.created" and stage_id in started:
            started[stage_id].set()
            assert release.wait(5)
        original_handle(delivery, agent_id=agent_id)

    runtime.team_workers.handle = observed_handle
    runtime.start()
    try:
        created = runtime.submit_task(
            TaskRequest(
                title="Parallel Team proof",
                brief="Run two independent evidence assignments before joining them.",
            )
        )
        assert started["evidence"].wait(3)
        assert started["risk"].wait(3)
        snapshot = runtime.snapshot(created.run_id)
        scheduler_view = snapshot["runtime"]["scheduler"]
        assert scheduler_view["active"] == 2
        assert scheduler_view["capacity"] == 2
        assert scheduler_view["policy"] == "round_robin"
        assert len(snapshot["queued_deliveries"]) == 2
        assert len(snapshot["work_claims"]) == 6
        assert (
            sum(
                claim["claim_key"].startswith("worker-slot:")
                for claim in snapshot["work_claims"]
            )
            == 2
        )
        active = {
            item["stage_id"]: item["agent_id"]
            for item in snapshot["leases"]
            if item["status"] == "active"
        }
        assert set(active) >= {"evidence", "risk"}
        assert active["evidence"] != active["risk"]
        release.set()
        deadline = monotonic() + 30
        while monotonic() < deadline:
            if runtime.snapshot(created.run_id)["run"]["status"] == "succeeded":
                break
            sleep(0.02)
        assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    finally:
        release.set()
        runtime.stop()


@pytest.mark.nightly
def test_scheduler_drains_a_multi_worker_burst_without_exceeding_capacity(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.db")
    scheduler = ResidentScheduler(store, FaultController(), max_workers=3)
    mailboxes = {
        worker_id: DurableMailbox(worker_id, store)
        for worker_id in ("alpha", "beta", "gamma", "delta")
    }
    expected = {f"{worker_id}-{index}" for worker_id in mailboxes for index in range(8)}
    for worker_id, mailbox in mailboxes.items():
        for index in range(8):
            _send(
                store,
                mailbox,
                delivery_id=f"{worker_id}-{index}",
                worker_id=worker_id,
            )

    lock = Lock()
    release_initial_burst = ThreadEvent()
    first_three_started = ThreadEvent()
    active = 0
    global_peak = 0
    worker_active = {worker_id: 0 for worker_id in mailboxes}
    worker_peak = {worker_id: 0 for worker_id in mailboxes}
    handled: list[str] = []

    def handler_for(worker_id: str):
        def handle(delivery):
            nonlocal active, global_peak
            with lock:
                active += 1
                worker_active[worker_id] += 1
                global_peak = max(global_peak, active)
                worker_peak[worker_id] = max(
                    worker_peak[worker_id], worker_active[worker_id]
                )
                if active == 3:
                    first_three_started.set()
            if not release_initial_burst.is_set():
                assert release_initial_burst.wait(3)
            sleep(0.005)
            with lock:
                handled.append(delivery.delivery_id)
                active -= 1
                worker_active[worker_id] -= 1

        return handle

    for worker_id, mailbox in mailboxes.items():
        scheduler.register(
            worker_id,
            mailbox,
            handler_for(worker_id),
            max_concurrency=2,
        )

    try:
        assert scheduler.dispatch_available() == 3
        assert first_three_started.wait(2)
        release_initial_burst.set()
        deadline = monotonic() + 20
        while monotonic() < deadline and (
            scheduler.pending_count or scheduler.in_flight_count
        ):
            completed = scheduler.completed_steps
            scheduler.dispatch_available()
            scheduler.wait_for_progress(completed_steps=completed, timeout=0.1)
        assert scheduler.wait_until_idle(timeout=2)
    finally:
        release_initial_burst.set()
        scheduler.shutdown()

    assert set(handled) == expected
    assert len(handled) == len(expected)
    assert global_peak == 3
    assert max(worker_peak.values()) <= 2
    assert all(mailbox.peek() is None for mailbox in mailboxes.values())
