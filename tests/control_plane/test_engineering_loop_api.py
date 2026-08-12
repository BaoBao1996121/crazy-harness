from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from crazy_harness.control_plane.api import create_app


def _request(request_id: str, *, objective: str = "Repair then improve") -> dict[str, object]:
    return {
        "request_id": request_id,
        "title": "Repository quality climb",
        "objective": objective,
        "exit_criteria": ["quality_score reaches 1"],
        "loop_pack": "repo-quality",
        "model_mode": "scripted",
        "budget": {"max_iterations": 3, "max_no_progress_iterations": 2},
    }


def test_http_create_get_and_list_are_idempotent_and_side_effect_free(tmp_path) -> None:
    app = create_app(tmp_path, background=False)
    runtime = app.state.runtime
    with TestClient(app) as client:
        first = client.post("/api/engineering-loops", json=_request("el3-api-create-1"))
        repeated = client.post("/api/engineering-loops", json=_request("el3-api-create-1"))
        loop_id = first.json()["loop_id"]
        before = len(runtime.store.read_records())
        fetched = client.get(f"/api/engineering-loops/{loop_id}")
        listed = client.get("/api/engineering-loops")
        missing = client.get("/api/engineering-loops/loop_missing")
        after = len(runtime.store.read_records())

    assert first.status_code == repeated.status_code == 201
    assert first.json() == repeated.json()
    assert fetched.status_code == listed.status_code == 200
    assert fetched.json()["status"] == "running"
    assert listed.json() == [fetched.json()]
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "engineering_loop_not_found"
    assert after == before


def test_http_create_rejects_request_id_payload_drift_with_structured_conflict(tmp_path) -> None:
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        first = client.post("/api/engineering-loops", json=_request("el3-api-conflict-1"))
        conflict = client.post(
            "/api/engineering-loops",
            json=_request("el3-api-conflict-1", objective="Different objective"),
        )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "engineering_loop_idempotency_conflict",
        "message": "engineering loop request key was reused with different input",
        "retryable": False,
    }


def test_http_advance_commits_one_parent_phase_for_only_the_target_loop(tmp_path) -> None:
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        target = client.post(
            "/api/engineering-loops", json=_request("el3-api-target-1")
        ).json()["loop_id"]
        other = client.post(
            "/api/engineering-loops", json=_request("el3-api-other-1")
        ).json()["loop_id"]
        advanced = client.post(f"/api/engineering-loops/{target}/advance")
        other_report = client.get(f"/api/engineering-loops/{other}").json()

    assert advanced.status_code == 200
    assert advanced.json()["advanced"] is True
    assert advanced.json()["report"]["iterations"][0]["status"] == "planned"
    assert other_report["iterations"] == []


def test_http_scoped_drain_completes_target_without_advancing_peer_loop(tmp_path) -> None:
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        target = client.post(
            "/api/engineering-loops", json=_request("el3-api-drain-target")
        ).json()["loop_id"]
        other = client.post(
            "/api/engineering-loops", json=_request("el3-api-drain-other")
        ).json()["loop_id"]
        drained = client.post(f"/api/engineering-loops/{target}/drain")
        other_report = client.get(f"/api/engineering-loops/{other}").json()

    assert drained.status_code == 200
    assert drained.json()["steps"] > 0
    assert drained.json()["report"]["status"] == "completed"
    assert [
        Decimal(item["evaluation"]["metrics"]["quality_score"])
        for item in drained.json()["report"]["iterations"]
    ] == [Decimal("0.5"), Decimal("1")]
    assert other_report["status"] == "running"
    assert other_report["iterations"] == []


def test_http_cancel_is_durable_idempotent_and_visible_in_parent_sse(tmp_path) -> None:
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        loop_id = client.post(
            "/api/engineering-loops", json=_request("el3-api-cancel-1")
        ).json()["loop_id"]
        body = {"request_id": "el3-cancel-command-1", "reason": "operator stop"}
        first = client.post(f"/api/engineering-loops/{loop_id}/cancel", json=body)
        repeated = client.post(f"/api/engineering-loops/{loop_id}/cancel", json=body)
        stream = client.get(
            f"/api/events/stream?run_id={loop_id}&after=0&once=true"
        )

    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json()
    assert first.json()["status"] == "cancelled"
    assert '"type":"engineering.loop.cancellation.requested"' in stream.text
    assert '"type":"engineering.loop.cancelled"' in stream.text


def test_http_pause_resume_survives_restart_and_stale_resume_cannot_win(tmp_path) -> None:
    first_app = create_app(tmp_path, background=False)
    with TestClient(first_app) as client:
        loop_id = client.post(
            "/api/engineering-loops", json=_request("el3-api-pause-1")
        ).json()["loop_id"]
        paused = client.post(
            f"/api/engineering-loops/{loop_id}/pause",
            json={"request_id": "el3-pause-command-1", "reason": "inspect evidence"},
        )
        refused_advance = client.post(f"/api/engineering-loops/{loop_id}/advance")

    second_app = create_app(tmp_path, background=False)
    with TestClient(second_app) as client:
        restarted = client.get(f"/api/engineering-loops/{loop_id}")
        resumed = client.post(
            f"/api/engineering-loops/{loop_id}/resume",
            json={"request_id": "el3-resume-command-1", "reason": "reviewed"},
        )
        advanced = client.post(f"/api/engineering-loops/{loop_id}/advance")
        paused_again = client.post(
            f"/api/engineering-loops/{loop_id}/pause",
            json={"request_id": "el3-pause-command-2", "reason": "newer stop"},
        )
        stale_resume = client.post(
            f"/api/engineering-loops/{loop_id}/resume",
            json={"request_id": "el3-resume-command-1", "reason": "reviewed"},
        )

    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    assert refused_advance.json()["advanced"] is False
    assert restarted.json()["status"] == "paused"
    assert resumed.json()["status"] == "running"
    assert advanced.json()["report"]["iterations"][0]["status"] == "planned"
    assert paused_again.json()["status"] == "paused"
    assert stale_resume.json()["status"] == "paused"


def test_openapi_publishes_parent_loop_surface_without_trusted_contract_fields(tmp_path) -> None:
    schema = create_app(tmp_path, background=False).openapi()
    for path in (
        "/api/engineering-loops",
        "/api/engineering-loops/{loop_id}",
        "/api/engineering-loops/{loop_id}/advance",
        "/api/engineering-loops/{loop_id}/drain",
        "/api/engineering-loops/{loop_id}/cancel",
        "/api/engineering-loops/{loop_id}/pause",
        "/api/engineering-loops/{loop_id}/resume",
    ):
        assert path in schema["paths"]
    public_schema = schema["components"]["schemas"]["EngineeringLoopCreateRequest"]
    properties = public_schema["properties"]
    assert "permissions" not in properties
    assert "metric" not in properties
    assert "worker" not in properties
    assert "initial_state_ref" not in properties
