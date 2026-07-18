import pytest

from crazy_harness.control_plane.kernel import (
    CommandCandidate,
    CommandKind,
    ControlKernel,
    FaultController,
    InjectedKernelCrash,
)
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event


def seed(store: SQLiteEventStore) -> None:
    store.append(Event(run_id="run-1", task_id="task-1", type="run.created", source="test", payload={}))


def candidate(kind: CommandKind, *, key: str, payload: dict, actor: str = "builder") -> CommandCandidate:
    return CommandCandidate(
        candidate_id=f"candidate-{key}",
        idempotency_key=key,
        run_id="run-1",
        task_id="task-1",
        actor_id=actor,
        kind=kind,
        payload=payload,
    )


@pytest.mark.smoke
def test_invalid_model_candidate_is_rejected_before_formal_a2a_event(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(store)
    kernel = ControlKernel(store)

    decision = kernel.submit(
        candidate(
            CommandKind.PEER_REQUEST,
            key="invalid-peer",
            payload={"assignment_id": "a-1", "depth": 1},
        )
    )

    assert decision.accepted is False
    assert decision.reason == "missing_fields:receiver,scope,permissions"
    assert not any(event.type == "a2a.peer.requested" for event in store.read_all())


def test_persisted_candidate_is_recovered_without_duplicate_formal_effect(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(store)
    fault = FaultController()
    fault.arm("after_candidate_persisted")
    kernel = ControlKernel(store, fault_controller=fault)
    command = candidate(
        CommandKind.EVIDENCE,
        key="evidence-once",
        payload={"assignment_id": "a-1", "summary": "tests pass", "evidence_refs": ["tool-1"]},
        actor="scout",
    )

    with pytest.raises(InjectedKernelCrash):
        kernel.submit(command)
    recovered = kernel.submit(command)
    reused = ControlKernel(SQLiteEventStore(tmp_path / "control.db")).submit(command)

    assert recovered.accepted is True and recovered.recovered is True
    assert reused.accepted is True and reused.reused is True
    events = store.read_all()
    assert sum(event.type == "candidate.submitted" for event in events) == 1
    assert sum(event.type == "evidence.recorded" for event in events) == 1


def test_command_events_and_ledger_finalization_commit_atomically(tmp_path):
    store = SQLiteEventStore(tmp_path / "control.db")
    seed(store)
    fault = FaultController()
    fault.arm("during_command_commit")
    kernel = ControlKernel(store, fault_controller=fault)
    command = candidate(
        CommandKind.PEER_REQUEST,
        key="atomic-peer",
        payload={
            "assignment_id": "a-1",
            "receiver": "scout",
            "scope": ["repo"],
            "permissions": ["read"],
            "depth": 1,
            "peer_budget": 1,
            "brief": "prove atomic materialization",
        },
    )

    with pytest.raises(InjectedKernelCrash):
        kernel.submit(command)

    assert store.command_record(command.idempotency_key)["state"] == "processing"
    rolled_back_types = {event.type for event in store.read_all()}
    assert "candidate.accepted" not in rolled_back_types
    assert "a2a.peer.requested" not in rolled_back_types

    recovered = kernel.submit(command)
    event_types = [event.type for event in store.read_all()]

    assert recovered.accepted is True
    assert recovered.recovered is True
    assert store.command_record(command.idempotency_key)["state"] == "accepted"
    assert event_types.count("candidate.accepted") == 1
    assert event_types.count("a2a.policy.allowed") == 1
    assert event_types.count("a2a.peer.requested") == 1
    assert event_types.count("assignment.waiting") == 1


def test_one_hop_peer_budget_is_enforced_from_durable_facts(tmp_path):
    path = tmp_path / "control.db"
    store = SQLiteEventStore(path)
    seed(store)
    first = candidate(
        CommandKind.PEER_REQUEST,
        key="peer-1",
        payload={
            "assignment_id": "a-1",
            "receiver": "scout",
            "scope": ["repo"],
            "permissions": ["read"],
            "depth": 1,
            "peer_budget": 1,
            "brief": "confirm evidence",
        },
    )
    second = first.model_copy(update={"candidate_id": "candidate-peer-2", "idempotency_key": "peer-2"})

    assert ControlKernel(store).submit(first).accepted is True
    denied = ControlKernel(SQLiteEventStore(path)).submit(second)

    assert denied.accepted is False
    assert denied.reason == "peer_budget_exhausted"
    assert sum(event.type == "a2a.peer.requested" for event in store.read_all()) == 1
