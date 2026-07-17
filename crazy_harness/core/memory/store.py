from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from crazy_harness.core.memory.models import (
    MemoryCandidate,
    MemorySlot,
    MemoryStatus,
    utc_now,
)


class InvalidMemoryTransitionError(ValueError):
    pass


class MemoryConflictError(ValueError):
    def __init__(self, candidate_id: str, conflicting_ids: list[str]) -> None:
        self.candidate_id = candidate_id
        self.conflicting_ids = conflicting_ids
        joined = ", ".join(conflicting_ids)
        super().__init__(f"memory {candidate_id} conflicts with approved memory: {joined}")


class MemoryStore:
    """Append-only JSONL store for human-governed memory candidates."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def propose(self, candidate: MemoryCandidate) -> MemoryCandidate:
        if candidate.status is not MemoryStatus.CANDIDATE:
            raise InvalidMemoryTransitionError("new memory must start as a candidate")
        if candidate.candidate_id in self._load():
            raise ValueError(f"duplicate memory candidate: {candidate.candidate_id}")
        self._append("propose", candidate)
        return candidate

    def approve(self, candidate_id: str, *, reviewer: str, reason: str = "") -> MemoryCandidate:
        candidate = self._require(candidate_id)
        self._require_status(candidate, MemoryStatus.CANDIDATE)
        reviewer = self._require_reviewer(reviewer)
        conflicts = self._conflicts(candidate)
        if conflicts:
            raise MemoryConflictError(candidate_id, conflicts)
        approved = candidate.model_copy(
            update={
                "status": MemoryStatus.APPROVED,
                "reviewed_by": reviewer,
                "decision_reason": reason,
                "updated_at": utc_now(),
            }
        )
        self._append("approve", approved)
        return approved

    def reject(self, candidate_id: str, *, reviewer: str, reason: str = "") -> MemoryCandidate:
        candidate = self._require(candidate_id)
        self._require_status(candidate, MemoryStatus.CANDIDATE)
        rejected = candidate.model_copy(
            update={
                "status": MemoryStatus.REJECTED,
                "reviewed_by": self._require_reviewer(reviewer),
                "decision_reason": reason,
                "updated_at": utc_now(),
            }
        )
        self._append("reject", rejected)
        return rejected

    def revoke(self, candidate_id: str, *, reviewer: str, reason: str = "") -> MemoryCandidate:
        candidate = self._require(candidate_id)
        self._require_status(candidate, MemoryStatus.APPROVED)
        revoked = candidate.model_copy(
            update={
                "status": MemoryStatus.REVOKED,
                "reviewed_by": self._require_reviewer(reviewer),
                "decision_reason": reason,
                "updated_at": utc_now(),
            }
        )
        self._append("revoke", revoked)
        return revoked

    def supersede(
        self,
        current_id: str,
        replacement_id: str,
        *,
        reviewer: str,
        reason: str = "",
    ) -> MemoryCandidate:
        current = self._require(current_id)
        replacement = self._require(replacement_id)
        self._require_status(current, MemoryStatus.APPROVED)
        self._require_status(replacement, MemoryStatus.CANDIDATE)
        reviewer = self._require_reviewer(reviewer)
        if (current.slot, current.scope) != (replacement.slot, replacement.scope):
            raise ValueError("replacement must use the same memory slot and scope")
        if replacement.version <= current.version:
            raise ValueError("replacement version must be newer than the current memory")
        conflicts = self._conflicts(replacement, exclude={current_id})
        if conflicts:
            raise MemoryConflictError(replacement_id, conflicts)
        now = utc_now()
        superseded = current.model_copy(
            update={
                "status": MemoryStatus.SUPERSEDED,
                "reviewed_by": reviewer,
                "decision_reason": reason,
                "superseded_by": replacement_id,
                "updated_at": now,
            }
        )
        promoted = replacement.model_copy(
            update={
                "status": MemoryStatus.APPROVED,
                "reviewed_by": reviewer,
                "decision_reason": reason,
                "supersedes": current_id,
                "updated_at": now,
            }
        )
        self._append("supersede", superseded, promoted)
        return promoted

    def get(self, candidate_id: str, *, at: datetime | None = None) -> MemoryCandidate:
        candidate = self._require(candidate_id)
        if candidate.status is MemoryStatus.APPROVED and candidate.is_expired(at):
            return candidate.model_copy(update={"status": MemoryStatus.EXPIRED})
        return candidate

    def recall(
        self,
        *,
        scope: str,
        slot: MemorySlot | None = None,
        at: datetime | None = None,
    ) -> list[MemoryCandidate]:
        recalled = [
            candidate
            for candidate in self._load().values()
            if candidate.status is MemoryStatus.APPROVED
            and not candidate.is_expired(at)
            and candidate.scope in {scope, "*"}
            and (slot is None or candidate.slot is slot)
        ]
        return sorted(
            recalled,
            key=lambda candidate: (candidate.created_at, candidate.candidate_id),
        )

    def _load(self) -> dict[str, MemoryCandidate]:
        candidates: dict[str, MemoryCandidate] = {}
        if not self.path.exists():
            return candidates
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                for data in record["candidates"]:
                    candidate = MemoryCandidate.model_validate(data)
                    candidates[candidate.candidate_id] = candidate
        return candidates

    def _append(self, action: str, *candidates: MemoryCandidate) -> None:
        record = {
            "action": action,
            "recorded_at": utc_now().isoformat(),
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _require(self, candidate_id: str) -> MemoryCandidate:
        try:
            return self._load()[candidate_id]
        except KeyError as exc:
            raise KeyError(f"unknown memory candidate: {candidate_id}") from exc

    def _conflicts(
        self,
        candidate: MemoryCandidate,
        *,
        exclude: set[str] | None = None,
    ) -> list[str]:
        excluded = exclude or set()
        return [
            existing.candidate_id
            for existing in self._load().values()
            if existing.candidate_id not in excluded
            and existing.status is MemoryStatus.APPROVED
            and not existing.is_expired()
            and existing.slot is candidate.slot
            and existing.scope == candidate.scope
        ]

    @staticmethod
    def _require_status(candidate: MemoryCandidate, expected: MemoryStatus) -> None:
        if candidate.status is not expected:
            raise InvalidMemoryTransitionError(
                f"memory {candidate.candidate_id} is {candidate.status}, expected {expected}"
            )

    @staticmethod
    def _require_reviewer(reviewer: str) -> str:
        if not reviewer.strip():
            raise ValueError("a human reviewer is required")
        return reviewer.strip()
