from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.core.a2a.messages import A2AMessage


class PeerContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    task_id: str
    contract_version: int = Field(default=1, ge=1)
    scope: set[str] = Field(default_factory=set)
    permissions: set[str] = Field(default_factory=set)
    peer_budget: int = Field(default=0, ge=0)
    max_depth: int = Field(default=1, ge=0)


class PeerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: A2AMessage
    scope: set[str] = Field(default_factory=set)
    permissions: set[str] = Field(default_factory=set)
    budget_cost: int = Field(default=1, ge=1)


class PolicyDecision(BaseModel):
    allowed: bool
    reason: str
    remaining_budget: int


class PeerPolicy:
    """Authorize bounded peer requests made by non-coordinator agents."""

    ALLOWED_INTENTS = frozenset({"evidence", "review", "revision", "block", "progress"})

    def __init__(self) -> None:
        self._spent: dict[tuple[str, str], int] = {}

    def authorize(self, request: PeerRequest, contract: PeerContract) -> PolicyDecision:
        key = (contract.assignment_id, request.message.sender)
        spent = self._spent.get(key, 0)
        remaining = contract.peer_budget - spent
        if request.message.task_id != contract.task_id:
            return PolicyDecision(allowed=False, reason="task_mismatch", remaining_budget=remaining)
        if request.message.context_id != contract.assignment_id:
            return PolicyDecision(allowed=False, reason="assignment_mismatch", remaining_budget=remaining)
        if request.message.contract_version != contract.contract_version:
            return PolicyDecision(allowed=False, reason="contract_version_mismatch", remaining_budget=remaining)
        if request.message.intent not in self.ALLOWED_INTENTS:
            return PolicyDecision(allowed=False, reason="intent_not_allowed", remaining_budget=remaining)
        if not 1 <= request.message.depth <= min(contract.max_depth, 1):
            return PolicyDecision(allowed=False, reason="peer_depth_exceeded", remaining_budget=remaining)
        if not request.scope.issubset(contract.scope):
            return PolicyDecision(allowed=False, reason="scope_escalation", remaining_budget=remaining)
        if not request.permissions.issubset(contract.permissions):
            return PolicyDecision(allowed=False, reason="permission_escalation", remaining_budget=remaining)
        if request.budget_cost > remaining:
            return PolicyDecision(allowed=False, reason="peer_budget_exhausted", remaining_budget=remaining)

        remaining -= request.budget_cost
        self._spent[key] = spent + request.budget_cost
        return PolicyDecision(allowed=True, reason="allowed", remaining_budget=remaining)
