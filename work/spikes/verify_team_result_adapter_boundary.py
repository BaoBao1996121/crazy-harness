from pathlib import Path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest


with TemporaryDirectory() as directory:
    runtime = ResidentRuntime(Path(directory))
    created = runtime.submit_task(
        TaskRequest(title="spike", brief="verify adapter boundary")
    )
    runtime.run_until_idle(max_steps=160)

    events = runtime.store.read_all(run_id=created.run_id)
    assignment = next(
        event
        for event in events
        if event.type == "assignment.created"
        and event.payload.get("stage_id") == "evidence"
    )
    child_task_id = f"{assignment.payload['assignment_id']}:agent-run"
    submission = next(
        event
        for event in events
        if event.task_id == child_task_id and event.type == "agent.submitted"
    )
    promotion = next(
        event
        for event in events
        if event.type == "agent.result.promoted"
        and event.payload.get("assignment_id")
        == assignment.payload["assignment_id"]
    )
    formal = next(
        event
        for event in events
        if event.type == "evidence.recorded"
        and event.payload.get("submission_event_id") == submission.id
    )

    assert promotion.causation_id == submission.id
    assert promotion.payload["accepted"] is True
    assert formal.payload["agent_run_id"] == child_task_id
    assert (
        runtime.store.projection("lease", assignment.payload["assignment_id"])[
            "status"
        ]
        == "released"
    )
    print("child result adapter boundary: ok")
