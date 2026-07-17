from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MetricDirection(StrEnum):
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class MetricThreshold(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    name: str = Field(min_length=1)
    direction: MetricDirection
    threshold: float
    max_regression: float = Field(default=0.0, ge=0.0)


class EvalScenario(BaseModel):
    scenario_id: str = Field(min_length=1)
    description: str = ""
    metrics: list[MetricThreshold] = Field(min_length=1)

    @model_validator(mode="after")
    def metric_names_are_unique(self) -> "EvalScenario":
        names = [metric.name for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("metric names must be unique within a scenario")
        return self


class ScenarioMetrics(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    scenario_id: str = Field(min_length=1)
    metrics: dict[str, float] = Field(min_length=1)


class MetricComparison(BaseModel):
    name: str
    baseline: float | None
    candidate: float | None
    favorable_delta: float | None
    baseline_threshold_met: bool
    threshold_met: bool
    non_regression_met: bool
    passed: bool
    reason: str = ""


class ScenarioComparison(BaseModel):
    scenario_id: str
    metrics: list[MetricComparison]
    baseline_passed: bool
    passed: bool


class EvalReport(BaseModel):
    baseline_version: str
    candidate_version: str
    scenarios: list[ScenarioComparison]
    baseline_passed: bool
    passed: bool
