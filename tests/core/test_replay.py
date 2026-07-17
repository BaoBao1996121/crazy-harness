from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.replay.replay import ReplayMode, replay_events


def test_dry_replay_classifies_trace_and_blocks_effects(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append(Event(run_id="r1", task_id="t1", type="model.completed", source="model"))
    log.append(Event(run_id="r1", task_id="t1", type="agent.command.validated", source="agent"))
    log.append(Event(run_id="r1", task_id="t1", type="operation.started", source="agent"))
    calls = []

    report = replay_events(path, mode=ReplayMode.DRY, side_effect_executor=lambda event: calls.append(event))

    assert report.event_count == 3
    assert report.provider_events == 1
    assert report.command_events == 1
    assert report.execution_events == 1
    assert report.blocked_side_effects == 1
    assert calls == []


def test_effectful_replay_requires_explicit_mode(tmp_path):
    path = tmp_path / "events.jsonl"
    EventLog(path).append(Event(run_id="r1", task_id="t1", type="operation.started", source="agent"))
    calls = []

    replay_events(path, mode=ReplayMode.EXECUTE_EFFECTS, side_effect_executor=lambda event: calls.append(event.type))

    assert calls == ["operation.started"]
