from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemorySlot(StrEnum):
    USER_CONSTRAINT = "user_constraint"
    WORLD_FACT = "world_fact"
    ACTIVE_CONCERN = "active_concern"
    EPISODE = "episode"
    PROCEDURE = "procedure"
    PREFERENCE = "preference"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class MemoryCandidate(BaseModel):
    candidate_id: str = Field(default_factory=lambda: f"memory_{uuid4().hex}")
    slot: MemorySlot
    content: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    expiry: AwareDatetime | None = None
    status: MemoryStatus = MemoryStatus.CANDIDATE
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    reviewed_by: str | None = None
    decision_reason: str = ""
    supersedes: str | None = None
    superseded_by: str | None = None

    def is_expired(self, at: datetime | None = None) -> bool:
        return self.expiry is not None and self.expiry <= (at or utc_now())
