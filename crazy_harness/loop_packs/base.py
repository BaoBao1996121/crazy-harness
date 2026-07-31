from __future__ import annotations

from typing import Protocol

from crazy_harness.control_plane.engineering_loops import ChildRunOutcome
from crazy_harness.core.engineering_loops import (
    CandidateProposal,
    EngineeringIterationIdentity,
    EngineeringLoopContract,
    IterationEvaluation,
)
from crazy_harness.taskpacks import TaskPack


class LoopPack(Protocol):
    """Business adapter around the generic durable EngineeringLoop state machine."""

    loop_pack_id: str
    task_pack: TaskPack
    required_permissions: frozenset[str]

    def propose(
        self,
        contract: EngineeringLoopContract,
        identity: EngineeringIterationIdentity,
        base_state_ref: str,
    ) -> CandidateProposal: ...

    def evaluate(
        self,
        contract: EngineeringLoopContract,
        candidate: CandidateProposal,
        outcome: ChildRunOutcome,
    ) -> IterationEvaluation: ...
