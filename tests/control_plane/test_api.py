from fastapi.testclient import TestClient
import pytest

from crazy_harness.control_plane.api import create_app
from crazy_harness.control_plane.paired_evals import PairedEvalCreationRejected
from crazy_harness.control_plane.runtime import ResidentRuntime


def test_health_reports_the_current_control_plane_behavior_version(tmp_path):
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["version"] == "v0.8.0-dev"


def test_http_tells_client_when_a_failed_pair_requires_a_new_request_id(
    tmp_path,
    monkeypatch,
):
    def reject_creation(_runtime, _request):
        raise PairedEvalCreationRejected("pair preparation failed")

    monkeypatch.setattr(ResidentRuntime, "create_paired_eval", reject_creation)
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/evals/pairs",
            json={
                "request_id": "api-terminal-pair-1",
                "title": "Compare repair",
                "brief": "Repair the same fixture.",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "paired_eval_creation_rejected",
        "message": "pair preparation failed",
    }


@pytest.mark.smoke
def test_http_can_run_and_replay_a_single_vs_team_eval_pair(tmp_path):
    app = create_app(tmp_path, background=False)
    with TestClient(app) as client:
        created = client.post(
            "/api/evals/pairs",
            json={
                "request_id": "api-fair-pair-1",
                "title": "Compare clamp repair",
                "brief": "Repair clamp without changing tests.",
                "model_mode": "scripted",
            },
        )
        assert created.status_code == 201
        eval_id = created.json()["eval_id"]

        before_drain = client.get(f"/api/evals/pairs/{eval_id}")
        drained = client.post(f"/api/evals/pairs/{eval_id}/drain")
        replay = client.get(f"/api/evals/pairs/{eval_id}")
        listed = client.get("/api/evals/pairs")

    assert drained.status_code == 200
    assert before_drain.status_code == 200
    assert before_drain.json()["status"] == "running"
    assert replay.status_code == 200
    assert replay.json() == drained.json()
    assert replay.json()["status"] == "completed"
    assert replay.json()["single"]["score"]["passed"] is True
    assert replay.json()["team"]["score"]["passed"] is True
    assert replay.json()["recommendation"]["outcome"] == ("insufficient_live_evidence")
    assert [item["eval_id"] for item in listed.json()] == [eval_id]


def test_campaign_http_create_freezes_parent_and_get_remains_pure_read(tmp_path):
    app = create_app(tmp_path, background=False)
    runtime = app.state.runtime
    with TestClient(app) as client:
        created = client.post(
            "/api/evals/campaigns",
            json={
                "request_id": "api-campaign-create-1",
                "title": "Persistent paired campaign",
                "brief": "Prepare two trials without running them during GET.",
                "trial_count": 2,
                "max_parallel_pairs": 1,
            },
        )
        assert created.status_code == 201
        campaign_id = created.json()["campaign_id"]
        before = len(runtime.store.read_records())

        fetched = client.get(f"/api/evals/campaigns/{campaign_id}")
        listed = client.get("/api/evals/campaigns")
        missing = client.get("/api/evals/campaigns/campaign_missing")
        after = len(runtime.store.read_records())

    assert fetched.status_code == 200
    assert fetched.json()["status"] == "running"
    assert fetched.json()["completed_trial_count"] == 0
    assert [item["campaign_id"] for item in listed.json()] == [campaign_id]
    assert missing.status_code == 404
    assert after == before
    assert not any(
        event.type == "eval.pair.requested" for event in runtime.store.read_all()
    )


def test_http_can_cancel_a_campaign_idempotently_and_get_stays_pure(tmp_path):
    app = create_app(tmp_path, background=False)
    runtime = app.state.runtime
    with TestClient(app) as client:
        created = client.post(
            "/api/evals/campaigns",
            json={
                "request_id": "api-campaign-cancel-1",
                "title": "Cancel campaign",
                "brief": "Cancellation is a durable terminal fact.",
                "trial_count": 2,
            },
        )
        campaign_id = created.json()["campaign_id"]

        first = client.post(f"/api/evals/campaigns/{campaign_id}/cancel")
        second = client.post(f"/api/evals/campaigns/{campaign_id}/cancel")
        drained = client.post(f"/api/evals/campaigns/{campaign_id}/drain")
        after_cancel = len(runtime.store.read_records())
        fetched = client.get(f"/api/evals/campaigns/{campaign_id}")
        listed = client.get("/api/evals/campaigns")
        after_reads = len(runtime.store.read_records())
        missing = client.post("/api/evals/campaigns/campaign_missing/cancel")

    assert created.status_code == 201
    assert first.status_code == 200
    assert first.json() == second.json() == drained.json() == fetched.json()
    assert first.json()["status"] == "cancelled"
    assert [item["status"] for item in listed.json()] == ["cancelled"]
    assert after_reads == after_cancel
    assert missing.status_code == 404
    assert (
        sum(
            event.type == "eval.campaign.cancelled"
            for event in runtime.store.read_all(run_id=campaign_id)
        )
        == 1
    )
    assert not any(
        event.type
        in {
            "eval.campaign.trial.started",
            "eval.campaign.trial.released",
            "eval.pair.requested",
        }
        for event in runtime.store.read_all()
    )


def test_campaign_drain_advances_only_the_requested_campaign(tmp_path):
    app = create_app(tmp_path, background=False)
    runtime = app.state.runtime
    with TestClient(app) as client:
        campaign_ids = []
        for suffix in ("target", "other"):
            response = client.post(
                "/api/evals/campaigns",
                json={
                    "request_id": f"api-scoped-campaign-{suffix}",
                    "title": f"Campaign {suffix}",
                    "brief": "Only the explicitly drained campaign may advance.",
                    "trial_count": 1,
                },
            )
            campaign_ids.append(response.json()["campaign_id"])
        ordinary = client.post(
            "/api/runs",
            json={
                "title": "Unrelated resident task",
                "brief": "This task must remain queued during Campaign drain.",
                "model_mode": "scripted",
                "execution_mode": "team",
                "task_pack": "resident-demo",
            },
        ).json()

        target = client.post(f"/api/evals/campaigns/{campaign_ids[0]}/drain")
        other = client.get(f"/api/evals/campaigns/{campaign_ids[1]}")

    assert target.status_code == 200
    assert target.json()["status"] == "completed"
    assert other.json()["status"] == "running"
    assert other.json()["completed_trial_count"] == 0
    assert not any(
        event.type == "eval.campaign.trial.started"
        for event in runtime.store.read_all(run_id=campaign_ids[1])
    )
    assert not any(
        event.type == "model.completed"
        for event in runtime.store.read_all(run_id=ordinary["run_id"])
    )


def test_openapi_exposes_campaigns_but_not_internal_pair_release_controls(tmp_path):
    schema = create_app(tmp_path, background=False).openapi()

    for path in (
        "/api/evals/campaigns",
        "/api/evals/campaigns/{campaign_id}",
        "/api/evals/campaigns/{campaign_id}/drain",
        "/api/evals/campaigns/{campaign_id}/cancel",
    ):
        assert path in schema["paths"]
    campaign_operations = {
        ("/api/evals/campaigns", "post"): {"400", "409", "422"},
        ("/api/evals/campaigns/{campaign_id}", "get"): {"404", "422"},
        ("/api/evals/campaigns/{campaign_id}/drain", "post"): {"404", "422"},
        ("/api/evals/campaigns/{campaign_id}/cancel", "post"): {
            "404",
            "409",
            "422",
        },
    }
    for (path, method), expected in campaign_operations.items():
        responses = schema["paths"][path][method]["responses"]
        assert expected.issubset(responses)
        for status_code in expected - {"422"}:
            error_schema = responses[status_code]["content"]["application/json"][
                "schema"
            ]
            assert error_schema["$ref"].endswith("/ApiErrorResponse")
    pair_post = schema["paths"]["/api/evals/pairs"]["post"]
    body_schema = pair_post["requestBody"]["content"]["application/json"]["schema"]
    ref_name = body_schema["$ref"].rsplit("/", 1)[-1]
    pair_properties = schema["components"]["schemas"][ref_name]["properties"]
    assert "release_policy" not in pair_properties
    assert "parent_campaign_id" not in pair_properties
    assert "parent_trial_index" not in pair_properties


def test_campaign_http_errors_match_the_structured_openapi_contract(
    tmp_path,
    monkeypatch,
):
    app = create_app(tmp_path, background=False)
    runtime = app.state.runtime

    with TestClient(app) as client:
        missing = client.get("/api/evals/campaigns/campaign_missing")

        monkeypatch.setattr(
            runtime,
            "create_eval_campaign",
            lambda _request: (_ for _ in ()).throw(ValueError("invalid envelope")),
        )
        invalid = client.post(
            "/api/evals/campaigns",
            json={
                "request_id": "api-campaign-invalid-contract-1",
                "title": "Invalid campaign",
                "brief": "Exercise the structured error contract.",
            },
        )

        monkeypatch.setattr(
            runtime,
            "create_eval_campaign",
            lambda _request: (_ for _ in ()).throw(TimeoutError("claim busy")),
        )
        conflict = client.post(
            "/api/evals/campaigns",
            json={
                "request_id": "api-campaign-conflict-contract-1",
                "title": "Conflicting campaign",
                "brief": "Exercise the retryable error contract.",
            },
        )

    assert missing.status_code == 404
    assert missing.json()["detail"] == {
        "code": "eval_campaign_not_found",
        "message": "campaign not found",
        "retryable": False,
    }
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == {
        "code": "eval_campaign_invalid_request",
        "message": "invalid envelope",
        "retryable": False,
    }
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "eval_campaign_creation_in_progress",
        "message": "claim busy",
        "retryable": True,
    }


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
        armed = client.post(
            "/api/chaos/faults", json={"point": "after_candidate_persisted"}
        )
        created = client.post(
            "/api/runs", json={"title": "Chaos", "brief": "Recover once."}
        ).json()
        client.post(f"/api/runs/{created['run_id']}/drain")
        events = client.get(f"/api/events?run_id={created['run_id']}&after=0").json()[
            "items"
        ]

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
