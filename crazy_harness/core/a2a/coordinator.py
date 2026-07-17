from __future__ import annotations

from enum import StrEnum
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.core.a2a.messages import AgentCard


class AgentStatus(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    WAITING = "waiting"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    task_id: str
    goal: str
    required_capabilities: set[str] = Field(default_factory=set)
    exit_criteria: list[str] = Field(default_factory=list)


class AgentInstance(BaseModel):
    card: AgentCard
    status: AgentStatus = AgentStatus.IDLE


class PlanStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    revision: int
    assignment_id: str
    agent_id: str | None
    state: Literal["active", "superseded", "blocked"]
    reason: str


class Coordinator:
    """Capability-driven scheduler with an append-only rolling plan."""

    def __init__(
        self,
        cards: Iterable[AgentCard],
        *,
        statuses: dict[str, AgentStatus | str] | None = None,
    ) -> None:
        current_statuses = statuses or {}
        self.instances = {
            card.agent_id: AgentInstance(
                card=card.model_copy(deep=True),
                status=AgentStatus(current_statuses.get(card.agent_id, AgentStatus.IDLE)),
            )
            for card in cards
        }
        self.rolling_plan: list[PlanStep] = []
        self.replan_reason: str | None = None
        self._assignments: dict[str, Assignment] = {}
        self._active_steps: dict[str, PlanStep] = {}

    def assign(self, assignment: Assignment) -> PlanStep:
        instance = self._select_instance(assignment)
        revision = len(self.rolling_plan) + 1
        if instance is None:
            step = PlanStep(
                revision=revision,
                assignment_id=assignment.assignment_id,
                agent_id=None,
                state="blocked",
                reason="no_available_capable_agent",
            )
        else:
            instance.status = AgentStatus.BUSY
            step = PlanStep(
                revision=revision,
                assignment_id=assignment.assignment_id,
                agent_id=instance.card.agent_id,
                state="active",
                reason="capability_and_status_match",
            )
        self._assignments[assignment.assignment_id] = assignment.model_copy(deep=True)
        self._active_steps[assignment.assignment_id] = step
        self.rolling_plan.append(step)
        return step

    def update_status(
        self,
        agent_id: str,
        status: AgentStatus | str,
        *,
        reason: str,
    ) -> list[PlanStep]:
        new_status = AgentStatus(status)
        self.instances[agent_id].status = new_status
        if new_status not in {AgentStatus.DEGRADED, AgentStatus.OFFLINE}:
            return []

        self.replan_reason = f"{agent_id} became {new_status.value}: {reason}"
        affected = sorted(
            assignment_id
            for assignment_id, step in self._active_steps.items()
            if step.state == "active" and step.agent_id == agent_id
        )
        changes: list[PlanStep] = []
        for assignment_id in affected:
            superseded = PlanStep(
                revision=len(self.rolling_plan) + 1,
                assignment_id=assignment_id,
                agent_id=agent_id,
                state="superseded",
                reason=self.replan_reason,
            )
            self.rolling_plan.append(superseded)
            changes.append(superseded)

            replacement = self._select_instance(self._assignments[assignment_id])
            if replacement is None:
                next_step = PlanStep(
                    revision=len(self.rolling_plan) + 1,
                    assignment_id=assignment_id,
                    agent_id=None,
                    state="blocked",
                    reason="no_available_capable_agent",
                )
            else:
                replacement.status = AgentStatus.BUSY
                next_step = PlanStep(
                    revision=len(self.rolling_plan) + 1,
                    assignment_id=assignment_id,
                    agent_id=replacement.card.agent_id,
                    state="active",
                    reason="replanned_after_status_change",
                )
            self._active_steps[assignment_id] = next_step
            self.rolling_plan.append(next_step)
            changes.append(next_step)
        return changes

    def _select_instance(self, assignment: Assignment) -> AgentInstance | None:
        required = assignment.required_capabilities
        eligible = [
            instance
            for instance in self.instances.values()
            if instance.status == AgentStatus.IDLE and required.issubset(set(instance.card.capabilities))
        ]
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda instance: (
                len(set(instance.card.capabilities) - required),
                instance.card.agent_id,
            ),
        )
