from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from crazy_harness.core.evals.models import EvalReport


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChangeTarget(StrEnum):
    PROMPT = "prompt"
    SKILL = "skill"
    POLICY = "policy"
    TOOL_SEARCH = "tool_search"
    THRESHOLD = "threshold"
    HARD_POLICY = "hard_policy"
    PERMISSION = "permission"


class DiffOperation(StrEnum):
    ADD = "add"
    REPLACE = "replace"
    REMOVE = "remove"


class PermissionEffect(StrEnum):
    UNCHANGED = "unchanged"
    REDUCED = "reduced"
    EXPANDED = "expanded"


class EvolutionStatus(StrEnum):
    CANDIDATE = "candidate"
    OFFLINE_PASSED = "offline_passed"
    SHADOW_PASSED = "shadow_passed"
    APPROVED = "approved"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class TypedDiff(BaseModel):
    target: ChangeTarget
    path: str = Field(min_length=1)
    operation: DiffOperation = DiffOperation.REPLACE
    before: JsonValue | None = None
    after: JsonValue | None = None
    permission_effect: PermissionEffect = PermissionEffect.UNCHANGED

    @model_validator(mode="after")
    def changes_value(self) -> "TypedDiff":
        if self.before == self.after:
            raise ValueError("a typed diff must change the value")
        return self


class ShadowResult(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    passed: bool
    baseline_version: str = Field(min_length=1)
    candidate_version: str = Field(min_length=1)
    metrics: dict[str, float] = Field(default_factory=dict)
    notes: str = ""
    observed_at: AwareDatetime = Field(default_factory=utc_now)


class EvolutionCandidate(BaseModel):
    candidate_id: str = Field(default_factory=lambda: f"evolution_{uuid4().hex}")
    base_version: str = Field(min_length=1)
    proposed_version: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    diffs: list[TypedDiff] = Field(min_length=1)
    status: EvolutionStatus = EvolutionStatus.CANDIDATE
    offline_report: EvalReport | None = None
    shadow_result: ShadowResult | None = None
    reviewed_by: str | None = None
    approval_reason: str = ""
    rejection_reason: str = ""
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def proposes_a_new_version(self) -> "EvolutionCandidate":
        if self.proposed_version == self.base_version:
            raise ValueError("proposed_version must differ from base_version")
        return self


class PromotionRecord(BaseModel):
    candidate_id: str
    previous_version: str
    version: str
    promoted_at: AwareDatetime = Field(default_factory=utc_now)
    rolled_back_at: AwareDatetime | None = None
    rollback_reviewer: str | None = None
    rollback_reason: str = ""


class RollbackResult(BaseModel):
    candidate_id: str
    from_version: str
    to_version: str
    reviewer: str
    reason: str = ""
    rolled_back_at: AwareDatetime


class _EvolutionState(BaseModel):
    active_version: str
    candidates: dict[str, EvolutionCandidate] = Field(default_factory=dict)
    promotions: list[PromotionRecord] = Field(default_factory=list)


class InvalidEvolutionTransitionError(ValueError):
    pass


class EvolutionController:
    """Persistent release-style gate for controlled harness changes."""

    def __init__(self, path: Path, *, initial_version: str) -> None:
        if not initial_version.strip():
            raise ValueError("initial_version is required")
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._append("initialize", _EvolutionState(active_version=initial_version))

    @property
    def active_version(self) -> str:
        return self._load().active_version

    def get_candidate(self, candidate_id: str) -> EvolutionCandidate:
        try:
            return self._load().candidates[candidate_id]
        except KeyError as exc:
            raise KeyError(f"unknown evolution candidate: {candidate_id}") from exc

    def submit(self, candidate: EvolutionCandidate) -> EvolutionCandidate:
        if candidate.status is not EvolutionStatus.CANDIDATE:
            raise InvalidEvolutionTransitionError("new evolution must start as a candidate")
        state = self._load()
        if candidate.candidate_id in state.candidates:
            raise ValueError(f"duplicate evolution candidate: {candidate.candidate_id}")
        reason = self._rejection_reason(candidate)
        if candidate.base_version != state.active_version:
            reason = f"stale_base_version:{state.active_version}"
        submitted = candidate
        if reason:
            submitted = candidate.model_copy(
                update={
                    "status": EvolutionStatus.REJECTED,
                    "rejection_reason": reason,
                    "updated_at": utc_now(),
                }
            )
        candidates = dict(state.candidates)
        candidates[submitted.candidate_id] = submitted
        self._append("submit", state.model_copy(update={"candidates": candidates}))
        return submitted

    def offline_gate(self, candidate_id: str, report: EvalReport) -> EvolutionCandidate:
        state = self._load()
        candidate = self._state_candidate(state, candidate_id)
        self._require_status(candidate, EvolutionStatus.CANDIDATE)
        if (report.baseline_version, report.candidate_version) != (
            candidate.base_version,
            candidate.proposed_version,
        ):
            raise ValueError("eval report versions do not match the evolution candidate")
        passed = report.passed
        evaluated = candidate.model_copy(
            update={
                "status": EvolutionStatus.OFFLINE_PASSED if passed else EvolutionStatus.REJECTED,
                "offline_report": report,
                "rejection_reason": "" if passed else "offline_eval_failed",
                "updated_at": utc_now(),
            }
        )
        candidates = dict(state.candidates)
        candidates[candidate_id] = evaluated
        self._append("offline_gate", state.model_copy(update={"candidates": candidates}))
        return evaluated

    def record_shadow(self, candidate_id: str, result: ShadowResult) -> EvolutionCandidate:
        state = self._load()
        candidate = self._state_candidate(state, candidate_id)
        self._require_status(candidate, EvolutionStatus.OFFLINE_PASSED)
        if (result.baseline_version, result.candidate_version) != (
            candidate.base_version,
            candidate.proposed_version,
        ):
            raise ValueError("shadow result versions do not match the evolution candidate")
        shadowed = candidate.model_copy(
            update={
                "status": (
                    EvolutionStatus.SHADOW_PASSED
                    if result.passed
                    else EvolutionStatus.REJECTED
                ),
                "shadow_result": result,
                "rejection_reason": "" if result.passed else "shadow_failed",
                "updated_at": utc_now(),
            }
        )
        self._save_candidate("record_shadow", state, shadowed)
        return shadowed

    def approve(
        self,
        candidate_id: str,
        *,
        reviewer: str,
        reason: str = "",
    ) -> EvolutionCandidate:
        state = self._load()
        candidate = self._state_candidate(state, candidate_id)
        self._require_status(candidate, EvolutionStatus.SHADOW_PASSED)
        approved = candidate.model_copy(
            update={
                "status": EvolutionStatus.APPROVED,
                "reviewed_by": self._require_reviewer(reviewer),
                "approval_reason": reason,
                "updated_at": utc_now(),
            }
        )
        self._save_candidate("approve", state, approved)
        return approved

    def promote(self, candidate_id: str) -> PromotionRecord:
        state = self._load()
        candidate = self._state_candidate(state, candidate_id)
        self._require_status(candidate, EvolutionStatus.APPROVED)
        if state.active_version != candidate.base_version:
            raise InvalidEvolutionTransitionError("candidate base is no longer the active version")
        if any(record.version == candidate.proposed_version for record in state.promotions):
            raise InvalidEvolutionTransitionError("proposed version has already been used")
        promoted = candidate.model_copy(
            update={"status": EvolutionStatus.PROMOTED, "updated_at": utc_now()}
        )
        promotion = PromotionRecord(
            candidate_id=candidate_id,
            previous_version=state.active_version,
            version=candidate.proposed_version,
        )
        candidates = dict(state.candidates)
        candidates[candidate_id] = promoted
        self._append(
            "promote",
            state.model_copy(
                update={
                    "active_version": candidate.proposed_version,
                    "candidates": candidates,
                    "promotions": [*state.promotions, promotion],
                }
            ),
        )
        return promotion

    def rollback(self, *, reviewer: str, reason: str = "") -> RollbackResult:
        reviewer = self._require_reviewer(reviewer)
        state = self._load()
        index = next(
            (
                index
                for index in range(len(state.promotions) - 1, -1, -1)
                if state.promotions[index].version == state.active_version
                and state.promotions[index].rolled_back_at is None
            ),
            None,
        )
        if index is None:
            raise InvalidEvolutionTransitionError("active version has no promotion to roll back")
        now = utc_now()
        promotion = state.promotions[index]
        rolled_promotion = promotion.model_copy(
            update={
                "rolled_back_at": now,
                "rollback_reviewer": reviewer,
                "rollback_reason": reason,
            }
        )
        candidate = self._state_candidate(state, promotion.candidate_id)
        self._require_status(candidate, EvolutionStatus.PROMOTED)
        rolled_candidate = candidate.model_copy(
            update={"status": EvolutionStatus.ROLLED_BACK, "updated_at": now}
        )
        promotions = list(state.promotions)
        promotions[index] = rolled_promotion
        candidates = dict(state.candidates)
        candidates[candidate.candidate_id] = rolled_candidate
        self._append(
            "rollback",
            state.model_copy(
                update={
                    "active_version": promotion.previous_version,
                    "candidates": candidates,
                    "promotions": promotions,
                }
            ),
        )
        return RollbackResult(
            candidate_id=candidate.candidate_id,
            from_version=promotion.version,
            to_version=promotion.previous_version,
            reviewer=reviewer,
            reason=reason,
            rolled_back_at=now,
        )

    @staticmethod
    def _rejection_reason(candidate: EvolutionCandidate) -> str:
        if any(diff.target is ChangeTarget.HARD_POLICY for diff in candidate.diffs):
            return "hard_policy_changes_are_forbidden"
        if any(diff.permission_effect is PermissionEffect.EXPANDED for diff in candidate.diffs):
            return "permission_expansion_is_forbidden"
        if any(
            diff.target is ChangeTarget.PERMISSION
            and diff.permission_effect is not PermissionEffect.REDUCED
            for diff in candidate.diffs
        ):
            return "permission_change_must_be_explicitly_reducing"
        return ""

    @staticmethod
    def _require_status(candidate: EvolutionCandidate, expected: EvolutionStatus) -> None:
        if candidate.status is not expected:
            raise InvalidEvolutionTransitionError(
                f"evolution {candidate.candidate_id} is {candidate.status}, expected {expected}"
            )

    @staticmethod
    def _require_reviewer(reviewer: str) -> str:
        if not reviewer.strip():
            raise ValueError("a human reviewer is required")
        return reviewer.strip()

    @staticmethod
    def _state_candidate(state: _EvolutionState, candidate_id: str) -> EvolutionCandidate:
        try:
            return state.candidates[candidate_id]
        except KeyError as exc:
            raise KeyError(f"unknown evolution candidate: {candidate_id}") from exc

    def _save_candidate(
        self,
        action: str,
        state: _EvolutionState,
        candidate: EvolutionCandidate,
    ) -> None:
        candidates = dict(state.candidates)
        candidates[candidate.candidate_id] = candidate
        self._append(action, state.model_copy(update={"candidates": candidates}))

    def _load(self) -> _EvolutionState:
        last: dict[str, object] | None = None
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = json.loads(line)
        if last is None:
            raise RuntimeError("evolution state log is empty")
        return _EvolutionState.model_validate(last["state"])

    def _append(self, action: str, state: _EvolutionState) -> None:
        record = {
            "action": action,
            "recorded_at": utc_now().isoformat(),
            "state": state.model_dump(mode="json"),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
