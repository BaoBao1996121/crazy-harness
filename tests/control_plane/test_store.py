from datetime import datetime, timedelta, timezone

import pytest

from crazy_harness.control_plane.store import (
    SQLiteEventStore,
    UnfencedAckError,
    WorkClaimLost,
)
from crazy_harness.core.events import Event
from crazy_harness.core.runtime import DurableMailbox


def event(event_type: str, *, event_id: str, payload: dict | None = None) -> Event:
    return Event(
        id=event_id,
        run_id="run-1",
        task_id="task-1",
        type=event_type,
        source="test",
        payload=payload or {},
    )


def test_sqlite_store_reopens_with_monotonic_cursor_and_deduplicates_events(tmp_path):
    path = tmp_path / "control.db"
    store = SQLiteEventStore(path)
    store.append(event("run.created", event_id="evt-1", payload={"title": "Demo"}))
    store.append(
        event("run.phase.changed", event_id="evt-2", payload={"phase": "plan"})
    )
    store.append(
        event("run.phase.changed", event_id="evt-2", payload={"phase": "plan"})
    )

    reopened = SQLiteEventStore(path)
    records = reopened.read_records(after=0, run_id="run-1")

    assert [record.cursor for record in records] == [1, 2]
    assert [record.event.id for record in records] == ["evt-1", "evt-2"]
    assert reopened.last(run_id="run-1").type == "run.phase.changed"


def test_existing_durable_mailbox_survives_sqlite_store_reopen(tmp_path):
    path = tmp_path / "control.db"
    mailbox = DurableMailbox("scout", SQLiteEventStore(path))
    delivery = mailbox.send(
        event("assignment.created", event_id="evt-a"), delivery_id="delivery-a"
    )

    reopened = DurableMailbox("scout", SQLiteEventStore(path))
    assert reopened.peek() == delivery

    reopened.ack(delivery.delivery_id)
    assert DurableMailbox("scout", SQLiteEventStore(path)).peek() is None


def test_event_projection_can_be_rebuilt_from_the_log(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    store.append(
        event(
            "agent.registered",
            event_id="evt-agent",
            payload={
                "agent_id": "scout",
                "role": "Scout",
                "capabilities": ["evidence.collect"],
            },
        )
    )
    store.append(
        event("runtime.agent.busy", event_id="evt-busy", payload={"agent_id": "scout"})
    )
    assert store.snapshot()["agents"][0]["status"] == "busy"

    store.clear_projections()
    assert store.snapshot()["agents"] == []
    store.rebuild_projections()

    agent = store.snapshot()["agents"][0]
    assert agent["agent_id"] == "scout"
    assert agent["status"] == "busy"


def test_agent_card_refresh_preserves_live_runtime_state(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    store.append(
        event(
            "agent.registered",
            event_id="evt-agent-v1",
            payload={
                "agent_id": "scout",
                "role": "Scout",
                "capabilities": ["evidence.collect"],
            },
        )
    )
    store.append(
        event(
            "runtime.agent.busy",
            event_id="evt-agent-busy",
            payload={"agent_id": "scout"},
        )
    )
    store.append(
        event(
            "runtime.agent.heartbeat",
            event_id="evt-agent-heartbeat",
            payload={"agent_id": "scout", "assignment_id": "assignment-1"},
        )
    )

    store.append(
        event(
            "agent.registered",
            event_id="evt-agent-v2",
            payload={
                "agent_id": "scout",
                "role": "Scout / 侦察",
                "capabilities": ["evidence.collect", "repo.inspect"],
            },
        )
    )

    agent = store.projection("agent", "scout")
    assert agent is not None
    assert agent["capabilities"] == ["evidence.collect", "repo.inspect"]
    assert agent["status"] == "busy"
    assert agent["active_assignment_id"] == "assignment-1"


def test_idle_agent_projection_clears_the_completed_active_assignment(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    store.append(
        event(
            "agent.registered",
            event_id="evt-agent",
            payload={
                "agent_id": "scout",
                "role": "Scout",
                "capabilities": ["evidence.collect"],
            },
        )
    )
    store.append(
        event(
            "runtime.agent.heartbeat",
            event_id="evt-heartbeat",
            payload={"agent_id": "scout", "assignment_id": "assignment-1"},
        )
    )
    heartbeat_at = store.snapshot()["agents"][0]["last_heartbeat_at"]
    store.append(
        event(
            "runtime.agent.idle",
            event_id="evt-idle",
            payload={"agent_id": "scout"},
        )
    )

    agent = store.snapshot()["agents"][0]
    assert agent["status"] == "idle"
    assert agent["active_assignment_id"] is None
    assert agent["last_heartbeat_at"] == heartbeat_at


def test_capability_manifest_projection_survives_rebuild(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    store.append(
        event("run.created", event_id="evt-run", payload={"title": "Capabilities"})
    )
    store.append(
        event(
            "capability.manifest.compiled",
            event_id="evt-capability",
            payload={
                "turn_id": "turn_1",
                "agent_id": "generalist",
                "assignment_id": "task-1",
                "strategy": "inline_all",
                "catalog_size": 8,
                "disclosed_count": 8,
                "withheld_count": 0,
                "excluded_count": 0,
                "manifest": {"disclosed_names": ["repo.read", "test.run"]},
            },
        )
    )

    before = store.snapshot(run_id="run-1")["capability_manifests"]
    store.clear_projections()
    store.rebuild_projections()
    after = store.snapshot(run_id="run-1")["capability_manifests"]

    assert before == after
    assert after[0]["agent_id"] == "generalist"
    assert after[0]["manifest"]["disclosed_names"] == ["repo.read", "test.run"]


def test_terminal_projections_are_absorbing_before_and_after_rebuild(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    assignment = {
        "assignment_id": "assignment-1",
        "agent_id": "scout",
        "stage_id": "evidence",
    }
    lease = {
        **assignment,
        "lease_id": "lease-1",
        "expires_at": "2030-01-01T00:00:00+00:00",
    }
    for item in (
        event("run.created", event_id="run-created"),
        event("assignment.created", event_id="assignment-created", payload=assignment),
        event("assignment.lease.acquired", event_id="lease-acquired", payload=lease),
        event("run.succeeded", event_id="run-succeeded"),
        event(
            "assignment.succeeded", event_id="assignment-succeeded", payload=assignment
        ),
        event("assignment.lease.released", event_id="lease-released", payload=lease),
        event("run.failed", event_id="late-run-failed"),
        event(
            "assignment.running", event_id="late-assignment-running", payload=assignment
        ),
        event(
            "assignment.lease.renewed",
            event_id="late-lease-renewed",
            payload={**lease, "expires_at": "2040-01-01T00:00:00+00:00"},
        ),
    ):
        store.append(item)

    def assert_terminal_state() -> None:
        snapshot = store.snapshot(run_id="run-1")
        assert snapshot["runs"][0]["status"] == "succeeded"
        assert snapshot["assignments"][0]["status"] == "succeeded"
        assert snapshot["leases"][0]["status"] == "released"
        assert snapshot["leases"][0]["expires_at"] == lease["expires_at"]

    assert_terminal_state()
    store.clear_projections()
    store.rebuild_projections()
    assert_terminal_state()


def test_sqlite_rejects_unfenced_ack_while_delivery_claim_is_active(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    mailbox = DurableMailbox("scout", store)
    delivery = mailbox.send(
        event("assignment.created", event_id="claimed-event"),
        delivery_id="claimed-delivery",
    )
    assert (
        store.claim_work(
            claim_keys=("delivery:scout:claimed-delivery", "agent-run:claimed-run"),
            owner_id="scheduler-owner",
            ttl_seconds=30,
        )
        is not None
    )

    with pytest.raises(UnfencedAckError):
        mailbox.ack(delivery.delivery_id)

    assert mailbox.peek() == delivery


def test_running_run_cannot_unfenced_ack_an_expired_delivery_claim(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    store.append(event("run.created", event_id="run-created"))
    mailbox = DurableMailbox("scout", store)
    delivery = mailbox.send(
        event("assignment.created", event_id="expired-claimed-event"),
        delivery_id="expired-claimed-delivery",
    )
    assert (
        store.claim_work(
            claim_keys=(
                "delivery:scout:expired-claimed-delivery",
                "agent-run:expired-claimed-run",
            ),
            owner_id="dead-scheduler",
            ttl_seconds=1,
            now=datetime.now(timezone.utc) - timedelta(seconds=2),
        )
        is not None
    )

    with pytest.raises(UnfencedAckError):
        mailbox.ack(delivery.delivery_id)

    assert mailbox.peek() == delivery


def test_cancelled_run_rejects_a_fenced_formal_command_commit(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    store.append(event("run.created", event_id="run-created"))
    claims = store.claim_work(
        claim_keys=("delivery:coordinator:cancelled", "agent-run:run-1"),
        owner_id="scheduler-owner",
        ttl_seconds=30,
    )
    assert claims is not None
    assert store.begin_command(
        idempotency_key="complete-after-cancel",
        candidate_id="candidate-after-cancel",
        candidate_json="{}",
    )
    store.append(
        event(
            "run.cancel.requested",
            event_id="cancel-requested",
            payload={"reason": "operator_requested"},
        )
    )

    with pytest.raises(WorkClaimLost, match="cancel"):
        store.commit_command(
            "complete-after-cancel",
            state="accepted",
            decision_json='{"accepted":true}',
            events=[event("run.succeeded", event_id="late-formal-success")],
            work_claim_owner_id="scheduler-owner",
            work_claims=claims,
        )

    assert not any(
        item.type == "run.succeeded" for item in store.read_all(run_id="run-1")
    )
    assert store.projection("run", "run-1")["status"] == "cancelling"


def test_default_mailbox_delivery_id_reuses_event_identity(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    mailbox = DurableMailbox("scout", store)
    trigger = event("assignment.created", event_id="semantic-event")

    first = mailbox.send(trigger)
    second = mailbox.send(trigger)

    assert first == second
    assert first.delivery_id == "event:semantic-event"
    assert (
        len([item for item in store.read_all() if item.type == "mailbox.delivery.sent"])
        == 1
    )
