from decimal import Decimal

import pytest

from crazy_harness.core.engineering_loops import (
    CandidateProposal,
    DeterministicLoopPolicy,
    EngineeringLoopBudget,
    EngineeringLoopContract,
    IterationEvaluation,
    LoopDecisionKind,
    MetricContract,
    MetricDirection,
    PromotionMode,
    WorkerProfile,
    engineering_iteration_identity,
)


def contract(
    *,
    direction: MetricDirection = MetricDirection.MAXIMIZE,
    target: str = "1",
    promotion_mode: PromotionMode = PromotionMode.AUTO_DISPOSABLE,
    max_iterations: int = 3,
    max_no_progress: int = 2,
) -> EngineeringLoopContract:
    return EngineeringLoopContract(
        loop_id="loop-quality",
        request_fingerprint="request-sha256",
        title="Improve repository quality",
        objective="Reach every independent repository quality gate",
        exit_criteria=("quality score reaches the frozen target",),
        loop_pack="repo-quality",
        worker=WorkerProfile(
            execution_mode="single",
            model_mode="scripted",
            task_pack="repo-quality",
        ),
        metric=MetricContract(
            name="quality_score",
            direction=direction,
            target=Decimal(target),
            evaluator_version="repo-quality-v1",
        ),
        budget=EngineeringLoopBudget(
            max_iterations=max_iterations,
            max_no_progress_iterations=max_no_progress,
        ),
        promotion_mode=promotion_mode,
        permissions=("disposable_workspace_write", "run_bounded_checks"),
        initial_state_ref="snapshot://initial",
    )


def evaluation(
    score: str,
    *,
    iteration: int = 1,
    valid: bool = True,
    hard_gate: bool = True,
    version: str = "repo-quality-v1",
) -> IterationEvaluation:
    return IterationEvaluation(
        iteration=iteration,
        candidate_id=f"candidate-{iteration}",
        candidate_state_ref=f"snapshot://candidate-{iteration}",
        evaluator_version=version,
        metrics={"quality_score": Decimal(score)},
        hard_gates={"workspace_confined": hard_gate},
        evidence_refs=("artifact://evaluation-1",),
        valid=valid,
    )


def test_contract_requires_a_machine_budget_and_one_exit_criterion() -> None:
    with pytest.raises(ValueError):
        contract(max_iterations=0)
    with pytest.raises(ValueError):
        EngineeringLoopContract.model_validate(
            {**contract().model_dump(mode="python"), "exit_criteria": ()}
        )


def test_iteration_and_child_run_identities_are_stable_and_distinct() -> None:
    first = engineering_iteration_identity("loop-quality", 1)
    replay = engineering_iteration_identity("loop-quality", 1)
    second = engineering_iteration_identity("loop-quality", 2)

    assert first == replay
    assert first.iteration_id != second.iteration_id
    assert first.child_run_id != second.child_run_id


def test_candidate_must_be_based_on_the_current_active_state() -> None:
    candidate = CandidateProposal(
        candidate_id="candidate-1",
        iteration=1,
        base_state_ref="snapshot://stale",
        change_set={"kind": "replace", "path": "calculator.py"},
        rationale="Improve the implementation",
        expected_effect="Increase quality score",
        proposer_attestation={"provider": "scripted", "version": "v1"},
    )

    with pytest.raises(ValueError, match="current active state"):
        candidate.validate_base("snapshot://active")


def test_valid_target_evidence_completes_an_auto_disposable_loop() -> None:
    decision = DeterministicLoopPolicy().decide(
        contract(),
        iteration=1,
        evaluation=evaluation("1"),
        active_score=Decimal("0.5"),
        no_progress_count=0,
    )

    assert decision.kind is LoopDecisionKind.COMPLETE
    assert decision.accepted is True
    assert decision.active_state_ref == "snapshot://candidate-1"
    assert decision.next_no_progress_count == 0


@pytest.mark.parametrize(
    ("kwargs", "reason_fragment"),
    [
        ({"valid": False}, "invalid"),
        ({"hard_gate": False}, "hard gate"),
        ({"version": "changed-v2"}, "evaluator version"),
    ],
)
def test_invalid_or_untrusted_evidence_cannot_complete(kwargs, reason_fragment) -> None:
    decision = DeterministicLoopPolicy().decide(
        contract(max_iterations=1),
        iteration=1,
        evaluation=evaluation("1", **kwargs),
        active_score=Decimal("0.5"),
        no_progress_count=0,
    )

    assert decision.kind is LoopDecisionKind.BLOCKED
    assert decision.accepted is False
    assert reason_fragment in decision.reason


def test_improving_candidate_is_accepted_but_loop_continues_below_target() -> None:
    decision = DeterministicLoopPolicy().decide(
        contract(target="2"),
        iteration=1,
        evaluation=evaluation("1"),
        active_score=Decimal("0.5"),
        no_progress_count=1,
    )

    assert decision.kind is LoopDecisionKind.ACCEPT_CONTINUE
    assert decision.accepted is True
    assert decision.next_no_progress_count == 0


def test_regression_is_rejected_and_repeated_no_progress_blocks() -> None:
    policy = DeterministicLoopPolicy()
    first = policy.decide(
        contract(target="2"),
        iteration=1,
        evaluation=evaluation("0.4"),
        active_score=Decimal("0.5"),
        no_progress_count=0,
    )
    blocked = policy.decide(
        contract(target="2"),
        iteration=2,
        evaluation=evaluation("0.4", iteration=2),
        active_score=Decimal("0.5"),
        no_progress_count=1,
    )

    assert first.kind is LoopDecisionKind.REJECT_CONTINUE
    assert first.accepted is False
    assert first.active_state_ref is None
    assert first.next_no_progress_count == 1
    assert blocked.kind is LoopDecisionKind.BLOCKED
    assert blocked.next_no_progress_count == 2


def test_target_candidate_waits_when_promotion_requires_approval() -> None:
    decision = DeterministicLoopPolicy().decide(
        contract(promotion_mode=PromotionMode.APPROVAL_REQUIRED),
        iteration=1,
        evaluation=evaluation("1"),
        active_score=Decimal("0.5"),
        no_progress_count=0,
    )

    assert decision.kind is LoopDecisionKind.AWAITING_APPROVAL
    assert decision.accepted is False
    assert decision.pending_state_ref == "snapshot://candidate-1"
