import pytest

from crazy_harness.control_plane.eval_campaigns import EvalCampaignRequest
from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.core.events import Event
from crazy_harness.core.evals import RecommendationOutcome


def test_runtime_runs_and_replays_a_scripted_eval_campaign(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.create_eval_campaign(
        EvalCampaignRequest(
            request_id="runtime-campaign-1",
            title="One persistent campaign trial",
            brief="Repair the same clamp fixture without changing tests.",
            model_mode="scripted",
            trial_count=1,
            max_parallel_pairs=1,
        )
    )

    runtime.run_until_idle(max_steps=600)
    report = runtime.eval_campaign(created.campaign_id)

    assert report.status == "completed"
    assert report.completed_trial_count == 1
    assert report.invalid_trial_count == 0
    assert report.aggregate is not None
    assert report.recommendation is not None
    assert report.recommendation.outcome is RecommendationOutcome.KEEP_SINGLE
    assert "scripted_duration_regression" in (
        report.recommendation.failed_thresholds
    )

    trial = report.trials[0]
    assert trial.sample is not None
    records = runtime.store.read_records()
    link_cursor = next(
        record.cursor
        for record in records
        if record.event.run_id == created.campaign_id
        and record.event.type == "eval.campaign.trial.linked"
    )
    child_run_ids = {
        trial.sample.single_run_id,
        trial.sample.team_run_id,
    }
    first_delivery_by_run = {}
    for record in records:
        if (
            record.event.run_id in child_run_ids
            and record.event.type == "mailbox.delivery.sent"
        ):
            first_delivery_by_run.setdefault(record.event.run_id, record.cursor)
    assert set(first_delivery_by_run) == child_run_ids
    assert all(cursor > link_cursor for cursor in first_delivery_by_run.values())
    arm_releases = [
        record
        for record in records
        if record.event.run_id == trial.eval_id
        and record.event.type == "eval.arm.released"
    ]
    assert len(arm_releases) == 2
    assert all(record.cursor > link_cursor for record in arm_releases)

    replay = ResidentRuntime(tmp_path).eval_campaign(created.campaign_id)
    assert replay == report
    assert sum(
        event.type == "eval.campaign.completed" for event in runtime.store.read_all()
    ) == 1


def test_cancelling_a_released_campaign_cancels_both_child_runs_idempotently(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.create_eval_campaign(
        EvalCampaignRequest(
            request_id="runtime-campaign-cancel-released",
            title="Cancel released campaign",
            brief="Cancel both prepared arms before workers consume their deliveries.",
            model_mode="scripted",
            trial_count=1,
            max_parallel_pairs=1,
        )
    )

    # start -> create/link Pair -> release both arms; do not dispatch workers yet.
    assert runtime._advance_eval_control() is True
    assert runtime._advance_eval_control() is True
    assert runtime._advance_eval_control() is True
    pair = runtime.eval_service.contract(
        runtime.campaign_service.contract(created.campaign_id).trials[0].eval_id
    )

    first = runtime.cancel_eval_campaign(created.campaign_id)
    second = runtime.cancel_eval_campaign(created.campaign_id)

    assert first == second
    assert first.status == "cancelled"
    assert runtime.store.projection("run", pair.single.run_id)["status"] == "cancelled"
    assert runtime.store.projection("run", pair.team.run_id)["status"] == "cancelled"
    assert sum(
        event.type == "eval.campaign.cancelled"
        for event in runtime.store.read_all(run_id=created.campaign_id)
    ) == 1


def test_deepseek_campaign_replay_does_not_require_the_current_api_key(
    tmp_path,
    monkeypatch,
):
    runtime = ResidentRuntime(tmp_path)
    request = EvalCampaignRequest(
        request_id="deepseek-campaign-replay",
        title="Replay a paid campaign identity",
        brief="The persisted request is authoritative after creation.",
        model_mode="deepseek",
        trial_count=1,
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    first = runtime.create_eval_campaign(request)
    monkeypatch.delenv("DEEPSEEK_API_KEY")

    assert runtime.create_eval_campaign(request) == first
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        runtime.create_eval_campaign(
            request.model_copy(update={"request_id": "new-deepseek-campaign"})
        )


def test_scoped_campaign_drain_reconciles_child_failure_before_dispatch(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.create_eval_campaign(
        EvalCampaignRequest(
            request_id="runtime-campaign-failure-barrier",
            title="Respect a child failure request",
            brief="Do not dispatch work after the durable failure request.",
            trial_count=1,
        )
    )
    assert runtime._advance_eval_control() is True
    assert runtime._advance_eval_control() is True
    assert runtime._advance_eval_control() is True
    trial = runtime.campaign_service.contract(created.campaign_id).trials[0]
    pair = runtime.eval_service.contract(trial.eval_id)
    single_created = next(
        event
        for event in runtime.store.read_all(run_id=pair.single.run_id)
        if event.type == "run.created"
    )
    runtime.store.append(
        Event(
            run_id=single_created.run_id,
            task_id=single_created.task_id,
            type="run.failure.requested",
            source="test",
            payload={"reason": "injected terminal failure"},
        )
    )
    unrelated = runtime.submit_task(
        TaskRequest(
            title="Unrelated queued task",
            brief="This Run must remain queued during scoped drain.",
            execution_mode="single",
            task_pack="repo-maintainer",
        )
    )

    runtime.run_eval_campaign_until_idle(created.campaign_id)

    assert runtime.store.projection("run", pair.single.run_id)["status"] == "failed"
    assert not any(
        event.type == "model.requested"
        for event in runtime.store.read_all(run_id=pair.single.run_id)
    )
    assert not any(
        event.type == "model.requested"
        for event in runtime.store.read_all(run_id=unrelated.run_id)
    )


def test_campaign_cancel_cleans_an_arm_prepared_before_pair_contract(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.create_eval_campaign(
        EvalCampaignRequest(
            request_id="runtime-campaign-cancel-orphan-arm",
            title="Cancel a half-prepared Pair",
            brief="The parent owns every prepared child Run.",
            trial_count=1,
        )
    )
    assert runtime._advance_eval_control() is True

    def interrupt_after_single(point: str) -> None:
        if point == "after_eval_arm_prepared:single":
            raise KeyboardInterrupt("simulated Pair preparation crash")

    runtime.eval_service.fault_injector = interrupt_after_single
    with pytest.raises(KeyboardInterrupt, match="preparation crash"):
        runtime._advance_eval_control()
    trial = runtime.campaign_service.contract(created.campaign_id).trials[0]
    arm = next(
        event
        for event in runtime.store.read_all(run_id=trial.eval_id)
        if event.type == "eval.arm.created"
    )

    report = runtime.cancel_eval_campaign(created.campaign_id)

    assert report.status == "cancelled"
    assert runtime.store.projection("run", arm.payload["run_id"])["status"] == (
        "cancelled"
    )


def test_campaign_retries_a_transient_runtime_pair_prepare_failure(
    tmp_path,
    monkeypatch,
):
    runtime = ResidentRuntime(tmp_path)
    original_prepare = runtime._prepare_single_task
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("workspace temporarily unavailable")
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(runtime, "_prepare_single_task", fail_once)
    created = runtime.create_eval_campaign(
        EvalCampaignRequest(
            request_id="runtime-campaign-transient-prepare",
            title="Retry temporary Pair preparation",
            brief="Keep the deterministic child identity across retry.",
            trial_count=1,
        )
    )

    runtime.run_until_idle(max_steps=600)
    report = runtime.eval_campaign(created.campaign_id)
    trial = report.trials[0]

    assert report.status == "completed"
    assert report.invalid_trial_count == 0
    assert trial.status == "observed"
    assert any(
        event.type == "eval.campaign.trial.creation_retryable_failed"
        for event in runtime.store.read_all(run_id=created.campaign_id)
    )
    assert not any(
        event.type == "eval.pair.failed"
        for event in runtime.store.read_all(run_id=trial.eval_id)
    )
