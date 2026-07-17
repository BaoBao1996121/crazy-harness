from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PlanEventType(StrEnum):
    CREATED = "plan.created"
    REVISED = "plan.revised"
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_CANCELLED = "step.cancelled"
    STEP_SUPERSEDED = "step.superseded"


class PlanStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: NonEmptyText
    description: NonEmptyText
    status: PlanStepStatus = PlanStepStatus.PENDING
    evidence_refs: tuple[NonEmptyText, ...] = ()


class PlanEvent(BaseModel):
    """A persisted LocalPlan fact, not model chain-of-thought."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: PlanEventType
    steps: tuple[PlanStep, ...] = ()
    step_id: NonEmptyText | None = None
    evidence_refs: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def validate_payload(self) -> "PlanEvent":
        is_plan_event = self.type in {PlanEventType.CREATED, PlanEventType.REVISED}
        if is_plan_event:
            if not self.steps:
                raise ValueError(f"steps are required for {self.type.value}")
            if self.step_id is not None:
                raise ValueError(f"step_id is not allowed for {self.type.value}")
            ids = [step.step_id for step in self.steps]
            if len(ids) != len(set(ids)):
                raise ValueError("plan steps must have unique step_id values")
        elif self.step_id is None:
            raise ValueError(f"step_id is required for {self.type.value}")
        if self.evidence_refs and self.type is not PlanEventType.STEP_COMPLETED:
            raise ValueError("evidence_refs are only allowed for step.completed")
        return self


class LocalPlan(BaseModel):
    """Latest projection rebuilt from PlanEvent facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=0, ge=0)
    steps: tuple[PlanStep, ...] = ()

    @property
    def is_complete(self) -> bool:
        inactive = {PlanStepStatus.CANCELLED, PlanStepStatus.SUPERSEDED}
        active = [step for step in self.steps if step.status not in inactive]
        return bool(active) and all(step.status is PlanStepStatus.COMPLETED for step in active)


def reduce_plan(events: Iterable[PlanEvent | Mapping[str, Any]]) -> LocalPlan:
    plan = LocalPlan()
    for raw_event in events:
        event = raw_event if isinstance(raw_event, PlanEvent) else PlanEvent.model_validate(raw_event)
        if event.type is PlanEventType.CREATED:
            if plan.version:
                raise ValueError("plan.created may only appear once")
            plan = LocalPlan(version=1, steps=tuple(_fresh(step) for step in event.steps))
        elif event.type is PlanEventType.REVISED:
            _require_plan(plan)
            plan = _revise(plan, event.steps)
        else:
            _require_plan(plan)
            plan = _apply_step_event(plan, event)
    return plan


def _fresh(step: PlanStep) -> PlanStep:
    return PlanStep(step_id=step.step_id, description=step.description)


def _revise(plan: LocalPlan, requested_steps: tuple[PlanStep, ...]) -> LocalPlan:
    previous = {step.step_id: step for step in plan.steps}
    revised: list[PlanStep] = []
    for requested in requested_steps:
        current = previous.pop(requested.step_id, None)
        if current is None:
            revised.append(_fresh(requested))
        else:
            revised.append(
                PlanStep(
                    step_id=current.step_id,
                    description=requested.description,
                    status=current.status,
                    evidence_refs=current.evidence_refs,
                )
            )
    for removed in previous.values():
        status = removed.status if removed.status is PlanStepStatus.CANCELLED else PlanStepStatus.SUPERSEDED
        revised.append(
            PlanStep(
                step_id=removed.step_id,
                description=removed.description,
                status=status,
                evidence_refs=removed.evidence_refs,
            )
        )
    return LocalPlan(version=plan.version + 1, steps=tuple(revised))


def _apply_step_event(plan: LocalPlan, event: PlanEvent) -> LocalPlan:
    index = next((i for i, step in enumerate(plan.steps) if step.step_id == event.step_id), None)
    if index is None:
        raise ValueError(f"unknown plan step: {event.step_id}")
    step = plan.steps[index]

    if event.type is PlanEventType.STEP_STARTED:
        if step.status not in {PlanStepStatus.PENDING, PlanStepStatus.RUNNING}:
            raise ValueError(f"cannot start {step.step_id} from {step.status.value}")
        status = PlanStepStatus.RUNNING
        evidence_refs = step.evidence_refs
    elif event.type is PlanEventType.STEP_COMPLETED:
        if step.status in {PlanStepStatus.CANCELLED, PlanStepStatus.SUPERSEDED}:
            raise ValueError(f"cannot complete {step.step_id} from {step.status.value}")
        status = PlanStepStatus.COMPLETED
        evidence_refs = tuple(dict.fromkeys((*step.evidence_refs, *event.evidence_refs)))
    elif event.type is PlanEventType.STEP_CANCELLED:
        status = PlanStepStatus.CANCELLED
        evidence_refs = step.evidence_refs
    else:
        status = PlanStepStatus.SUPERSEDED
        evidence_refs = step.evidence_refs

    updated = PlanStep(
        step_id=step.step_id,
        description=step.description,
        status=status,
        evidence_refs=evidence_refs,
    )
    steps = list(plan.steps)
    steps[index] = updated
    return LocalPlan(version=plan.version, steps=tuple(steps))


def _require_plan(plan: LocalPlan) -> None:
    if not plan.version:
        raise ValueError("plan.created must be the first plan event")
