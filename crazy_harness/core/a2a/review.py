from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvidencePack(BaseModel):
    """The complete, transcript-free view available to a reviewer."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    goal: str
    exit_criteria: list[str]
    candidate_artifact_refs: list[str] = Field(default_factory=list)
    evidence_by_criterion: dict[str, list[str]] = Field(default_factory=dict)


class CriterionReview(BaseModel):
    criterion: str
    verdict: Literal["approve", "revise"]
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str


class ReviewDecision(BaseModel):
    assignment_id: str
    verdict: Literal["approve", "revise"]
    criteria: list[CriterionReview]


class ReviewerGate:
    """Produce an auditable verdict for every exit criterion in a pack."""

    def review(self, pack: EvidencePack) -> ReviewDecision:
        criteria: list[CriterionReview] = []
        for criterion in pack.exit_criteria:
            evidence_refs = [ref for ref in pack.evidence_by_criterion.get(criterion, []) if ref]
            verdict = "approve" if evidence_refs else "revise"
            criteria.append(
                CriterionReview(
                    criterion=criterion,
                    verdict=verdict,
                    evidence_refs=evidence_refs,
                    reason="evidence_attached" if evidence_refs else "missing_evidence",
                )
            )
        overall = "approve" if all(item.verdict == "approve" for item in criteria) else "revise"
        return ReviewDecision(
            assignment_id=pack.assignment_id,
            verdict=overall,
            criteria=criteria,
        )
