from __future__ import annotations

from crazy_harness.core.evals.models import (
    EvalReport,
    EvalScenario,
    MetricComparison,
    MetricDirection,
    MetricThreshold,
    ScenarioComparison,
    ScenarioMetrics,
)


class EvalRunner:
    """Compare deterministic baseline and candidate observations."""

    def compare(
        self,
        *,
        scenarios: list[EvalScenario],
        baseline: list[ScenarioMetrics],
        candidate: list[ScenarioMetrics],
        baseline_version: str,
        candidate_version: str,
    ) -> EvalReport:
        baseline_by_id = self._index(baseline, "baseline")
        candidate_by_id = self._index(candidate, "candidate")
        compared = [
            self._compare_scenario(
                scenario,
                baseline_by_id.get(scenario.scenario_id),
                candidate_by_id.get(scenario.scenario_id),
            )
            for scenario in scenarios
        ]
        return EvalReport(
            baseline_version=baseline_version,
            candidate_version=candidate_version,
            scenarios=compared,
            baseline_passed=bool(compared) and all(item.baseline_passed for item in compared),
            passed=bool(compared) and all(item.passed for item in compared),
        )

    def _compare_scenario(
        self,
        scenario: EvalScenario,
        baseline: ScenarioMetrics | None,
        candidate: ScenarioMetrics | None,
    ) -> ScenarioComparison:
        metrics = [
            self._compare_metric(
                threshold,
                None if baseline is None else baseline.metrics.get(threshold.name),
                None if candidate is None else candidate.metrics.get(threshold.name),
            )
            for threshold in scenario.metrics
        ]
        return ScenarioComparison(
            scenario_id=scenario.scenario_id,
            metrics=metrics,
            baseline_passed=all(metric.baseline_threshold_met for metric in metrics),
            passed=all(metric.passed for metric in metrics),
        )

    @staticmethod
    def _compare_metric(
        spec: MetricThreshold,
        baseline: float | None,
        candidate: float | None,
    ) -> MetricComparison:
        if baseline is None or candidate is None:
            missing = "_and_".join(
                label
                for label, value in (("baseline", baseline), ("candidate", candidate))
                if value is None
            )
            baseline_met = baseline is not None and (
                baseline >= spec.threshold
                if spec.direction is MetricDirection.AT_LEAST
                else baseline <= spec.threshold
            )
            threshold_met = candidate is not None and (
                candidate >= spec.threshold
                if spec.direction is MetricDirection.AT_LEAST
                else candidate <= spec.threshold
            )
            return MetricComparison(
                name=spec.name,
                baseline=baseline,
                candidate=candidate,
                favorable_delta=None,
                baseline_threshold_met=baseline_met,
                threshold_met=threshold_met,
                non_regression_met=False,
                passed=False,
                reason=f"missing_{missing}_metric",
            )
        if spec.direction is MetricDirection.AT_LEAST:
            baseline_met = baseline >= spec.threshold
            threshold_met = candidate >= spec.threshold
            favorable_delta = candidate - baseline
        else:
            baseline_met = baseline <= spec.threshold
            threshold_met = candidate <= spec.threshold
            favorable_delta = baseline - candidate
        non_regression_met = favorable_delta >= -spec.max_regression
        return MetricComparison(
            name=spec.name,
            baseline=baseline,
            candidate=candidate,
            favorable_delta=favorable_delta,
            baseline_threshold_met=baseline_met,
            threshold_met=threshold_met,
            non_regression_met=non_regression_met,
            passed=threshold_met and non_regression_met,
        )

    @staticmethod
    def _index(results: list[ScenarioMetrics], label: str) -> dict[str, ScenarioMetrics]:
        indexed = {result.scenario_id: result for result in results}
        if len(indexed) != len(results):
            raise ValueError(f"duplicate {label} scenario metrics")
        return indexed
