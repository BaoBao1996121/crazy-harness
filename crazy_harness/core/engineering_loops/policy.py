from __future__ import annotations

from decimal import Decimal

from crazy_harness.core.engineering_loops.models import (
    EngineeringLoopContract,
    IterationEvaluation,
    LoopDecision,
    LoopDecisionKind,
    MetricDirection,
    PromotionMode,
)


class DeterministicLoopPolicy:
    """Make promotion decisions only from frozen contract and evaluator facts."""

    def decide(
        self,
        contract: EngineeringLoopContract,
        *,
        iteration: int,
        evaluation: IterationEvaluation,
        active_score: Decimal | None,
        no_progress_count: int,
    ) -> LoopDecision:
        metric = contract.metric
        if evaluation.iteration != iteration:
            return self._blocked(evaluation, no_progress_count, "evaluation iteration mismatch")
        if evaluation.evaluator_version != metric.evaluator_version:
            return self._blocked(evaluation, no_progress_count, "evaluator version mismatch")
        if not evaluation.valid:
            return self._blocked(evaluation, no_progress_count, "evaluation is invalid")
        score = evaluation.metrics.get(metric.name)
        if score is None:
            return self._blocked(evaluation, no_progress_count, "required metric is missing")

        hard_gates_passed = all(evaluation.hard_gates.values())
        improved = hard_gates_passed and self._improved(
            metric.direction,
            score,
            active_score,
        )
        next_no_progress = 0 if improved else no_progress_count + 1
        target_reached = hard_gates_passed and self._target_reached(
            metric.direction,
            score,
            metric.target,
        )
        if target_reached:
            if contract.promotion_mode is PromotionMode.AUTO_DISPOSABLE:
                return LoopDecision(
                    kind=LoopDecisionKind.COMPLETE,
                    iteration=iteration,
                    candidate_id=evaluation.candidate_id,
                    accepted=True,
                    score=score,
                    active_state_ref=evaluation.candidate_state_ref,
                    next_no_progress_count=0,
                    reason="candidate reached the frozen target and every hard gate passed",
                )
            return LoopDecision(
                kind=LoopDecisionKind.AWAITING_APPROVAL,
                iteration=iteration,
                candidate_id=evaluation.candidate_id,
                accepted=False,
                score=score,
                pending_state_ref=evaluation.candidate_state_ref,
                next_no_progress_count=0,
                reason="target reached but promotion requires approval",
            )

        exhausted_iterations = iteration >= contract.budget.max_iterations
        exhausted_progress = (
            not improved
            and next_no_progress >= contract.budget.max_no_progress_iterations
        )
        if exhausted_iterations or exhausted_progress:
            reason = (
                "candidate failed a hard gate and the loop budget is exhausted"
                if not hard_gates_passed
                else "loop budget exhausted before the target was reached"
            )
            return LoopDecision(
                kind=LoopDecisionKind.BLOCKED,
                iteration=iteration,
                candidate_id=evaluation.candidate_id,
                accepted=False,
                score=score,
                next_no_progress_count=next_no_progress,
                reason=reason,
            )

        if improved:
            return LoopDecision(
                kind=LoopDecisionKind.ACCEPT_CONTINUE,
                iteration=iteration,
                candidate_id=evaluation.candidate_id,
                accepted=True,
                score=score,
                active_state_ref=evaluation.candidate_state_ref,
                next_no_progress_count=0,
                reason="candidate improved the active metric but has not reached the target",
            )
        reason = (
            "candidate failed at least one hard gate"
            if not hard_gates_passed
            else "candidate did not improve the active metric"
        )
        return LoopDecision(
            kind=LoopDecisionKind.REJECT_CONTINUE,
            iteration=iteration,
            candidate_id=evaluation.candidate_id,
            accepted=False,
            score=score,
            next_no_progress_count=next_no_progress,
            reason=reason,
        )

    @staticmethod
    def _target_reached(
        direction: MetricDirection,
        score: Decimal,
        target: Decimal,
    ) -> bool:
        if direction is MetricDirection.MAXIMIZE:
            return score >= target
        return score <= target

    @staticmethod
    def _improved(
        direction: MetricDirection,
        score: Decimal,
        active_score: Decimal | None,
    ) -> bool:
        if active_score is None:
            return True
        if direction is MetricDirection.MAXIMIZE:
            return score > active_score
        return score < active_score

    @staticmethod
    def _blocked(
        evaluation: IterationEvaluation,
        no_progress_count: int,
        reason: str,
    ) -> LoopDecision:
        return LoopDecision(
            kind=LoopDecisionKind.BLOCKED,
            iteration=evaluation.iteration,
            candidate_id=evaluation.candidate_id,
            accepted=False,
            next_no_progress_count=no_progress_count,
            reason=reason,
        )
