from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

from crazy_harness.control_plane.kernel import (
    CommandCandidate,
    CommandKind,
)
from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.core.events import Event


def _team_run(runtime: ResidentRuntime):
    created = runtime.submit_task(
        TaskRequest(
            title="Canonical Team Worker",
            brief="Collect evidence, compose a bounded artifact, and review it.",
        )
    )
    runtime.run_until_idle(max_steps=160)
    return created, runtime.store.read_all(run_id=created.run_id)


def _child_events(events, assignment_id: str):
    child_task_id = f"{assignment_id}:agent-run"
    return [event for event in events if event.task_id == child_task_id]


def test_every_team_assignment_runs_through_the_canonical_agent_loop(tmp_path):
    runtime = ResidentRuntime(tmp_path)

    created, events = _team_run(runtime)

    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert runtime.snapshot(created.run_id)["run"]["behavior_version"] == "v0.5.0-dev"
    assignments = [event for event in events if event.type == "assignment.created"]
    assert len(assignments) == 3
    for assignment in assignments:
        child = _child_events(events, assignment.payload["assignment_id"])
        child_types = [event.type for event in child]
        assert child_types.count("agent.run.created") == 1
        assert "context.manifest.compiled" in child_types
        assert "model.requested" in child_types
        assert "model.completed" in child_types
        assert "agent.command.validated" in child_types
        assert "tool.completed" in child_types
        assert "completion.gate.passed" in child_types
        assert "agent.submitted" in child_types

    formal_results = [
        event
        for event in events
        if event.type in {"evidence.recorded", "artifact.recorded", "review.recorded"}
    ]
    assert len(formal_results) == 3
    for result in formal_results:
        assert result.payload["agent_run_id"].endswith(":agent-run")
        assert result.payload["submission_event_id"]
        assert result.payload["evidence_refs"]
        assert all(
            runtime.store.get_event(ref) is not None
            for ref in result.payload["evidence_refs"]
        )


def test_builder_and_peer_responder_both_use_persistent_agent_loops(tmp_path):
    runtime = ResidentRuntime(tmp_path)

    _, events = _team_run(runtime)

    artifact_assignment = next(
        event
        for event in events
        if event.type == "assignment.created"
        and event.payload["stage_id"] == "artifact"
    )
    builder_child = _child_events(events, artifact_assignment.payload["assignment_id"])
    waiting_index = next(
        index
        for index, event in enumerate(builder_child)
        if event.type == "agent.waiting"
    )
    response_index = next(
        index
        for index, event in enumerate(builder_child)
        if event.type == "a2a.peer.responded"
        and event.payload["correlation_id"]
        == builder_child[waiting_index].payload["correlation_id"]
    )
    resumed_index = next(
        index
        for index, event in enumerate(builder_child)
        if index > response_index and event.type == "model.requested"
    )
    assert waiting_index < response_index < resumed_index

    root_response = next(
        event
        for event in events
        if event.type == "a2a.peer.responded"
        and event.task_id == artifact_assignment.task_id
    )
    peer_child = [
        event
        for event in events
        if event.task_id == root_response.payload["agent_run_id"]
    ]
    assert {
        "model.requested",
        "agent.command.validated",
        "tool.completed",
        "agent.submitted",
    } <= {event.type for event in peer_child}
    assert "full_context" not in root_response.payload
    assert "local_plan" not in root_response.payload
    assert root_response.payload["brief"]
    assert root_response.payload["evidence_refs"]


def test_team_worker_rebuilds_after_builder_wait_without_replaying_actions(tmp_path):
    first = ResidentRuntime(tmp_path)
    created = first.submit_task(
        TaskRequest(title="Restart while waiting", brief="Prove durable A2A resume.")
    )

    builder_wait = None
    for _ in range(80):
        assert first.scheduler.run_once() is True
        builder_wait = next(
            (
                event
                for event in first.store.read_all(run_id=created.run_id)
                if event.type == "agent.waiting"
                and event.task_id.endswith(":artifact:attempt:1:agent-run")
            ),
            None,
        )
        if builder_wait is not None:
            break
    assert builder_wait is not None

    recovered = ResidentRuntime(tmp_path)
    recovered.run_until_idle(max_steps=160)
    events = recovered.store.read_all(run_id=created.run_id)

    assert recovered.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert sum(event.type == "a2a.peer.requested" for event in events) == 1
    assignments = [event for event in events if event.type == "assignment.created"]
    for assignment in assignments:
        child = _child_events(events, assignment.payload["assignment_id"])
        assert sum(event.type == "agent.submitted" for event in child) == 1


def _peer_candidate(
    *,
    created,
    assignment: dict,
    actor_id: str,
    key: str,
    receiver: str = "reviewer",
) -> CommandCandidate:
    return CommandCandidate(
        candidate_id=f"candidate-{key}",
        idempotency_key=key,
        run_id=created.run_id,
        task_id=created.task_id,
        actor_id=actor_id,
        kind=CommandKind.PEER_REQUEST,
        payload={
            "assignment_id": assignment["assignment_id"],
            "receiver": receiver,
            "scope": ["evidence"],
            "permissions": ["read"],
            "depth": 1,
            "peer_budget": 1,
            "brief": "verify the active assignment evidence",
        },
    )


def _advance_to_active_stage(runtime: ResidentRuntime, created, stage_id: str) -> dict:
    for _ in range(100):
        assignment = next(
            (
                item
                for item in runtime.snapshot(created.run_id)["assignments"]
                if item.get("stage_id") == stage_id
                and item.get("status") not in {"succeeded", "completed", "failed"}
            ),
            None,
        )
        if assignment is not None:
            return assignment
        assert runtime.scheduler.run_once() is True
    raise AssertionError(f"stage did not become active: {stage_id}")


def _seed_assignment_agent_run(
    runtime: ResidentRuntime,
    created,
    assignment: dict,
    *,
    seed_contract: dict | None = None,
    include_model_chain: bool = True,
    include_operation_terminal: bool = True,
    include_seed_causation: bool = True,
    orphan_model_trigger: bool = False,
    tool_name: str | None = None,
):
    agent_run_id = f"{assignment['assignment_id']}:agent-run"
    actor_id = str(assignment["agent_id"])
    contract = runtime.team_pack.assignment_contract(
        str(assignment["stage_id"])
    ).model_dump(mode="json")
    assignment_event = next(
        event
        for event in runtime.store.read_all(run_id=created.run_id)
        if event.type == "assignment.created"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
    )
    seed = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.run.created",
            source="runtime.team",
            payload={
                "agent_run_id": agent_run_id,
                "agent_run_kind": "assignment",
                "root_task_id": created.task_id,
                "assignment_id": assignment["assignment_id"],
                "agent_id": actor_id,
                "contract": seed_contract or contract,
            },
            causation_id=assignment_event.id if include_seed_causation else None,
        )
    )
    tool_turn_id = "turn-test-tool"
    tool_command_causation = seed.id
    model_trigger = seed
    if orphan_model_trigger:
        model_trigger = runtime.store.append(
            Event(
                run_id=created.run_id,
                task_id=agent_run_id,
                type="test.orphan.model-trigger",
                source="test",
                payload={},
            )
        )
    if include_model_chain:
        tool_requested_model = runtime.store.append(
            Event(
                run_id=created.run_id,
                task_id=agent_run_id,
                type="model.requested",
                source=actor_id,
                payload={"turn_id": tool_turn_id},
                causation_id=model_trigger.id,
            )
        )
        tool_completed_model = runtime.store.append(
            Event(
                run_id=created.run_id,
                task_id=agent_run_id,
                type="model.completed",
                source=actor_id,
                payload={"turn_id": tool_turn_id, "content": "{}"},
                causation_id=tool_requested_model.id,
            )
        )
        tool_command_causation = tool_completed_model.id
    required_tool = tool_name or str(contract["evidence_requirements"][0])
    tool_command = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.command.validated",
            source=actor_id,
            payload={
                "turn_id": tool_turn_id,
                "command": {
                    "type": "call_tool",
                    "reason": "collect test evidence",
                    "tool_name": required_tool,
                    "tool_args": {},
                },
            },
            causation_id=tool_command_causation,
        )
    )
    operation_id = f"operation:{agent_run_id}:test"
    operation = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="operation.started",
            source=actor_id,
            payload={
                "turn_id": tool_turn_id,
                "operation_id": operation_id,
                "tool_name": required_tool,
            },
            causation_id=tool_command.id,
        )
    )
    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="tool.requested",
            source=actor_id,
            payload={
                "turn_id": tool_turn_id,
                "operation_id": operation_id,
                "tool_name": required_tool,
            },
            causation_id=operation.id,
        )
    )
    tool = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="tool.completed",
            source=actor_id,
            payload={
                "turn_id": tool_turn_id,
                "operation_id": operation_id,
                "result": {"name": required_tool, "status": "ok"},
            },
            causation_id=operation.id,
        )
    )
    observation = tool
    if include_operation_terminal:
        observation = runtime.store.append(
            Event(
                run_id=created.run_id,
                task_id=agent_run_id,
                type="operation.completed",
                source=actor_id,
                payload={
                    "turn_id": tool_turn_id,
                    "operation_id": operation_id,
                    "result_event_id": tool.id,
                },
                causation_id=tool.id,
            )
        )
    turn_id = "turn-test-submission"
    artifact = {"summary": "tested"}
    command_causation = observation.id
    if include_model_chain:
        requested = runtime.store.append(
            Event(
                run_id=created.run_id,
                task_id=agent_run_id,
                type="model.requested",
                source=actor_id,
                payload={"turn_id": turn_id},
                causation_id=observation.id,
            )
        )
        completed = runtime.store.append(
            Event(
                run_id=created.run_id,
                task_id=agent_run_id,
                type="model.completed",
                source=actor_id,
                payload={"turn_id": turn_id, "content": "{}"},
                causation_id=requested.id,
            )
        )
        command_causation = completed.id
    command = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.command.validated",
            source=actor_id,
            payload={
                "turn_id": turn_id,
                "command": {
                    "type": "submit_output",
                    "reason": "test submission",
                    "artifact": artifact,
                },
            },
            causation_id=command_causation,
        )
    )
    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="completion.gate.passed",
            source=actor_id,
            payload={"turn_id": turn_id, "findings": []},
            causation_id=command.id,
        )
    )
    submission = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.submitted",
            source=actor_id,
            payload={"turn_id": turn_id, "artifact": artifact},
            causation_id=command.id,
        )
    )
    return agent_run_id, tool, submission


def _seed_peer_agent_run(
    runtime: ResidentRuntime,
    created,
    *,
    assignment: dict,
    request: Event,
    actor_id: str,
):
    correlation_id = str(request.payload["correlation_id"])
    agent_run_id = f"peer:{correlation_id}:agent-run"
    contract = runtime.team_pack.peer_contract().model_dump(mode="json")
    seed = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.run.created",
            source="runtime.team",
            payload={
                "agent_run_id": agent_run_id,
                "agent_run_kind": "peer",
                "root_task_id": created.task_id,
                "assignment_id": assignment["assignment_id"],
                "correlation_id": correlation_id,
                "agent_id": actor_id,
                "contract": contract,
            },
            causation_id=request.id,
        )
    )
    tool_name = str(contract["evidence_requirements"][0])
    tool_turn_id = "turn-test-peer-tool"
    requested_model = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="model.requested",
            source=actor_id,
            payload={"turn_id": tool_turn_id},
            causation_id=seed.id,
        )
    )
    completed_model = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="model.completed",
            source=actor_id,
            payload={"turn_id": tool_turn_id, "content": "{}"},
            causation_id=requested_model.id,
        )
    )
    tool_command = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.command.validated",
            source=actor_id,
            payload={
                "turn_id": tool_turn_id,
                "command": {
                    "type": "call_tool",
                    "reason": "inspect peer evidence",
                    "tool_name": tool_name,
                    "tool_args": {},
                },
            },
            causation_id=completed_model.id,
        )
    )
    operation_id = f"operation:{agent_run_id}:test"
    operation = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="operation.started",
            source=actor_id,
            payload={
                "turn_id": tool_turn_id,
                "operation_id": operation_id,
                "tool_name": tool_name,
            },
            causation_id=tool_command.id,
        )
    )
    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="tool.requested",
            source=actor_id,
            payload={
                "turn_id": tool_turn_id,
                "operation_id": operation_id,
                "tool_name": tool_name,
            },
            causation_id=operation.id,
        )
    )
    tool = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="tool.completed",
            source=actor_id,
            payload={
                "turn_id": tool_turn_id,
                "operation_id": operation_id,
                "result": {"name": tool_name, "status": "ok"},
            },
            causation_id=operation.id,
        )
    )
    operation_completed = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="operation.completed",
            source=actor_id,
            payload={
                "turn_id": tool_turn_id,
                "operation_id": operation_id,
                "result_event_id": tool.id,
            },
            causation_id=tool.id,
        )
    )
    turn_id = "turn-test-peer-submission"
    artifact = {"brief": "peer verification complete"}
    requested = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="model.requested",
            source=actor_id,
            payload={"turn_id": turn_id},
            causation_id=operation_completed.id,
        )
    )
    completed = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="model.completed",
            source=actor_id,
            payload={"turn_id": turn_id, "content": "{}"},
            causation_id=requested.id,
        )
    )
    command = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.command.validated",
            source=actor_id,
            payload={
                "turn_id": turn_id,
                "command": {
                    "type": "submit_output",
                    "reason": "test peer submission",
                    "artifact": artifact,
                },
            },
            causation_id=completed.id,
        )
    )
    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="completion.gate.passed",
            source=actor_id,
            payload={"turn_id": turn_id, "findings": []},
            causation_id=command.id,
        )
    )
    submission = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.submitted",
            source=actor_id,
            payload={"turn_id": turn_id, "artifact": artifact},
            causation_id=command.id,
        )
    )
    return agent_run_id, tool, submission


def test_peer_request_requires_the_actor_to_hold_an_active_assignment_lease(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Peer authority", brief="Reject an assignment impersonator.")
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    assert assignment["agent_id"] == "scout"

    impersonated = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id="builder",
            key="peer-impersonated",
        )
    )
    capability_denied = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id="scout",
            key="peer-legitimate",
        )
    )

    assert impersonated.accepted is False
    assert impersonated.reason == "peer_assignment_not_held_by_actor"
    assert capability_denied.accepted is False
    assert capability_denied.reason == "peer_request_not_allowed_by_assignment"


def test_peer_response_must_match_the_persisted_request_participants(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Peer response authority", brief="Reject a spoofed response.")
    )
    assignment = _advance_to_active_stage(runtime, created, "artifact")
    request_decision = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id="builder",
            key="peer-request-for-response",
        )
    )
    assert request_decision.accepted is True
    request = next(
        event
        for event in runtime.kernel.events_for(request_decision)
        if event.type == "a2a.peer.requested"
    )
    agent_run_id, tool, submission = _seed_peer_agent_run(
        runtime,
        created,
        assignment=assignment,
        request=request,
        actor_id="reviewer",
    )
    payload = {
        "assignment_id": assignment["assignment_id"],
        "receiver": "builder",
        "brief": "peer verification complete",
        "evidence_refs": [tool.id],
        "correlation_id": request.payload["correlation_id"],
        "agent_run_id": agent_run_id,
        "submission_event_id": submission.id,
    }

    spoofed = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-peer-spoofed-response",
            idempotency_key="peer-spoofed-response",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id="scout",
            kind=CommandKind.PEER_RESPONSE,
            payload=payload,
        )
    )
    legitimate = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-peer-legitimate-response",
            idempotency_key="peer-legitimate-response",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id="reviewer",
            kind=CommandKind.PEER_RESPONSE,
            payload=payload,
        )
    )

    assert spoofed.accepted is False
    assert spoofed.reason == "peer_response_actor_mismatch"
    assert legitimate.accepted is True


def test_peer_response_is_rejected_after_the_requesting_lease_expires(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Stale peer response",
            brief="Do not promote an answer for an expired Assignment attempt.",
        )
    )
    assignment = _advance_to_active_stage(runtime, created, "artifact")
    request_decision = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id="builder",
            key="peer-response-after-expiry",
        )
    )
    request = next(
        event
        for event in runtime.kernel.events_for(request_decision)
        if event.type == "a2a.peer.requested"
    )
    agent_run_id, tool, submission = _seed_peer_agent_run(
        runtime,
        created,
        assignment=assignment,
        request=request,
        actor_id="reviewer",
    )
    expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="assignment.lease.renewed",
            source="test.clock",
            payload={
                "lease_id": f"lease:{assignment['assignment_id']}",
                "assignment_id": assignment["assignment_id"],
                "agent_id": assignment["agent_id"],
                "expires_at": expired_at.isoformat(),
            },
        )
    )
    assert (
        runtime.store.projection("lease", assignment["assignment_id"])["status"]
        == "active"
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-stale-peer-response",
            idempotency_key="stale-peer-response",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id="reviewer",
            kind=CommandKind.PEER_RESPONSE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "receiver": "builder",
                "brief": "peer verification complete",
                "evidence_refs": [tool.id],
                "correlation_id": request.payload["correlation_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "peer_response_request_assignment_not_active"
    assert not any(
        event.type == "a2a.peer.responded"
        and event.payload.get("correlation_id") == request.payload["correlation_id"]
        for event in runtime.store.read_all(run_id=created.run_id)
    )


def test_peer_response_rechecks_the_lease_inside_the_commit_boundary(
    tmp_path, monkeypatch
):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Peer commit race",
            brief="Expire the requesting Lease after validation but before commit.",
        )
    )
    assignment = _advance_to_active_stage(runtime, created, "artifact")
    request_decision = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id="builder",
            key="peer-commit-race-request",
        )
    )
    request = next(
        event
        for event in runtime.kernel.events_for(request_decision)
        if event.type == "a2a.peer.requested"
    )
    agent_run_id, tool, submission = _seed_peer_agent_run(
        runtime,
        created,
        assignment=assignment,
        request=request,
        actor_id="reviewer",
    )
    original_commit = runtime.store.commit_command
    injected = False

    def expire_before_commit(*args, **kwargs):
        nonlocal injected
        if kwargs.get("state") == "accepted" and not injected:
            injected = True
            runtime.store.append(
                Event(
                    run_id=created.run_id,
                    task_id=created.task_id,
                    type="assignment.lease.expired",
                    source="test.race",
                    payload={
                        "lease_id": f"lease:{assignment['assignment_id']}",
                        "assignment_id": assignment["assignment_id"],
                        "agent_id": assignment["agent_id"],
                        "reason": "expired_between_validation_and_commit",
                    },
                )
            )
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "commit_command", expire_before_commit)
    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-peer-commit-race",
            idempotency_key="peer-commit-race-response",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id="reviewer",
            kind=CommandKind.PEER_RESPONSE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "receiver": "builder",
                "brief": "peer verification complete",
                "evidence_refs": [tool.id],
                "correlation_id": request.payload["correlation_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "peer_response_request_assignment_not_active"
    assert not any(
        event.type == "a2a.peer.responded"
        and event.payload.get("correlation_id") == request.payload["correlation_id"]
        for event in runtime.store.read_all(run_id=created.run_id)
    )


def test_concurrent_peer_responses_have_one_semantic_winner(tmp_path, monkeypatch):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Peer response uniqueness",
            brief="Only one response may become the formal fact for a request.",
        )
    )
    assignment = _advance_to_active_stage(runtime, created, "artifact")
    request_decision = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id="builder",
            key="peer-unique-request",
        )
    )
    request = next(
        event
        for event in runtime.kernel.events_for(request_decision)
        if event.type == "a2a.peer.requested"
    )
    agent_run_id, tool, submission = _seed_peer_agent_run(
        runtime,
        created,
        assignment=assignment,
        request=request,
        actor_id="reviewer",
    )
    payload = {
        "assignment_id": assignment["assignment_id"],
        "receiver": "builder",
        "brief": "peer verification complete",
        "evidence_refs": [tool.id],
        "correlation_id": request.payload["correlation_id"],
        "agent_run_id": agent_run_id,
        "submission_event_id": submission.id,
    }
    candidates = [
        CommandCandidate(
            candidate_id=f"candidate-peer-unique-{index}",
            idempotency_key=f"peer-unique-response-{index}",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id="reviewer",
            kind=CommandKind.PEER_RESPONSE,
            payload=payload,
        )
        for index in range(2)
    ]
    barrier = Barrier(2)
    original_commit = runtime.store.commit_command

    def synchronize_accepted_commits(*args, **kwargs):
        if kwargs.get("state") == "accepted":
            barrier.wait(timeout=3)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(
        runtime.store, "commit_command", synchronize_accepted_commits
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(runtime.kernel.submit, candidates))

    assert sorted(decision.accepted for decision in decisions) == [False, True]
    assert {decision.reason for decision in decisions if not decision.accepted} == {
        "peer_response_already_recorded"
    }
    responses = [
        event
        for event in runtime.store.read_all(run_id=created.run_id)
        if event.type == "a2a.peer.responded"
        and event.payload.get("correlation_id") == request.payload["correlation_id"]
    ]
    assert len(responses) == 1


def test_peer_agent_failure_immediately_fails_the_requesting_assignment(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Immediate peer failure",
            brief="Do not wait for the assignment Lease deadline.",
        )
    )
    assignment = _advance_to_active_stage(runtime, created, "artifact")
    request_decision = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id="builder",
            key="peer-that-fails",
        )
    )
    assert request_decision.accepted is True
    request = next(
        event
        for event in runtime.kernel.events_for(request_decision)
        if event.type == "a2a.peer.requested"
    )
    runtime.team_workers._ensure_peer_seed(request, agent_id="reviewer")
    correlation_id = str(request.payload["correlation_id"])
    agent_run_id = runtime.team_workers.task_pack.peer_agent_run_id(correlation_id)
    failure = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.failed",
            source="reviewer",
            payload={"reason": "peer provider unavailable"},
        )
    )
    loop = runtime.team_workers._peer_loop(request, agent_id="reviewer")

    runtime.team_workers._advance_peer(
        request,
        agent_id="reviewer",
        correlation_id=correlation_id,
        loop=loop,
    )
    runtime.team_workers._advance_peer(
        request,
        agent_id="reviewer",
        correlation_id=correlation_id,
        loop=loop,
    )

    events = runtime.store.read_all(run_id=created.run_id)
    failures = [
        event
        for event in events
        if event.type == "assignment.failed"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
    ]
    assert len(failures) == 1
    assert failures[0].causation_id == failure.id
    assert failures[0].payload["reason"] == (
        "peer AgentRun failed: peer provider unavailable"
    )
    assert runtime.store.projection("lease", assignment["assignment_id"])["status"] == (
        "released"
    )
    assert any(
        event.type == "agent.nudged"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
        and event.payload.get("kind") == "assignment_failure"
        for event in events
    )


def test_rejected_peer_promotion_immediately_fails_the_requesting_assignment(
    tmp_path,
):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Rejected peer promotion",
            brief="Reject a peer answer without tool evidence immediately.",
        )
    )
    assignment = _advance_to_active_stage(runtime, created, "artifact")
    request_decision = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id="builder",
            key="peer-with-unverifiable-result",
        )
    )
    request = next(
        event
        for event in runtime.kernel.events_for(request_decision)
        if event.type == "a2a.peer.requested"
    )
    seed = runtime.team_workers._ensure_peer_seed(request, agent_id="reviewer")
    submission = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=seed.task_id,
            type="agent.submitted",
            source="reviewer",
            payload={"artifact": {"brief": "unsupported peer claim"}},
            causation_id=seed.id,
        )
    )

    runtime.team_workers._promote_peer_response(
        request,
        submission,
        runtime.store.read_all(task_id=seed.task_id),
        agent_id="reviewer",
    )

    events = runtime.store.read_all(run_id=created.run_id)
    rejected = next(event for event in events if event.type == "agent.result.rejected")
    assert rejected.payload["reason"] == "invalid_peer_response_schema"
    failure = next(
        event
        for event in events
        if event.type == "assignment.failed"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
    )
    assert failure.causation_id == rejected.id
    assert failure.payload["reason"] == (
        "peer result rejected: invalid_peer_response_schema"
    )
    assert runtime.store.projection("lease", assignment["assignment_id"])["status"] == (
        "released"
    )


def test_rejected_assignment_promotion_immediately_releases_its_lease(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Rejected assignment promotion",
            brief="Do not wait for Deadline after a Kernel rejection.",
        )
    )
    assignment = _advance_to_active_stage(runtime, created, "evidence")
    assignment_event = next(
        event
        for event in runtime.store.read_all(run_id=created.run_id)
        if event.type == "assignment.created"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
    )
    seed = runtime.team_workers._ensure_assignment_seed(assignment_event)
    submission = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=seed.task_id,
            type="agent.submitted",
            source=assignment["agent_id"],
            payload={"artifact": {"summary": "claim without tool evidence"}},
            causation_id=seed.id,
        )
    )

    runtime.team_workers._promote_assignment_result(
        assignment_event,
        submission,
        runtime.store.read_all(task_id=seed.task_id),
    )

    events = runtime.store.read_all(run_id=created.run_id)
    rejected = next(
        event
        for event in events
        if event.type == "agent.result.rejected"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
    )
    assert rejected.payload["reason"] == "evidence_refs_empty"
    failure = next(
        event
        for event in events
        if event.type == "assignment.failed"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
    )
    assert failure.causation_id == rejected.id
    assert failure.payload["reason"] == (
        "assignment result rejected: evidence_refs_empty"
    )
    assert runtime.store.projection("lease", assignment["assignment_id"])["status"] == (
        "released"
    )
    assert any(
        event.type == "agent.nudged"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
        for event in events
    )


def test_peer_request_is_rejected_after_its_assignment_lease_expires(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Expired peer authority", brief="Reject stale worker authority."
        )
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    lease = runtime.snapshot(created.run_id)["leases"][0]
    after_deadline = datetime.fromisoformat(lease["expires_at"]) + timedelta(seconds=1)
    assert runtime.expire_due_leases(now=after_deadline) == 1

    decision = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id=assignment["agent_id"],
            key="peer-after-expiry",
        )
    )

    assert decision.accepted is False
    assert decision.reason == "peer_assignment_lease_not_active_for_actor"


def test_team_result_rejects_nonexistent_evidence_references(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Evidence authority", brief="Reject invented evidence IDs.")
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-invented-evidence",
            idempotency_key="invented-evidence",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id=assignment["agent_id"],
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "summary": "the text claims evidence exists",
                "evidence_refs": ["evt-that-does-not-exist"],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "evidence_ref_not_found:evt-that-does-not-exist"
    assert not any(
        event.type == "evidence.recorded"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
        for event in runtime.store.read_all(run_id=created.run_id)
    )


def test_malformed_peer_payload_is_durably_rejected_instead_of_crashing_kernel(
    tmp_path,
):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Malformed peer payload", brief="Reject invalid A2A schema.")
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    malformed = _peer_candidate(
        created=created,
        assignment=assignment,
        actor_id=assignment["agent_id"],
        key="malformed-peer-payload",
    )
    malformed.payload["scope"] = 1

    decision = runtime.kernel.submit(malformed)

    assert decision.accepted is False
    assert decision.reason == "invalid_peer_request_schema"
    assert (
        runtime.store.command_record(malformed.idempotency_key)["state"] == "rejected"
    )


def test_assignment_without_peer_capability_cannot_open_a_peer_channel(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Peer capability", brief="Only delegated stages may use A2A.")
    )
    assert runtime.scheduler.run_once() is True
    evidence_assignment = runtime.snapshot(created.run_id)["assignments"][0]

    decision = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=evidence_assignment,
            actor_id=evidence_assignment["agent_id"],
            key="peer-without-assignment-capability",
        )
    )

    assert decision.accepted is False
    assert decision.reason == "peer_request_not_allowed_by_assignment"


def test_model_cannot_inflate_the_harness_peer_budget(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Peer budget", brief="Harness owns the peer-call limit.")
    )
    artifact_assignment = _advance_to_active_stage(runtime, created, "artifact")
    inflated = _peer_candidate(
        created=created,
        assignment=artifact_assignment,
        actor_id=artifact_assignment["agent_id"],
        key="inflated-peer-budget",
        receiver="scout",
    )
    inflated.payload["peer_budget"] = 999

    decision = runtime.kernel.submit(inflated)

    assert decision.accepted is False
    assert decision.reason == "peer_budget_escalation"
    assert not any(
        event.type == "a2a.peer.requested"
        and event.payload.get("assignment_id") == artifact_assignment["assignment_id"]
        for event in runtime.store.read_all(run_id=created.run_id)
    )


def test_peer_request_cannot_use_the_requester_as_its_own_reviewer(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Independent peer", brief="Reject self-reconciliation.")
    )
    assignment = _advance_to_active_stage(runtime, created, "artifact")

    decision = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id="builder",
            receiver="builder",
            key="self-peer-request",
        )
    )

    assert decision.accepted is False
    assert decision.reason == "peer_self_request_denied"


def test_peer_receiver_must_advertise_the_response_capability(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Receiver capability",
            brief="AgentCard authority applies to both A2A participants.",
        )
    )
    assignment = _advance_to_active_stage(runtime, created, "artifact")
    runtime.store.append(
        Event(
            run_id="control-plane",
            task_id="control-plane",
            type="agent.registered",
            source="test",
            payload={
                "agent_id": "reviewer",
                "role": "Reviewer without peer response authority",
                "capabilities": ["artifact.review", "evidence.verify"],
                "max_concurrency": 1,
            },
        )
    )

    decision = runtime.kernel.submit(
        _peer_candidate(
            created=created,
            assignment=assignment,
            actor_id="builder",
            receiver="reviewer",
            key="incapable-peer-receiver",
        )
    )

    assert decision.accepted is False
    assert decision.reason == "peer_receiver_not_capable"
    assert not any(
        event.type == "a2a.peer.requested"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
        for event in runtime.store.read_all(run_id=created.run_id)
    )


def test_existing_but_unrelated_event_cannot_masquerade_as_team_evidence(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Evidence semantics", brief="Reject unrelated event refs.")
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    run_created = next(
        event
        for event in runtime.store.read_all(run_id=created.run_id)
        if event.type == "run.created"
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-unrelated-evidence",
            idempotency_key="unrelated-evidence",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id=assignment["agent_id"],
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "summary": "a run event is not tool evidence",
                "evidence_refs": [run_created.id],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "evidence_ref_type_not_allowed:run.created"


def test_team_result_task_must_match_the_root_assignment_task(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(title="Task lineage", brief="Prevent cross-AgentRun pollution.")
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    agent_run_id, tool, submission = _seed_assignment_agent_run(
        runtime, created, assignment
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-wrong-result-task",
            idempotency_key="wrong-result-task",
            run_id=created.run_id,
            task_id="another-agent-run",
            actor_id=assignment["agent_id"],
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
                "summary": "valid evidence sent to the wrong task",
                "evidence_refs": [tool.id],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "assignment_task_mismatch"
    assert not any(
        event.type == "evidence.recorded" and event.task_id == "another-agent-run"
        for event in runtime.store.read_all(run_id=created.run_id)
    )


def test_assignment_candidate_payload_must_match_the_persisted_submission(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Submission binding",
            brief="Reject payload changes made by the promotion adapter.",
        )
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    agent_run_id, tool, submission = _seed_assignment_agent_run(
        runtime, created, assignment
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-tampered-submission",
            idempotency_key="tampered-submission",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id=assignment["agent_id"],
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
                "summary": "adapter replaced the submitted summary",
                "evidence_refs": [tool.id],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "assignment_submission_payload_mismatch"


def test_assignment_agent_run_contract_must_match_the_persisted_assignment(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Contract provenance",
            brief="A child AgentRun cannot replace its durable AssignmentContract.",
        )
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    forged_contract = runtime.team_pack.assignment_contract(
        assignment["stage_id"]
    ).model_dump(mode="json")
    forged_contract["goal"] = "A forged child goal"
    agent_run_id, tool, submission = _seed_assignment_agent_run(
        runtime,
        created,
        assignment,
        seed_contract=forged_contract,
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-forged-agent-run-contract",
            idempotency_key="forged-agent-run-contract",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id=assignment["agent_id"],
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
                "summary": "tested",
                "evidence_refs": [tool.id],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "assignment_agent_run_contract_mismatch"


def test_assignment_submission_requires_a_persisted_model_command_chain(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Model provenance",
            brief="A manually inserted command is not a canonical AgentLoop result.",
        )
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    agent_run_id, tool, submission = _seed_assignment_agent_run(
        runtime,
        created,
        assignment,
        include_model_chain=False,
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-model-chain-bypass",
            idempotency_key="model-chain-bypass",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id=assignment["agent_id"],
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
                "summary": "tested",
                "evidence_refs": [tool.id],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "assignment_submission_model_chain_invalid"


def test_assignment_tool_evidence_requires_a_terminal_operation(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Operation terminal provenance",
            brief="A successful tool result is not settled until its Operation closes.",
        )
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    agent_run_id, tool, submission = _seed_assignment_agent_run(
        runtime,
        created,
        assignment,
        include_operation_terminal=False,
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-unsettled-operation",
            idempotency_key="unsettled-operation",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id=assignment["agent_id"],
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
                "summary": "tested",
                "evidence_refs": [tool.id],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "assignment_tool_evidence_chain_invalid"


def test_assignment_agent_run_seed_must_be_caused_by_its_assignment(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="AgentRun root provenance",
            brief="Bind the child AgentRun start to the durable Assignment fact.",
        )
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    agent_run_id, tool, submission = _seed_assignment_agent_run(
        runtime,
        created,
        assignment,
        include_seed_causation=False,
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-unbound-agent-run-seed",
            idempotency_key="unbound-agent-run-seed",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id=assignment["agent_id"],
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
                "summary": "tested",
                "evidence_refs": [tool.id],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "assignment_agent_run_provenance_mismatch"


def test_model_request_trigger_must_trace_back_to_the_agent_run_seed(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Model request root provenance",
            brief="Reject an orphan trigger inserted inside the child EventLog.",
        )
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    agent_run_id, tool, submission = _seed_assignment_agent_run(
        runtime,
        created,
        assignment,
        orphan_model_trigger=True,
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-orphan-model-trigger",
            idempotency_key="orphan-model-trigger",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id=assignment["agent_id"],
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
                "summary": "tested",
                "evidence_refs": [tool.id],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "assignment_submission_model_chain_invalid"


def test_assignment_evidence_must_satisfy_the_persisted_tool_requirements(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Tool evidence provenance",
            brief="A different successful tool cannot satisfy the durable Contract.",
        )
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    agent_run_id, tool, submission = _seed_assignment_agent_run(
        runtime,
        created,
        assignment,
        tool_name="team.unrelated.inspect",
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-wrong-contract-tool",
            idempotency_key="wrong-contract-tool",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id=assignment["agent_id"],
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
                "summary": "tested",
                "evidence_refs": [tool.id],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == (
        "assignment_evidence_requirements_missing:team.evidence.collect"
    )


def test_assignment_submission_requires_a_passed_completion_gate(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Gate provenance",
            brief="A bare submitted event is not a completed AgentRun.",
        )
    )
    assert runtime.scheduler.run_once() is True
    assignment = runtime.snapshot(created.run_id)["assignments"][0]
    agent_run_id = f"{assignment['assignment_id']}:ungated-agent-run"
    actor_id = str(assignment["agent_id"])
    assignment_event = next(
        event
        for event in runtime.store.read_all(run_id=created.run_id)
        if event.type == "assignment.created"
        and event.payload.get("assignment_id") == assignment["assignment_id"]
    )
    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.run.created",
            source="runtime.team",
            payload={
                "agent_run_id": agent_run_id,
                "agent_run_kind": "assignment",
                "root_task_id": created.task_id,
                "assignment_id": assignment["assignment_id"],
                "agent_id": actor_id,
                "contract": assignment["contract"],
            },
            causation_id=assignment_event.id,
        )
    )
    tool = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="tool.completed",
            source=actor_id,
            payload={"result": {"name": "test.evidence", "status": "ok"}},
        )
    )
    submission = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=agent_run_id,
            type="agent.submitted",
            source=actor_id,
            payload={"turn_id": "turn-ungated", "artifact": {"summary": "ungated"}},
            causation_id=tool.id,
        )
    )

    decision = runtime.kernel.submit(
        CommandCandidate(
            candidate_id="candidate-ungated-submission",
            idempotency_key="ungated-submission",
            run_id=created.run_id,
            task_id=created.task_id,
            actor_id=actor_id,
            kind=CommandKind.EVIDENCE,
            payload={
                "assignment_id": assignment["assignment_id"],
                "agent_run_id": agent_run_id,
                "submission_event_id": submission.id,
                "summary": "ungated",
                "evidence_refs": [tool.id],
            },
        )
    )

    assert decision.accepted is False
    assert decision.reason == "assignment_submission_chain_invalid"
