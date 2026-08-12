from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.engineering_loops import EngineeringLoopService
from crazy_harness.control_plane.store import SQLiteEventStore

fixture = run_path("tests/control_plane/test_engineering_loop_service.py")
with TemporaryDirectory() as raw:
    service = EngineeringLoopService(SQLiteEventStore(Path(raw) / "events.db"))
    first = service.create(fixture["request"](request_id="el3-target-first"))
    second = service.create(fixture["request"](request_id="el3-target-second"))
    ports = fixture["DeterministicPorts"]()
    service.advance_one(first.loop_id, propose=ports.propose, launch_child=ports.launch, child_outcome=ports.observe, evaluate=ports.evaluate)
    assert len(service.report(first.loop_id).iterations) == 1
    assert service.report(second.loop_id).iterations == ()
    print("PASS: one parent advance touches only its target loop")
