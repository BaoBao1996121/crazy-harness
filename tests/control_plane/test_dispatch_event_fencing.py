from datetime import datetime, timedelta, timezone

import pytest

from crazy_harness.control_plane.store import SQLiteEventStore, WorkClaimLost
from crazy_harness.core.dispatch import DispatchContext, activate_dispatch_context
from crazy_harness.core.events import Event


def event(event_type: str, *, event_id: str) -> Event:
    return Event(
        id=event_id,
        run_id="run-1",
        task_id="task-1",
        type=event_type,
        source="test-worker",
        payload={"result": {"status": "passed"}},
    )


@pytest.mark.parametrize(
    "event_type",
    ("tool.completed", "agent.progress.updated", "operation.completed"),
)
def test_stale_dispatch_owner_cannot_append_trusted_event(tmp_path, event_type):
    store = SQLiteEventStore(tmp_path / "control.db")
    now = datetime.now(timezone.utc)
    claim_keys = ("delivery:builder:delivery-1", "agent-run:builder:run-1")
    stale_tokens = store.claim_work(
        claim_keys=claim_keys,
        owner_id="scheduler-old",
        ttl_seconds=1,
        now=now - timedelta(seconds=2),
    )
    assert stale_tokens is not None
    assert store.claim_work(
        claim_keys=claim_keys,
        owner_id="scheduler-new",
        ttl_seconds=30,
        now=now,
    ) is not None
    stale_context = DispatchContext.create(
        worker_id="builder",
        delivery_id="delivery-1",
        claim_owner_id="scheduler-old",
        claim_tokens=stale_tokens,
    )
    stale_event = event(event_type, event_id=f"stale-{event_type}")

    with activate_dispatch_context(stale_context), pytest.raises(WorkClaimLost):
        store.append(stale_event)

    assert stale_event.id not in {item.id for item in store.read_all()}


def test_current_dispatch_owner_can_append_trusted_event(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    tokens = store.claim_work(
        claim_keys=("delivery:builder:delivery-1", "agent-run:builder:run-1"),
        owner_id="scheduler-current",
        ttl_seconds=30,
    )
    assert tokens is not None
    context = DispatchContext.create(
        worker_id="builder",
        delivery_id="delivery-1",
        claim_owner_id="scheduler-current",
        claim_tokens=tokens,
    )
    trusted_event = event("tool.completed", event_id="current-tool-completed")

    with activate_dispatch_context(context):
        persisted = store.append(trusted_event)

    assert persisted.id == trusted_event.id
    assert [item.id for item in store.read_all()] == [trusted_event.id]


def test_cancelled_dispatch_cannot_append_even_while_claim_is_active(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    tokens = store.claim_work(
        claim_keys=("delivery:builder:delivery-1", "agent-run:builder:run-1"),
        owner_id="scheduler-current",
        ttl_seconds=30,
    )
    assert tokens is not None
    context = DispatchContext.create(
        worker_id="builder",
        delivery_id="delivery-1",
        claim_owner_id="scheduler-current",
        claim_tokens=tokens,
    )
    context.cancellation.cancel("run cancelled")
    cancelled_event = event("agent.progress.updated", event_id="cancelled-progress")

    with activate_dispatch_context(context), pytest.raises(WorkClaimLost, match="run cancelled"):
        store.append(cancelled_event)

    assert store.read_all() == []


def test_expired_dispatch_claim_cannot_append_trusted_event(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    tokens = store.claim_work(
        claim_keys=("delivery:builder:delivery-1", "agent-run:builder:run-1"),
        owner_id="scheduler-current",
        ttl_seconds=1,
        now=datetime.now(timezone.utc) - timedelta(seconds=2),
    )
    assert tokens is not None
    context = DispatchContext.create(
        worker_id="builder",
        delivery_id="delivery-1",
        claim_owner_id="scheduler-current",
        claim_tokens=tokens,
    )

    with activate_dispatch_context(context), pytest.raises(WorkClaimLost):
        store.append(event("tool.completed", event_id="expired-tool-completed"))

    assert store.read_all() == []


def test_inactive_dispatch_claim_cannot_append_trusted_event(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    tokens = store.claim_work(
        claim_keys=("delivery:builder:delivery-1", "agent-run:builder:run-1"),
        owner_id="scheduler-current",
        ttl_seconds=30,
    )
    assert tokens is not None
    assert store.finish_work_claims(
        claims=tokens,
        owner_id="scheduler-current",
        state="released",
    )
    context = DispatchContext.create(
        worker_id="builder",
        delivery_id="delivery-1",
        claim_owner_id="scheduler-current",
        claim_tokens=tokens,
    )

    with activate_dispatch_context(context), pytest.raises(WorkClaimLost):
        store.append(event("agent.progress.updated", event_id="released-progress"))

    assert store.read_all() == []


def test_scheduler_diagnostic_outside_dispatch_context_is_not_fenced(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    assert store.claim_work(
        claim_keys=("delivery:builder:delivery-1", "agent-run:builder:run-1"),
        owner_id="scheduler-current",
        ttl_seconds=30,
    ) is not None
    diagnostic = event("runtime.delivery.claim.lost", event_id="claim-lost-diagnostic")

    persisted = store.append(diagnostic)

    assert persisted.id == diagnostic.id
    assert [item.id for item in store.read_all()] == [diagnostic.id]
