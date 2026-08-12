from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from crazy_harness.control_plane.api import create_app

request = run_path("tests/control_plane/test_engineering_loop_runtime.py")["_repo_quality_request"]
with TemporaryDirectory() as raw:
    app = create_app(Path(raw), background=False)
    first = app.state.runtime.create_engineering_loop(request())
    second = app.state.runtime.create_engineering_loop(request().model_copy(update={"request_id": "el3-sse-other"}))
    before = len(app.state.runtime.store.read_records())
    body = TestClient(app).get(f"/api/events/stream?run_id={first.loop_id}&once=true").text
    assert first.loop_id in body and second.loop_id not in body
    assert len(app.state.runtime.store.read_records()) == before
    print("PASS: parent SSE is scoped and side-effect free")
