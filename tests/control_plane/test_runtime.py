from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError, Event as ThreadEvent
from time import monotonic, sleep

from crazy_harness.control_plane.kernel import FaultController
from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.control_plane.runtime import ResidentScheduler
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event
from crazy_harness.core.runtime import DurableMailbox


def test_resident_scheduler_serializes_concurrent_delivery_consumers(tmp_path):
    store = SQLiteEventStore(tmp_path / "control_plane.db")
    mailbox = DurableMailbox("worker", store)
    trigger = store.append(
        Event(
            run_id="run-concurrent",
            task_id="task-concurrent",
            type="assignment.created",
            source="test",
            payload={"assignment_id": "task-concurrent"},
        )
    )
    mailbox.send(trigger, delivery_id="delivery-concurrent")
    scheduler = ResidentScheduler(store, FaultController())
    overlap = Barrier(2)
    handled: list[str] = []

    def handler(delivery):
        handled.append(delivery.delivery_id)
        try:
            overlap.wait(timeout=0.2)
        except BrokenBarrierError:
            pass

    scheduler.register("worker", mailbox, handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: scheduler.run_once(), range(2)))

    assert handled == ["delivery-concurrent"]
    assert sorted(results) == [False, True]

def test_scheduler_signal_is_sticky_until_wait_consumes_it(tmp_path):
    scheduler = ResidentScheduler(SQLiteEventStore(tmp_path / "control_plane.db"), FaultController())

    scheduler.signal()
    started = monotonic()

    assert scheduler.wait(0.2) is True
    assert monotonic() - started < 0.1


def test_resident_runtime_does_not_poll_mailboxes_while_idle(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    first_attempt = ThreadEvent()
    attempts = 0
    original_run_once = runtime.scheduler.run_once

    def counted_run_once():
        nonlocal attempts
        attempts += 1
        first_attempt.set()
        return original_run_once()

    runtime.scheduler.run_once = counted_run_once
    runtime.start()
    try:
        assert first_attempt.wait(1)
        sleep(0.35)
        assert attempts == 1
    finally:
        runtime.stop()


def test_resident_runtime_wakes_for_work_after_becoming_idle(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    runtime.start()
    try:
        sleep(0.1)
        created = runtime.submit_task(
            TaskRequest(title="Wake acceptance", brief="Prove sticky scheduler wake."),
        )
        deadline = monotonic() + 15
        status = "queued"
        while monotonic() < deadline:
            status = runtime.snapshot(created.run_id)["run"]["status"]
            if status == "succeeded":
                break
            sleep(0.02)
        assert status == "succeeded"
    finally:
        runtime.stop()


def test_resident_runtime_runs_the_four_role_story_and_dream_to_completion(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Trace the release", brief="Collect evidence, propose a plan, and review it.")
    )

    runtime.run_until_idle(max_steps=80)
    snapshot = runtime.snapshot(created.run_id)
    event_types = [record.event.type for record in runtime.store.read_records(run_id=created.run_id)]

    assert snapshot["run"]["status"] == "succeeded"
    assert {item["agent_id"] for item in snapshot["agents"]} >= {
        "coordinator",
        "scout",
        "builder",
        "reviewer",
    }
    assert event_types.count("a2a.peer.requested") == 1
    assert "a2a.peer.responded" in event_types
    assert "context.item.offloaded" in event_types
    assert "completion.gate.passed" in event_types
    assert "dream.job.completed" in event_types
    assert snapshot["memories"][0]["status"] == "active"


def test_one_shot_kernel_crash_is_visible_and_delivery_recovers(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    runtime.arm_fault("after_candidate_persisted")
    created = runtime.submit_task(TaskRequest(title="Recover me", brief="Exercise the durable path."))

    runtime.run_until_idle(max_steps=100)
    records = runtime.store.read_records(run_id=created.run_id)

    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert any(record.event.type == "runtime.agent.crashed" for record in records)
    assert any(record.event.type == "candidate.recovered" for record in records)
    submitted_keys = [
        record.event.payload["idempotency_key"]
        for record in records
        if record.event.type == "candidate.submitted"
    ]
    assert len(submitted_keys) == len(set(submitted_keys))


def test_recursive_peer_delegation_is_denied_without_stalling_the_run(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(TaskRequest(title="Depth policy", brief="Check one-hop collaboration."))
    runtime.run_until_idle(max_steps=80)

    decision = runtime.submit_peer_probe(created.run_id, sender="scout", receiver="reviewer", depth=2)

    assert decision.accepted is False
    assert decision.reason == "peer_depth_exceeded"
    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
