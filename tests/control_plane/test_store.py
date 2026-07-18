from crazy_harness.control_plane.store import SQLiteEventStore
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
    store.append(event("run.phase.changed", event_id="evt-2", payload={"phase": "plan"}))
    store.append(event("run.phase.changed", event_id="evt-2", payload={"phase": "plan"}))

    reopened = SQLiteEventStore(path)
    records = reopened.read_records(after=0, run_id="run-1")

    assert [record.cursor for record in records] == [1, 2]
    assert [record.event.id for record in records] == ["evt-1", "evt-2"]
    assert reopened.last(run_id="run-1").type == "run.phase.changed"


def test_existing_durable_mailbox_survives_sqlite_store_reopen(tmp_path):
    path = tmp_path / "control.db"
    mailbox = DurableMailbox("scout", SQLiteEventStore(path))
    delivery = mailbox.send(event("assignment.created", event_id="evt-a"), delivery_id="delivery-a")

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
            payload={"agent_id": "scout", "role": "Scout", "capabilities": ["evidence.collect"]},
        )
    )
    store.append(event("runtime.agent.busy", event_id="evt-busy", payload={"agent_id": "scout"}))
    assert store.snapshot()["agents"][0]["status"] == "busy"

    store.clear_projections()
    assert store.snapshot()["agents"] == []
    store.rebuild_projections()

    agent = store.snapshot()["agents"][0]
    assert agent["agent_id"] == "scout"
    assert agent["status"] == "busy"


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
    store.append(event("run.created", event_id="evt-run", payload={"title": "Capabilities"}))
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
