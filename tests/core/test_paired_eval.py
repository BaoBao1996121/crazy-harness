from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from crazy_harness.core.evals import (
    EvidenceTier,
    PairedEvalContract,
    RecommendationOutcome,
    RunTraceAggregator,
    TeamRecommendationEvidence,
    TeamRecommendationPolicy,
)
from crazy_harness.core.events import Event


MODEL_PROFILE = {
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "temperature": 0.0,
}
MODEL_BUDGET = {
    "max_total_tokens": 10_000,
    "max_cost_usd": "0.10",
    "max_concurrent_calls": 2,
    "max_output_tokens_per_call": 1_024,
    "max_retries_per_call": 2,
}


def _arm(mode: str, run_id: str, workspace: str) -> dict[str, object]:
    return {
        "execution_mode": mode,
        "run_id": run_id,
        "workspace": workspace,
        "input_hash": "a" * 64,
        "model_profile": deepcopy(MODEL_PROFILE),
        "model_budget": deepcopy(MODEL_BUDGET),
    }


def _contract_data() -> dict[str, object]:
    return {
        "eval_id": "eval-1",
        "case_id": "repo-bug-1",
        "task_pack": "repo-maintainer",
        "fixture_hash": "f" * 64,
        "scorer_version": "repo-scorer-v1",
        "evidence_tier": "live_paired",
        "single": _arm("single", "run-single", "runs/eval-1/single"),
        "team": _arm("team", "run-team", "runs/eval-1/team"),
    }


def test_paired_contract_accepts_only_mechanically_fair_arms() -> None:
    contract = PairedEvalContract.model_validate(_contract_data())

    assert contract.single.execution_mode == "single"
    assert contract.team.execution_mode == "team"
    assert contract.single.model_budget == contract.team.model_budget


def test_paired_contract_rejects_two_path_aliases_for_the_same_workspace(
    tmp_path,
) -> None:
    data = _contract_data()
    data["single"]["workspace"] = str(tmp_path / "workspace")
    data["team"]["workspace"] = str(tmp_path / "nested" / ".." / "workspace")

    with pytest.raises(ValidationError, match="different workspaces"):
        PairedEvalContract.model_validate(data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("single", "execution_mode"), "team"),
        (("team", "execution_mode"), "single"),
        (("team", "run_id"), "run-single"),
        (("team", "workspace"), "runs/eval-1/single"),
        (("team", "input_hash"), "b" * 64),
        (("team", "model_profile"), {"provider": "other", "model": "x"}),
        (("team", "model_budget"), {**MODEL_BUDGET, "max_total_tokens": 9_999}),
    ],
)
def test_paired_contract_rejects_unfair_arms(
    path: tuple[str, str], value: object
) -> None:
    data = _contract_data()
    arm = data[path[0]]
    assert isinstance(arm, dict)
    arm[path[1]] = value

    with pytest.raises(ValidationError):
        PairedEvalContract.model_validate(data)


def _event(
    event_type: str,
    milliseconds: int,
    *,
    run_id: str = "run-1",
    payload: dict[str, object] | None = None,
) -> Event:
    return Event(
        id=f"event-{event_type}-{milliseconds}",
        run_id=run_id,
        task_id="task-1",
        type=event_type,
        source="test",
        payload=payload or {},
        created_at=datetime(2026, 7, 19, tzinfo=timezone.utc)
        + timedelta(milliseconds=milliseconds),
    )


def _trace() -> list[Event]:
    return [
        _event("run.created", 0),
        _event("model.requested", 10),
        _event("model.call.attempt.started", 20),
        _event("model.call.retry.scheduled", 30),
        _event("model.call.attempt.started", 40),
        _event("model.completed", 50),
        _event("operation.started", 60),
        _event("tool.requested", 70),
        _event("tool.completed", 80),
        _event("operation.completed", 90),
        _event("assignment.created", 100, payload={"attempt": 1}),
        _event("assignment.failed", 110),
        _event("assignment.created", 120, payload={"attempt": 2}),
        _event("a2a.peer.requested", 130),
        _event("a2a.peer.responded", 140),
        _event("operation.unknown", 150),
        _event("model.call.failed", 160, payload={"state": "unknown"}),
        _event("mailbox.delivery.dead_lettered", 170),
        _event("run.succeeded", 250),
    ]


def _budget_status(run_id: str = "run-1") -> dict[str, object]:
    return {
        "run_id": run_id,
        "spent_tokens": 80,
        "committed_tokens": 120,
        "estimated_spent_microusd": 12,
        "committed_cost_microusd": 20,
        "unknown_calls": 1,
    }


def test_trace_aggregation_uses_durable_facts_and_is_replay_stable() -> None:
    aggregator = RunTraceAggregator()

    first = aggregator.aggregate(events=_trace(), model_budget_status=_budget_status())
    replayed_events = [Event.model_validate_json(item.model_dump_json()) for item in _trace()]
    replay = aggregator.aggregate(
        events=list(reversed(replayed_events)),
        model_budget_status=deepcopy(_budget_status()),
    )

    assert first == replay
    assert first.run_id == "run-1"
    assert first.terminal_status == "succeeded"
    assert first.duration_ms == 250
    assert first.model_requests == 1
    assert first.model_completions == 1
    assert first.physical_model_attempts == 2
    assert first.tool_requests == first.tool_completions == 1
    assert first.operations_started == first.operations_completed == 1
    assert first.a2a_requests == first.a2a_responses == 1
    assert first.assignment_failures == first.assignment_retries == 1
    assert first.operation_unknowns == first.model_unknown_calls == 1
    assert first.dead_letters == 1
    assert first.spent_tokens == 80
    assert first.committed_tokens == 120
    assert first.spent_cost_microusd == 12
    assert first.committed_cost_microusd == 20


@pytest.mark.parametrize(
    "events",
    [
        [_event("run.succeeded", 10)],
        [_event("run.created", 0), _event("tool.completed", 10)],
        [
            _event("run.created", 0),
            _event("run.succeeded", 10),
            _event("run.failed", 20),
        ],
        [
            _event("run.created", 0),
            _event("tool.completed", 5, run_id="run-other"),
            _event("run.succeeded", 10),
        ],
    ],
    ids=["missing_created", "missing_terminal", "multiple_terminals", "mixed_runs"],
)
def test_trace_aggregation_fails_closed_for_untrusted_trace(
    events: list[Event],
) -> None:
    with pytest.raises(ValueError):
        RunTraceAggregator().aggregate(
            events=events,
            model_budget_status=_budget_status(),
        )


def test_trace_aggregation_rejects_budget_status_for_another_run() -> None:
    with pytest.raises(ValueError, match="budget status run_id"):
        RunTraceAggregator().aggregate(
            events=_trace(),
            model_budget_status=_budget_status("run-other"),
        )


def _recommendation_evidence(
    *,
    tier: EvidenceTier,
    trials: int,
    reliability_regression: bool = False,
) -> TeamRecommendationEvidence:
    return TeamRecommendationEvidence(
        evidence_tier=tier,
        paired_live_trials=trials,
        success_rate_delta=0.10,
        quality_delta=0.05,
        cost_ratio=1.25,
        duration_ratio=1.20,
        hard_reliability_regression=reliability_regression,
    )


def test_deterministic_or_too_few_live_trials_never_recommends_team() -> None:
    policy = TeamRecommendationPolicy(minimum_live_trials=5)

    deterministic = policy.decide(
        _recommendation_evidence(tier=EvidenceTier.DETERMINISTIC, trials=100)
    )
    too_few = policy.decide(
        _recommendation_evidence(tier=EvidenceTier.LIVE_PAIRED, trials=4)
    )

    assert deterministic.outcome is RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE
    assert too_few.outcome is RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE


def test_team_recommendation_requires_every_explicit_initial_threshold() -> None:
    policy = TeamRecommendationPolicy(
        minimum_live_trials=5,
        minimum_success_rate_delta=0.0,
        minimum_quality_delta=0.0,
        maximum_cost_ratio=1.5,
        maximum_duration_ratio=1.5,
    )

    recommended = policy.decide(
        _recommendation_evidence(tier=EvidenceTier.LIVE_PAIRED, trials=5)
    )
    unsafe = policy.decide(
        _recommendation_evidence(
            tier=EvidenceTier.LIVE_PAIRED,
            trials=5,
            reliability_regression=True,
        )
    )

    assert recommended.outcome is RecommendationOutcome.RECOMMEND_TEAM
    assert unsafe.outcome is RecommendationOutcome.KEEP_SINGLE
