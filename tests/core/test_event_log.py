from crazy_harness.core.events import Event, EventLog


def test_event_log_roundtrip(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    event = Event(run_id="r1", task_id="t1", type="seed", source="test")

    log.append(event)

    assert log.read_all()[0].id == event.id
