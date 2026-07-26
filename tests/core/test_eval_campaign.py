from decimal import Decimal
from pathlib import Path

import pytest

from crazy_harness.core.evals import (
    EvidenceTier,
    PairedEvalArm,
    PairedEvalContract,
    RecommendationOutcome,
)
from crazy_harness.core.evals.campaign import (
    CampaignAggregator,
    CampaignBudgetEnvelope,
    CampaignRecommendationEvidence,
    CampaignRecommendationPolicy,
    CampaignTrialPlan,
    CampaignScope,
    EvalCampaignContract,
    PairedTrialSample,
)


def test_campaign_budget_envelope_covers_every_pre_registered_arm() -> None:
    envelope = CampaignBudgetEnvelope(
        trial_count=5,
        per_arm_max_tokens=250_000,
        per_arm_max_cost_usd=Decimal("0.10"),
        max_total_tokens=2_500_000,
        max_total_cost_usd=Decimal("1.00"),
    )

    assert envelope.required_total_tokens == 2_500_000
    assert envelope.required_total_cost_usd == Decimal("1.00")
    with pytest.raises(ValueError, match="campaign token cap"):
        CampaignBudgetEnvelope(
            trial_count=6,
            per_arm_max_tokens=250_000,
            per_arm_max_cost_usd=Decimal("0.10"),
            max_total_tokens=2_500_000,
            max_total_cost_usd=Decimal("1.20"),
        )


def test_campaign_contract_freezes_an_ordered_unique_trial_plan() -> None:
    budget = CampaignBudgetEnvelope(
        trial_count=3,
        per_arm_max_tokens=100,
        per_arm_max_cost_usd=Decimal("0.01"),
        max_total_tokens=600,
        max_total_cost_usd=Decimal("0.06"),
    )
    trials = tuple(
        CampaignTrialPlan(
            trial_index=index,
            pair_request_id=f"request-{index}",
            eval_id=f"eval-{index}",
        )
        for index in range(1, 4)
    )

    contract = EvalCampaignContract(
        campaign_id="campaign-demo",
        request_fingerprint="request-sha256",
        title="Compare Single and Team",
        brief="Repair the same fixture repeatedly.",
        task_pack="repo-maintainer",
        model_mode="scripted",
        evidence_tier=EvidenceTier.DETERMINISTIC,
        planned_trial_count=3,
        trials=trials,
        pair_model_budget={
            "max_total_tokens": 100,
            "max_cost_usd": "0.01",
            "max_concurrent_calls": 1,
            "max_output_tokens_per_call": 64,
            "max_retries_per_call": 0,
        },
        budget=budget,
        max_parallel_pairs=2,
    )

    assert [trial.trial_index for trial in contract.trials] == [1, 2, 3]
    with pytest.raises(ValueError, match="unique eval_id"):
        EvalCampaignContract(
            **contract.model_dump(exclude={"trials"}),
            trials=(
                trials[0],
                trials[1],
                trials[1].model_copy(
                    update={"trial_index": 3, "pair_request_id": "request-3-copy"}
                ),
            ),
        )


def _pair_contract(
    root: Path,
    *,
    eval_id: str,
    harness_profile: dict[str, object],
) -> PairedEvalContract:
    shared = {
        "input_hash": "input-sha256",
        "model_profile": {"provider": "DeepSeek", "model": "deepseek-v4-flash"},
        "model_budget": {"max_total_tokens": 100},
    }
    return PairedEvalContract(
        eval_id=eval_id,
        case_id="clamp-bounds-v1",
        task_pack="repo-maintainer",
        fixture_hash="fixture-sha256",
        scorer_version="repo-maintainer-v2",
        evidence_tier=EvidenceTier.LIVE_PAIRED,
        harness_profile=harness_profile,
        single=PairedEvalArm(
            execution_mode="single",
            run_id=f"run-{eval_id}-single",
            workspace=root / eval_id / "single",
            **shared,
        ),
        team=PairedEvalArm(
            execution_mode="team",
            run_id=f"run-{eval_id}-team",
            workspace=root / eval_id / "team",
            **shared,
        ),
    )


def test_campaign_scope_ignores_run_identity_but_detects_harness_drift(
    tmp_path: Path,
) -> None:
    profile = {
        "single_behavior_version": "v0.8.0-dev",
        "team_behavior_version": "v0.8.0-dev",
        "supervisor_policy": "SupervisorPolicy",
    }
    first = CampaignScope.from_pair_contract(
        _pair_contract(tmp_path, eval_id="eval-one", harness_profile=profile)
    )
    replay = CampaignScope.from_pair_contract(
        _pair_contract(tmp_path, eval_id="eval-two", harness_profile=profile)
    )
    drifted = CampaignScope.from_pair_contract(
        _pair_contract(
            tmp_path,
            eval_id="eval-three",
            harness_profile={**profile, "supervisor_policy": "ChangedPolicy"},
        )
    )

    assert first.fingerprint == replay.fingerprint
    assert first.fingerprint != drifted.fingerprint


def _sample(
    index: int,
    *,
    single_success: bool,
    team_success: bool,
    single_quality_ppm: int,
    team_quality_ppm: int,
    single_cost: int,
    team_cost: int,
    single_duration: int,
    team_duration: int,
) -> PairedTrialSample:
    return PairedTrialSample(
        trial_index=index,
        eval_id=f"eval-{index}",
        pair_report_sha256=f"report-{index}",
        single_run_id=f"single-{index}",
        team_run_id=f"team-{index}",
        single_terminal_event_id=f"single-terminal-{index}",
        team_terminal_event_id=f"team-terminal-{index}",
        single_success=single_success,
        team_success=team_success,
        single_quality_ppm=single_quality_ppm,
        team_quality_ppm=team_quality_ppm,
        single_committed_cost_microusd=single_cost,
        team_committed_cost_microusd=team_cost,
        single_duration_ms=single_duration,
        team_duration_ms=team_duration,
    )


def test_campaign_aggregator_is_replay_stable_and_uses_ratio_of_sums() -> None:
    samples = (
        _sample(
            1,
            single_success=False,
            team_success=True,
            single_quality_ppm=600_000,
            team_quality_ppm=800_000,
            single_cost=100,
            team_cost=200,
            single_duration=100,
            team_duration=150,
        ),
        _sample(
            2,
            single_success=True,
            team_success=True,
            single_quality_ppm=800_000,
            team_quality_ppm=900_000,
            single_cost=300,
            team_cost=300,
            single_duration=300,
            team_duration=450,
        ),
    )
    policy = CampaignRecommendationPolicy(
        minimum_interval_trials=2,
        minimum_live_trials=2,
        bootstrap_resamples=100,
    )

    first = CampaignAggregator().aggregate(
        samples=samples,
        policy=policy,
        seed_material="campaign-scope",
    )
    replay = CampaignAggregator().aggregate(
        samples=tuple(reversed(samples)),
        policy=policy,
        seed_material="campaign-scope",
    )

    assert first == replay
    assert first.success_rate_delta.point_ppm == 500_000
    assert first.quality_delta.point_ppm == 150_000
    assert first.cost_ratio.point_ppm == 1_250_000
    assert first.duration_ratio.point_ppm == 1_500_000
    assert first.quality_delta.lower_bound_ppm is not None


def test_scripted_campaign_can_veto_but_never_promote_team() -> None:
    samples = tuple(
        _sample(
            index,
            single_success=True,
            team_success=True,
            single_quality_ppm=700_000,
            team_quality_ppm=800_000,
            single_cost=100,
            team_cost=110,
            single_duration=100,
            team_duration=110,
        )
        for index in range(1, 3)
    )
    policy = CampaignRecommendationPolicy(
        minimum_interval_trials=2,
        minimum_live_trials=2,
        bootstrap_resamples=100,
    )
    aggregate = CampaignAggregator().aggregate(
        samples=samples,
        policy=policy,
        seed_material="scripted-campaign",
    )

    passing = policy.decide(
        CampaignRecommendationEvidence(
            evidence_tier=EvidenceTier.DETERMINISTIC,
            planned_trial_count=2,
            completed_trial_count=2,
            invalid_trial_count=0,
            scope_valid=True,
            aggregate=aggregate,
        )
    )
    regressed = policy.decide(
        CampaignRecommendationEvidence(
            evidence_tier=EvidenceTier.DETERMINISTIC,
            planned_trial_count=2,
            completed_trial_count=2,
            invalid_trial_count=0,
            scope_valid=True,
            aggregate=aggregate.model_copy(update={"hard_reliability_regression": True}),
        )
    )

    assert passing.outcome is RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE
    assert regressed.outcome is RecommendationOutcome.KEEP_SINGLE
