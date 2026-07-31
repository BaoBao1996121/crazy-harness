import json

from crazy_harness.core.agents import (
    AgentLoop,
    AgentRunKind,
    AgentRunSession,
    AgentRunSessionIdentity,
    AgentRunStatus,
    LoopPhase,
)
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.models import FakeModelProvider
from crazy_harness.core.tools import ToolRegistry


def test_agent_run_session_rebuilds_its_view_without_replaying_the_model(tmp_path):
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(
        Event(
            run_id="run-1",
            task_id="task-1",
            type="assignment.created",
            source="coordinator",
        )
    )
    identity = AgentRunSessionIdentity(
        run_id="run-1",
        task_id="task-1",
        agent_id="generalist",
        kind=AgentRunKind.SINGLE,
    )
    first_model = FakeModelProvider(
        [json.dumps({"type": "stop", "reason": "evidence is complete"})]
    )
    first_session = AgentRunSession(
        identity=identity,
        loop=AgentLoop(
            agent_id="generalist",
            task_id="task-1",
            model=first_model,
            event_log=event_log,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            tool_registry=ToolRegistry(),
        ),
    )

    assert first_session.view().status is AgentRunStatus.READY
    completed = first_session.step()

    assert completed.status is AgentRunStatus.COMPLETED
    assert completed.completed_turns == 1
    assert completed.latest_phase is LoopPhase.RESULT_RECORDING
    assert completed.latest_event_type == "agent.stopped"
    assert first_model.call_count == 1

    restart_model = FakeModelProvider(
        [json.dumps({"type": "stop", "reason": "must not be sampled"})]
    )
    restarted_session = AgentRunSession(
        identity=identity,
        loop=AgentLoop(
            agent_id="generalist",
            task_id="task-1",
            model=restart_model,
            event_log=event_log,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            tool_registry=ToolRegistry(),
        ),
    )

    assert restarted_session.view() == completed
    assert restarted_session.step() == completed
    assert restart_model.call_count == 0


def test_agent_run_session_separates_operator_control_from_wait_and_cancel_states():
    identity = AgentRunSessionIdentity(
        run_id="run-1",
        task_id="task-1",
        agent_id="generalist",
        kind=AgentRunKind.SINGLE,
    )
    created = Event(
        run_id="run-1",
        task_id="task-1",
        type="run.created",
        source="gateway.http",
        payload={"task_pack": "repo-maintainer"},
    )

    unknown = Event(
        run_id="run-1",
        task_id="task-1",
        type="operation.unknown",
        source="tool.pipeline",
        payload={"operation_id": "operation-1", "turn_id": "turn-1"},
    )
    system_pause = Event(
        run_id="run-1",
        task_id="task-1",
        type="run.paused",
        source="runtime.single",
        payload={"reason": "waiting for reconciliation"},
        causation_id=unknown.id,
    )
    assert AgentRunSession.project_view(
        identity, [created, unknown, system_pause]
    ).status is AgentRunStatus.BLOCKED

    waiting = Event(
        run_id="run-1",
        task_id="task-1",
        type="agent.waiting",
        source="agent.generalist",
        payload={"turn_id": "turn-1", "correlation_id": "approval-1"},
    )
    wait_pause = system_pause.model_copy(update={"causation_id": waiting.id})
    assert AgentRunSession.project_view(
        identity, [created, waiting, wait_pause]
    ).status is AgentRunStatus.WAITING

    plain_wait = waiting.model_copy(update={"payload": {"turn_id": "turn-1"}})
    assert AgentRunSession.project_view(
        identity, [created, plain_wait, wait_pause]
    ).status is AgentRunStatus.WAITING
    explicit_wake = Event(
        run_id="run-1",
        task_id="task-1",
        type="agent.nudge.set",
        source="runtime.agent-control",
        payload={"message": "continue with the new fact"},
    )
    assert AgentRunSession.project_view(
        identity, [created, plain_wait, wait_pause, explicit_wake]
    ).status is AgentRunStatus.RUNNING

    pause_requested = Event(
        run_id="run-1",
        task_id="task-1",
        type="run.pause.requested",
        source="runtime.agent-control",
        payload={"request_id": "pause-1", "reason": "inspect"},
    )
    operator_pause = Event(
        run_id="run-1",
        task_id="task-1",
        type="run.paused",
        source="runtime.agent-control",
        payload={"request_id": "pause-1", "reason": "inspect"},
        causation_id=pause_requested.id,
    )
    assert AgentRunSession.project_view(
        identity, [created, pause_requested, operator_pause]
    ).status is AgentRunStatus.PAUSED

    cancelling = Event(
        run_id="run-1",
        task_id="task-1",
        type="run.cancel.requested",
        source="runtime.control",
    )
    cancelled = Event(
        run_id="run-1",
        task_id="task-1",
        type="run.cancelled",
        source="runtime.control",
    )
    assert AgentRunSession.project_view(
        identity, [created, cancelling]
    ).status is AgentRunStatus.CANCELLING
    assert AgentRunSession.project_view(
        identity, [created, cancelling, cancelled]
    ).status is AgentRunStatus.CANCELLED


def test_agent_run_session_discloses_whether_verified_fork_is_supported():
    identity = AgentRunSessionIdentity(
        run_id="run-1",
        task_id="task-1",
        agent_id="generalist",
        kind=AgentRunKind.SINGLE,
    )

    supported = AgentRunSession.project_view(
        identity,
        [
            Event(
                run_id="run-1",
                task_id="task-1",
                type="run.created",
                source="gateway.http",
                payload={"task_pack": "repo-maintainer"},
            )
        ],
    )
    unsupported = AgentRunSession.project_view(
        identity,
        [
            Event(
                run_id="run-1",
                task_id="task-1",
                type="run.created",
                source="gateway.http",
                payload={"task_pack": "repo-quality"},
            )
        ],
    )

    assert supported.fork_supported is True
    assert supported.fork_ready is True
    assert supported.fork_blocker is None
    assert unsupported.fork_supported is False
    assert unsupported.fork_ready is False
    assert unsupported.fork_blocker
