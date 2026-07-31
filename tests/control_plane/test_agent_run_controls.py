import json
import threading
from time import monotonic, sleep

import pytest

from crazy_harness.control_plane.run_controls import (
    RunForkRequest,
    RunNudgeRequest,
    RunPauseRequest,
    RunResumeRequest,
)
from crazy_harness.control_plane.kernel import InjectedKernelCrash
from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.core.agents import AgentRunStatus
from crazy_harness.core.events import Event
from crazy_harness.core.models import FakeModelProvider
from crazy_harness.taskpacks import RepoMaintainerTaskPack


def _single_request() -> TaskRequest:
    return TaskRequest(
        title="Controllable repair",
        brief="Repair the implementation and prove it with tests.",
        execution_mode="single",
        model_mode="scripted",
        task_pack="repo-maintainer",
    )


def test_pause_and_resume_survive_restart_without_starting_a_model_call(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(_single_request())
    pause_request = RunPauseRequest(
        request_id="pause-before-first-turn",
        reason="inspect the queued run",
    )

    paused = runtime.pause_agent_run(created.run_id, pause_request)
    repeated = runtime.pause_agent_run(created.run_id, pause_request)

    assert paused == repeated
    assert paused.status == "paused"
    assert runtime.scheduler.run_once() is False
    for _ in range(3):
        assert runtime.scheduler.dispatch_available() == 0
    assert not any(
        event.type == "runtime.scheduler.backpressure"
        for event in runtime.store.read_all(run_id=created.run_id)
    )
    assert not any(
        event.type == "model.completed"
        for event in runtime.store.read_all(run_id=created.run_id)
    )

    restarted = ResidentRuntime(tmp_path)
    assert restarted.snapshot(created.run_id)["run"]["status"] == "paused"
    assert restarted.agent_run_view(created.run_id).status is AgentRunStatus.PAUSED
    assert restarted.scheduler.run_once() is False

    resumed = restarted.resume_agent_run(
        created.run_id,
        RunResumeRequest(
            request_id="resume-after-inspection",
            reason="continue from durable facts",
        ),
    )
    assert resumed.status == "running"
    restarted.run_until_idle(max_steps=50)

    events = restarted.store.read_all(run_id=created.run_id)
    assert restarted.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert [event.type for event in events].count("run.pause.requested") == 1
    assert [event.type for event in events].count("run.paused") == 1
    assert [event.type for event in events].count("run.resume.requested") == 1
    assert [event.type for event in events].count("run.resumed") == 1


def test_latest_persisted_nudge_replaces_the_previous_prompt_slot(tmp_path):
    class RecordingModel(FakeModelProvider):
        def __init__(self, responses):
            super().__init__(responses)
            self.message_batches = []

        def complete(self, messages, *, tools=None, response_schema=None):
            self.message_batches.append(messages)
            return super().complete(
                messages,
                tools=tools,
                response_schema=response_schema,
            )

    model = RecordingModel(RepoMaintainerTaskPack(tmp_path).scripted_responses())
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)
    created = runtime.submit_task(_single_request())
    runtime.pause_agent_run(
        created.run_id,
        RunPauseRequest(request_id="pause-for-nudge", reason="edit guidance"),
    )

    first = runtime.nudge_agent_run(
        created.run_id,
        RunNudgeRequest(
            request_id="nudge-1",
            message="OLD GUIDANCE: inspect only the first line.",
        ),
    )
    latest_request = RunNudgeRequest(
        request_id="nudge-2",
        message="LATEST GUIDANCE: inspect behavior and tests before editing.",
    )
    latest = runtime.nudge_agent_run(created.run_id, latest_request)
    repeated = runtime.nudge_agent_run(created.run_id, latest_request)

    assert latest == repeated
    assert latest.supersedes_event_id == first.nudge_event_id
    runtime.resume_agent_run(
        created.run_id,
        RunResumeRequest(request_id="resume-with-nudge", reason="apply latest guidance"),
    )
    assert runtime.scheduler.run_once() is True

    compiled_prompt = "\n".join(
        message.content for message in model.message_batches[0]
    )
    assert "LATEST GUIDANCE" in compiled_prompt
    assert "OLD GUIDANCE" not in compiled_prompt
    events = runtime.store.read_all(run_id=created.run_id)
    assert [event.type for event in events].count("agent.nudge.set") == 2
    request_event = next(event for event in events if event.type == "model.requested")
    assert request_event.payload["active_nudge_event_id"] == latest.nudge_event_id


def test_fork_is_idempotent_and_branch_lineage_is_rebuilt_from_restore_facts(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    source = runtime.submit_task(_single_request())
    for _ in range(4):
        assert runtime.scheduler.run_once() is True

    request = RunForkRequest(
        request_id="fork-after-first-edit",
        label="try another continuation",
    )
    forked = runtime.fork_agent_run(source.run_id, request)
    child_created = next(
        event
        for event in runtime.store.read_all(run_id=forked.run_id)
        if event.type == "run.created"
    )
    child_source = (
        runtime.data_dir
        / "workspaces"
        / forked.run_id
        / "calculator.py"
    )
    assert str(child_source.parent) == str(child_created.payload["workspace_path"])
    child_source.write_text(
        child_source.read_text(encoding="utf-8") + "\n# child advanced\n",
        encoding="utf-8",
    )
    repeated = runtime.fork_agent_run(source.run_id, request)

    assert forked == repeated
    assert forked.run_id != source.run_id
    source_branch = runtime.agent_run_branch(source.run_id)
    child_branch = runtime.agent_run_branch(forked.run_id)
    assert source_branch.parent_run_id is None
    assert source_branch.children_run_ids == (forked.run_id,)
    assert child_branch.parent_run_id == source.run_id
    assert child_branch.checkpoint_id == forked.checkpoint_id
    assert child_branch.source_event_id
    assert child_branch.source_turn_id
    assert runtime.agent_run_view(forked.run_id).status is AgentRunStatus.READY


def test_fork_assignment_waits_for_restore_commit_across_a_crash(
    tmp_path, monkeypatch
):
    runtime = ResidentRuntime(tmp_path)
    source = runtime.submit_task(_single_request())
    for _ in range(4):
        assert runtime.scheduler.run_once() is True

    request = RunForkRequest(
        request_id="fork-crash-before-commit",
        label="prove the restore publication barrier",
    )
    append_deterministic = runtime._append_deterministic

    def crash_before_restore_commit(identity, key, event_type, payload, **kwargs):
        if event_type == "checkpoint.restore.committed":
            raise InjectedKernelCrash("before checkpoint.restore.committed")
        return append_deterministic(
            identity,
            key,
            event_type,
            payload,
            **kwargs,
        )

    monkeypatch.setattr(runtime, "_append_deterministic", crash_before_restore_commit)

    with pytest.raises(InjectedKernelCrash, match="checkpoint.restore.committed"):
        runtime.fork_agent_run(source.run_id, request)

    checkpoint = next(
        event
        for event in runtime.store.read_all(run_id=source.run_id)
        if event.type == "checkpoint.committed"
    )
    child_id = runtime.checkpoint_restore_identity(
        str(checkpoint.payload["checkpoint_id"]),
        f"{request.request_id}:restore",
    ).run_id
    crashed_events = runtime.store.read_all(run_id=child_id)
    assert any(event.type == "assignment.created" for event in crashed_events)
    assert not any(
        event.type in {"checkpoint.restore.committed", "mailbox.delivery.sent"}
        for event in crashed_events
    )

    restarted = ResidentRuntime(tmp_path)
    restarted.run_until_idle(max_steps=20)
    assert not any(
        event.type == "mailbox.delivery.sent"
        for event in restarted.store.read_all(run_id=child_id)
    )

    restored = restarted.fork_agent_run(source.run_id, request)
    restored_events = restarted.store.read_all(run_id=restored.run_id)
    event_types = [event.type for event in restored_events]
    assert restored.run_id == child_id
    assert event_types.count("assignment.created") == 1
    assert event_types.count("checkpoint.restore.committed") == 1
    assert event_types.count("mailbox.delivery.sent") == 1
    assert event_types.index("checkpoint.restore.committed") < event_types.index(
        "mailbox.delivery.sent"
    )


def test_unsupported_fork_is_rejected_before_a_checkpoint_is_created(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        _single_request().model_copy(update={"task_pack": "repo-quality"})
    )

    with pytest.raises(ValueError, match="repo-maintainer"):
        runtime.fork_agent_run(
            created.run_id,
            RunForkRequest(request_id="unsupported-fork", label="must not persist"),
        )

    events = runtime.store.read_all(run_id=created.run_id)
    assert not any(event.type.startswith("checkpoint.") for event in events)
    assert runtime.agent_run_view(created.run_id).fork_supported is False


def test_agent_run_view_separates_fork_capability_from_current_boundary(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(_single_request())

    initial = runtime.agent_run_view(created.run_id)
    assert initial.fork_supported is True
    assert initial.fork_ready is True

    claim_key = f"agent-run:generalist:{created.run_id}"
    claims = runtime.store.claim_work(
        claim_keys=(claim_key,),
        owner_id="fork-readiness-test",
        ttl_seconds=30,
        run_id=created.run_id,
    )
    assert claims is not None
    active = runtime.agent_run_view(created.run_id)
    assert active.fork_ready is False
    assert "active Agent turn" in str(active.fork_blocker)
    assert runtime.store.finish_work_claims(
        claims=claims,
        owner_id="fork-readiness-test",
        state="released",
    )

    started = Event(
        run_id=created.run_id,
        task_id=created.task_id,
        type="operation.started",
        source="test",
        payload={
            "turn_id": "turn-inflight",
            "operation_id": "operation-inflight",
            "tool_name": "repo.read",
            "side_effect_level": "none",
        },
    )
    runtime.store.append(started)
    unsafe = runtime.agent_run_view(created.run_id)
    assert unsafe.fork_supported is True
    assert unsafe.fork_ready is False
    assert "unresolved operation" in str(unsafe.fork_blocker)

    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="operation.completed",
            source="test",
            payload={"operation_id": "operation-inflight"},
            causation_id=started.id,
        )
    )
    safe = runtime.agent_run_view(created.run_id)
    assert safe.fork_ready is True
    assert safe.fork_blocker is None


def test_persisted_resume_request_is_reconciled_after_restart(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(_single_request())
    runtime.pause_agent_run(
        created.run_id,
        RunPauseRequest(request_id="pause-before-resume-crash", reason="inspect"),
    )
    run_created = next(
        event
        for event in runtime.store.read_all(run_id=created.run_id)
        if event.type == "run.created"
    )
    resume_requested = Event(
        run_id=created.run_id,
        task_id=created.task_id,
        type="run.resume.requested",
        source="runtime.agent-control",
        payload={"request_id": "resume-before-crash", "reason": "continue"},
        causation_id=run_created.id,
    )
    runtime.store.append(resume_requested)

    restarted = ResidentRuntime(tmp_path)
    assert restarted._reconcile_resumes() is True
    assert restarted.snapshot(created.run_id)["run"]["status"] == "running"
    assert sum(
        event.type == "run.resumed" and event.causation_id == resume_requested.id
        for event in restarted.store.read_all(run_id=created.run_id)
    ) == 1


def test_stale_resume_does_not_override_a_newer_pause_after_restart(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(_single_request())
    runtime.pause_agent_run(
        created.run_id,
        RunPauseRequest(request_id="pause-before-stale-resume", reason="inspect"),
    )
    first_pause = next(
        event
        for event in runtime.store.read_all(run_id=created.run_id)
        if event.type == "run.pause.requested"
    )
    stale_resume = runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="run.resume.requested",
            source="runtime.agent-control",
            payload={"request_id": "stale-resume", "reason": "old intent"},
            causation_id=first_pause.id,
        )
    )
    latest_pause = runtime.pause_agent_run(
        created.run_id,
        RunPauseRequest(request_id="latest-pause", reason="newest intent wins"),
    )
    assert latest_pause.status == "paused"

    restarted = ResidentRuntime(tmp_path)
    assert restarted._reconcile_resumes() is False
    assert restarted.snapshot(created.run_id)["run"]["status"] == "paused"
    assert not any(
        event.type == "run.resumed" and event.causation_id == stale_resume.id
        for event in restarted.store.read_all(run_id=created.run_id)
    )


def test_resume_rejects_a_system_wait_without_an_operator_pause(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(_single_request())
    run_created = next(
        event
        for event in runtime.store.read_all(run_id=created.run_id)
        if event.type == "run.created"
    )
    runtime.store.append(
        Event(
            run_id=created.run_id,
            task_id=created.task_id,
            type="run.paused",
            source="runtime.single",
            payload={"reason": "waiting for external reconciliation"},
            causation_id=run_created.id,
        )
    )

    with pytest.raises(ValueError, match="operator pause"):
        runtime.resume_agent_run(
            created.run_id,
            RunResumeRequest(request_id="unsafe-resume", reason="do not bypass wait"),
        )


def test_paused_delivery_is_not_used_as_another_runs_backpressure_identity(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    source = runtime.submit_task(_single_request())
    runtime.pause_agent_run(
        source.run_id,
        RunPauseRequest(request_id="pause-parent", reason="keep the parent still"),
    )
    children = [
        runtime.fork_agent_run(
            source.run_id,
            RunForkRequest(request_id=f"fork-child-{index}", label=f"child {index}"),
        )
        for index in range(2)
    ]

    try:
        assert runtime.scheduler.dispatch_available() == 1
        backpressure = [
            event
            for event in runtime.store.read_all()
            if event.type == "runtime.scheduler.backpressure"
        ]
        assert len(backpressure) == 1
        assert backpressure[0].run_id in {child.run_id for child in children}
        assert backpressure[0].run_id != source.run_id
    finally:
        runtime.scheduler.shutdown(wait=True)


def test_pause_waits_for_the_active_turn_boundary_and_blocks_the_next_turn(tmp_path):
    class BlockingModel(FakeModelProvider):
        def __init__(self):
            super().__init__(
                [
                    json.dumps(
                        {
                            "type": "call_tool",
                            "reason": "inspect before pausing",
                            "tool_name": "repo.read",
                            "tool_args": {"path": "calculator.py"},
                        }
                    )
                ]
            )
            self.started = threading.Event()
            self.release = threading.Event()

        def complete(self, messages, *, tools=None, response_schema=None):
            self.started.set()
            assert self.release.wait(timeout=5)
            return super().complete(
                messages,
                tools=tools,
                response_schema=response_schema,
            )

    model = BlockingModel()
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)
    runtime.start()
    try:
        created = runtime.submit_task(_single_request())
        assert model.started.wait(timeout=5)

        pausing = runtime.pause_agent_run(
            created.run_id,
            RunPauseRequest(request_id="pause-in-flight", reason="inspect boundary"),
        )
        assert pausing.status == "pausing"
        assert runtime.snapshot(created.run_id)["run"]["status"] == "pausing"

        model.release.set()
        deadline = monotonic() + 5
        while runtime.snapshot(created.run_id)["run"]["status"] != "paused":
            assert monotonic() < deadline
            sleep(0.02)

        events = runtime.store.read_all(run_id=created.run_id)
        assert model.call_count == 1
        assert any(event.type == "tool.completed" for event in events)
        assert [event.type for event in events].index("tool.completed") < [
            event.type for event in events
        ].index("run.paused")
    finally:
        model.release.set()
        runtime.stop()


def test_pause_does_not_reject_an_inflight_deepseek_reservation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    model = FakeModelProvider(
        [
            json.dumps(
                {
                    "type": "call_tool",
                    "reason": "inspect before pausing",
                    "tool_name": "repo.read",
                    "tool_args": {"path": "calculator.py"},
                }
            )
        ]
    )
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)
    reservation_entered = threading.Event()
    reservation_release = threading.Event()
    reserve = runtime.store.reserve_model_call

    def delayed_reserve(**kwargs):
        reservation_entered.set()
        assert reservation_release.wait(timeout=5)
        return reserve(**kwargs)

    monkeypatch.setattr(runtime.store, "reserve_model_call", delayed_reserve)
    runtime.start()
    try:
        created = runtime.submit_task(
            _single_request().model_copy(update={"model_mode": "deepseek"})
        )
        assert reservation_entered.wait(timeout=5)
        assert runtime.pause_agent_run(
            created.run_id,
            RunPauseRequest(request_id="pause-before-reserve", reason="inspect"),
        ).status == "pausing"
        reservation_release.set()

        deadline = monotonic() + 5
        while runtime.snapshot(created.run_id)["run"]["status"] != "paused":
            assert monotonic() < deadline
            sleep(0.02)
        events = runtime.store.read_all(run_id=created.run_id)
        assert any(event.type == "model.completed" for event in events)
        assert any(event.type == "tool.completed" for event in events)
        assert not any(event.type == "agent.failed" for event in events)
    finally:
        reservation_release.set()
        runtime.stop()


def test_nudge_arriving_after_model_request_is_consumed_by_the_next_turn(tmp_path):
    class TwoTurnBlockingModel(FakeModelProvider):
        def __init__(self):
            super().__init__(
                [
                    json.dumps(
                        {
                            "type": "call_tool",
                            "reason": "inspect first",
                            "tool_name": "repo.read",
                            "tool_args": {"path": "calculator.py"},
                        }
                    ),
                    json.dumps({"type": "stop", "reason": "capture second prompt"}),
                ]
            )
            self.first_started = threading.Event()
            self.first_release = threading.Event()
            self.second_started = threading.Event()
            self.second_release = threading.Event()
            self.message_batches = []

        def complete(self, messages, *, tools=None, response_schema=None):
            self.message_batches.append(messages)
            if len(self.message_batches) == 1:
                self.first_started.set()
                assert self.first_release.wait(timeout=5)
            else:
                self.second_started.set()
                assert self.second_release.wait(timeout=5)
            return super().complete(
                messages,
                tools=tools,
                response_schema=response_schema,
            )

    model = TwoTurnBlockingModel()
    runtime = ResidentRuntime(tmp_path, model_factory=lambda _: model)
    runtime.start()
    try:
        created = runtime.submit_task(_single_request())
        assert model.first_started.wait(timeout=5)
        nudge = runtime.nudge_agent_run(
            created.run_id,
            RunNudgeRequest(
                request_id="nudge-between-model-and-tool",
                message="NEXT TURN ONLY: verify the just-observed evidence.",
            ),
        )
        model.first_release.set()
        assert model.second_started.wait(timeout=5)

        second_prompt = "\n".join(
            message.content for message in model.message_batches[1]
        )
        assert "NEXT TURN ONLY" in second_prompt
        requests = [
            event
            for event in runtime.store.read_all(run_id=created.run_id)
            if event.type == "model.requested"
        ]
        assert requests[0].payload["active_nudge_event_id"] is None
        assert requests[1].payload["active_nudge_event_id"] == nudge.nudge_event_id
    finally:
        model.first_release.set()
        model.second_release.set()
        runtime.stop()
