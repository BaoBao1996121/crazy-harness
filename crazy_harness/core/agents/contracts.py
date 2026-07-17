from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AssignmentBudget(BaseModel):
    """Optional hard limits carried by an assignment contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turns: int | None = Field(default=None, ge=1)
    tokens: int | None = Field(default=None, ge=1)
    tool_calls: int | None = Field(default=None, ge=0)
    retries: int | None = Field(default=None, ge=0)
    wall_time_seconds: float | None = Field(default=None, gt=0)
    cost_usd: float | None = Field(default=None, ge=0)


class AssignmentContract(BaseModel):
    """Immutable, versioned boundary between a coordinator and a worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1)
    goal: NonEmptyText
    exit_criteria: tuple[NonEmptyText, ...] = Field(min_length=1)
    output_schema: dict[str, Any]
    evidence_requirements: tuple[NonEmptyText, ...] = ()
    constraints: tuple[NonEmptyText, ...] = ()
    permissions: tuple[NonEmptyText, ...] = ()
    budgets: AssignmentBudget = Field(default_factory=AssignmentBudget)
    dependencies: tuple[NonEmptyText, ...] = ()
