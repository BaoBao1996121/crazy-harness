from fastapi.testclient import TestClient
import pytest

from crazy_harness.control_plane.api import create_app


def test_health_reports_the_current_control_plane_behavior_version(tmp_path):
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["version"] == "v0.6.0-dev"


@pytest.mark.smoke
def test_http_snapshot_and_finite_sse_feed_share_the_same_event_cursor(tmp_path):
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"title": "HTTP demo", "brief": "Show the resident A2A path."},
        )
        assert created.status_code == 201
        run_id = created.json()["run_id"]

        assert client.post(f"/api/runs/{run_id}/drain").status_code == 200
        snapshot = client.get(f"/api/snapshot?run_id={run_id}").json()
        events = client.get(f"/api/events?run_id={run_id}&after=0").json()
        stream = client.get(f"/api/events/stream?run_id={run_id}&after=0&once=true")

        assert snapshot["run"]["status"] == "succeeded"
        assert events["next_cursor"] == events["items"][-1]["cursor"]
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert f"id: {events['items'][0]['cursor']}" in stream.text
        assert '"type":"run.created"' in stream.text


def test_fault_can_be_armed_through_control_api(tmp_path):
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        armed = client.post("/api/chaos/faults", json={"point": "after_candidate_persisted"})
        created = client.post("/api/runs", json={"title": "Chaos", "brief": "Recover once."}).json()
        client.post(f"/api/runs/{created['run_id']}/drain")
        events = client.get(f"/api/events?run_id={created['run_id']}&after=0").json()["items"]

        assert armed.status_code == 200
        assert any(item["event"]["type"] == "runtime.agent.crashed" for item in events)


def test_http_can_cancel_a_queued_run_idempotently(tmp_path):
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={"title": "Cancel queued", "brief": "Do not start this run."},
        ).json()
        run_id = created["run_id"]

        first = client.post(f"/api/runs/{run_id}/cancel")
        second = client.post(f"/api/runs/{run_id}/cancel")
        snapshot = client.get(f"/api/snapshot?run_id={run_id}").json()

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["status"] == "cancelled"
        assert second.json()["status"] == "cancelled"
        assert snapshot["run"]["status"] == "cancelled"
        assert snapshot["queued_deliveries"] == []


def test_http_can_start_and_drain_the_single_agent_repo_maintainer(tmp_path):
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        created = client.post(
            "/api/runs",
            json={
                "title": "Repair repository",
                "brief": "Repair the implementation and prove it.",
                "execution_mode": "single",
                "model_mode": "scripted",
                "task_pack": "repo-maintainer",
            },
        )
        assert created.status_code == 201
        run_id = created.json()["run_id"]

        drained = client.post(f"/api/runs/{run_id}/drain")
        snapshot = client.get(f"/api/snapshot?run_id={run_id}").json()

        assert drained.status_code == 200
        assert snapshot["run"]["status"] == "succeeded"
        assert snapshot["contexts"][-1]["agent_id"] == "generalist"


def test_http_rejects_live_deepseek_without_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={
                "title": "Live repair",
                "brief": "Use the live model.",
                "execution_mode": "single",
                "model_mode": "deepseek",
                "task_pack": "repo-maintainer",
            },
        )

    assert response.status_code == 400
    assert "DEEPSEEK_API_KEY" in response.json()["detail"]
