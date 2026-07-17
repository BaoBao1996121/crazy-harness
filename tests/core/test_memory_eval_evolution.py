from datetime import datetime, timedelta, timezone

import pytest

from crazy_harness.core.evals.evolution import (
    ChangeTarget,
    EvolutionCandidate,
    EvolutionController,
    EvolutionStatus,
    PermissionEffect,
    ShadowResult,
    TypedDiff,
)
from crazy_harness.core.evals.models import (
    EvalScenario,
    MetricDirection,
    MetricThreshold,
    ScenarioMetrics,
)
from crazy_harness.core.evals.runner import EvalRunner
from crazy_harness.core.memory.models import MemoryCandidate, MemorySlot, MemoryStatus
from crazy_harness.core.memory.store import MemoryConflictError, MemoryStore


def test_memory_candidate_requires_human_approval_before_recall(tmp_path):
    path = tmp_path / "memory.jsonl"
    store = MemoryStore(path)
    candidate = store.propose(
        MemoryCandidate(
            slot=MemorySlot.USER_CONSTRAINT,
            content="Never deploy directly to production.",
            scope="project:crazy",
            evidence=["event://run-1/human-correction"],
        )
    )

    assert candidate.status is MemoryStatus.CANDIDATE
    assert store.recall(scope="project:crazy") == []

    approved = store.approve(candidate.candidate_id, reviewer="course-owner")

    reopened = MemoryStore(path)
    assert approved.status is MemoryStatus.APPROVED
    assert reopened.recall(scope="project:crazy") == [approved]


def test_conflicting_memory_requires_explicit_supersede(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    current = store.propose(
        MemoryCandidate(
            slot=MemorySlot.PREFERENCE,
            content="Use concise release notes.",
            scope="user:owner",
            evidence=["event://run-1/review"],
        )
    )
    current = store.approve(current.candidate_id, reviewer="course-owner")
    replacement = store.propose(
        MemoryCandidate(
            slot=MemorySlot.PREFERENCE,
            content="Include evidence links in release notes.",
            scope="user:owner",
            evidence=["event://run-2/review"],
            version=2,
        )
    )

    with pytest.raises(MemoryConflictError):
        store.approve(replacement.candidate_id, reviewer="course-owner")

    replacement = store.supersede(
        current.candidate_id,
        replacement.candidate_id,
        reviewer="course-owner",
        reason="The newer correction is authoritative.",
    )

    assert store.get(current.candidate_id).status is MemoryStatus.SUPERSEDED
    assert replacement.supersedes == current.candidate_id
    assert store.recall(scope="user:owner") == [replacement]


def test_expired_memory_is_explicit_and_not_recalled(tmp_path):
    now = datetime.now(timezone.utc)
    store = MemoryStore(tmp_path / "memory.jsonl")
    candidate = store.propose(
        MemoryCandidate(
            slot=MemorySlot.ACTIVE_CONCERN,
            content="The staging API is temporarily rate limited.",
            scope="project:crazy",
            evidence=["event://run-3/tool-result"],
            expiry=now + timedelta(hours=1),
        )
    )
    approved = store.approve(candidate.candidate_id, reviewer="course-owner")

    assert store.recall(scope="project:crazy", at=now) == [approved]

    after_expiry = now + timedelta(hours=2)
    assert store.recall(scope="project:crazy", at=after_expiry) == []
    assert store.get(candidate.candidate_id, at=after_expiry).status is MemoryStatus.EXPIRED


def test_human_reject_and_revoke_keep_memory_out_of_recall(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    rejected = store.propose(
        MemoryCandidate(
            slot=MemorySlot.WORLD_FACT,
            content="An unverified endpoint exists.",
            scope="world:staging",
            evidence=["event://run-4/model-claim"],
        )
    )
    rejected = store.reject(rejected.candidate_id, reviewer="course-owner", reason="Unverified")

    revoked = store.propose(
        MemoryCandidate(
            slot=MemorySlot.PROCEDURE,
            content="Use the legacy release command.",
            scope="project:legacy",
            evidence=["event://run-5/postmortem"],
        )
    )
    revoked = store.approve(revoked.candidate_id, reviewer="course-owner")
    revoked = store.revoke(revoked.candidate_id, reviewer="course-owner", reason="Tool retired")

    assert rejected.status is MemoryStatus.REJECTED
    assert revoked.status is MemoryStatus.REVOKED
    assert store.recall(scope="world:staging") == []
    assert store.recall(scope="project:legacy") == []


def _eval_report(*, candidate_success: float = 0.86, candidate_tokens: float = 600):
    scenario = EvalScenario(
        scenario_id="safe-release",
        metrics=[
            MetricThreshold(
                name="task_success",
                direction=MetricDirection.AT_LEAST,
                threshold=0.80,
                max_regression=0.02,
            ),
            MetricThreshold(
                name="token_cost",
                direction=MetricDirection.AT_MOST,
                threshold=1_000,
            ),
        ],
    )

    return EvalRunner().compare(
        scenarios=[scenario],
        baseline=[
            ScenarioMetrics(
                scenario_id="safe-release",
                metrics={"task_success": 0.95, "token_cost": 900},
            )
        ],
        candidate=[
            ScenarioMetrics(
                scenario_id="safe-release",
                metrics={"task_success": candidate_success, "token_cost": candidate_tokens},
            )
        ],
        baseline_version="v1",
        candidate_version="v2",
    )


def test_eval_rejects_local_improvement_when_an_important_metric_regresses():
    report = _eval_report()
    comparisons = {metric.name: metric for metric in report.scenarios[0].metrics}
    assert comparisons["token_cost"].favorable_delta == 300
    assert comparisons["token_cost"].passed is True
    assert comparisons["task_success"].threshold_met is True
    assert comparisons["task_success"].non_regression_met is False
    assert report.baseline_passed is True
    assert report.passed is False


def test_eval_fails_closed_when_candidate_metrics_are_missing():
    scenario = EvalScenario(
        scenario_id="recovery",
        metrics=[
            MetricThreshold(
                name="task_success",
                direction=MetricDirection.AT_LEAST,
                threshold=0.80,
            )
        ],
    )

    report = EvalRunner().compare(
        scenarios=[scenario],
        baseline=[ScenarioMetrics(scenario_id="recovery", metrics={"task_success": 0.90})],
        candidate=[],
        baseline_version="v1",
        candidate_version="v2",
    )

    metric = report.scenarios[0].metrics[0]
    assert metric.reason == "missing_candidate_metric"
    assert report.baseline_passed is True
    assert report.passed is False


def test_evolution_rejects_hard_policy_and_permission_expansion(tmp_path):
    path = tmp_path / "evolution.jsonl"
    controller = EvolutionController(path, initial_version="v1")
    forbidden_diffs = [
        TypedDiff(
            target=ChangeTarget.HARD_POLICY,
            path="policy.production_requires_approval",
            before=True,
            after=False,
        ),
        TypedDiff(
            target=ChangeTarget.POLICY,
            path="tools.deploy.allowed_scopes",
            before=["staging"],
            after=["staging", "production"],
            permission_effect=PermissionEffect.EXPANDED,
        ),
        TypedDiff(
            target=ChangeTarget.PERMISSION,
            path="tools.deploy.allowed_scopes",
            before=["staging"],
            after=["staging", "production"],
        ),
    ]

    rejected_ids = []
    for index, diff in enumerate(forbidden_diffs, start=2):
        candidate = EvolutionCandidate(
            base_version="v1",
            proposed_version=f"v{index}",
            scope="project:crazy",
            rationale="Reduce release friction.",
            evidence=["eval://safe-release"],
            diffs=[diff],
        )
        rejected = controller.submit(candidate)
        rejected_ids.append(rejected.candidate_id)
        assert rejected.status is EvolutionStatus.REJECTED
        assert rejected.rejection_reason

    reopened = EvolutionController(path, initial_version="v1")
    assert reopened.active_version == "v1"
    assert all(
        reopened.get_candidate(candidate_id).status is EvolutionStatus.REJECTED
        for candidate_id in rejected_ids
    )


def test_evolution_candidate_must_propose_a_new_version():
    with pytest.raises(ValueError, match="proposed_version"):
        EvolutionCandidate(
            base_version="v1",
            proposed_version="v1",
            scope="project:crazy",
            rationale="No real version change.",
            evidence=["trace://run-7"],
            diffs=[
                TypedDiff(
                    target=ChangeTarget.PROMPT,
                    path="prompt.release.summary",
                    before="long",
                    after="short",
                )
            ],
        )


def test_offline_gate_rejects_a_candidate_with_overall_regression(tmp_path):
    controller = EvolutionController(tmp_path / "evolution.jsonl", initial_version="v1")
    candidate = controller.submit(
        EvolutionCandidate(
            base_version="v1",
            proposed_version="v2",
            scope="project:crazy",
            rationale="Save tokens in the release prompt.",
            evidence=["trace://run-8"],
            diffs=[
                TypedDiff(
                    target=ChangeTarget.PROMPT,
                    path="prompt.release.instructions",
                    before="verify, then summarize",
                    after="summarize",
                )
            ],
        )
    )

    rejected = controller.offline_gate(candidate.candidate_id, _eval_report())

    assert rejected.status is EvolutionStatus.REJECTED
    assert rejected.rejection_reason == "offline_eval_failed"
    assert rejected.offline_report is not None
    assert controller.active_version == "v1"


def test_approved_evolution_can_promote_and_rollback_across_restarts(tmp_path):
    path = tmp_path / "evolution.jsonl"
    controller = EvolutionController(path, initial_version="v1")
    candidate = controller.submit(
        EvolutionCandidate(
            base_version="v1",
            proposed_version="v2",
            scope="project:crazy",
            rationale="Require evidence links in release summaries.",
            evidence=["trace://run-9", "eval://safe-release"],
            diffs=[
                TypedDiff(
                    target=ChangeTarget.PROMPT,
                    path="prompt.release.evidence_requirement",
                    before=False,
                    after=True,
                )
            ],
        )
    )
    candidate = controller.offline_gate(
        candidate.candidate_id,
        _eval_report(candidate_success=0.96, candidate_tokens=800),
    )
    candidate = controller.record_shadow(
        candidate.candidate_id,
        ShadowResult(
            passed=True,
            baseline_version="v1",
            candidate_version="v2",
            metrics={"decision_match": 1.0},
        ),
    )
    candidate = controller.approve(
        candidate.candidate_id,
        reviewer="course-owner",
        reason="Offline and shadow gates passed.",
    )

    promotion = controller.promote(candidate.candidate_id)

    assert promotion.previous_version == "v1"
    assert promotion.version == "v2"
    assert controller.active_version == "v2"
    assert controller.get_candidate(candidate.candidate_id).status is EvolutionStatus.PROMOTED

    reopened = EvolutionController(path, initial_version="v1")
    rollback = reopened.rollback(reviewer="course-owner", reason="Post-promotion regression")

    assert rollback.from_version == "v2"
    assert rollback.to_version == "v1"
    assert reopened.active_version == "v1"
    assert EvolutionController(path, initial_version="v1").get_candidate(
        candidate.candidate_id
    ).status is EvolutionStatus.ROLLED_BACK
