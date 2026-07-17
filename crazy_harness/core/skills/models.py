from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillScope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"
    AGENT = "agent"


class SkillSource(BaseModel):
    """One explicitly configured and trust-classified Skill root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=128)
    root: Path
    scope: SkillScope
    trusted: bool
    agent_id: str | None = None
    priority: int = Field(default=0, ge=-1000, le=1000)

    @model_validator(mode="after")
    def validate_agent_scope(self) -> "SkillSource":
        if self.scope is SkillScope.AGENT and not self.agent_id:
            raise ValueError("agent_id is required for agent-scoped Skill sources")
        if self.scope is not SkillScope.AGENT and self.agent_id is not None:
            raise ValueError("agent_id is only valid for agent-scoped Skill sources")
        return self


class SkillMetadata(BaseModel):
    """Agent Skills frontmatter fields used by the runtime."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    license: str | None = None
    compatibility: str | None = Field(default=None, max_length=500)
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools_hint: tuple[str, ...] = Field(default=(), alias="allowed-tools")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SKILL_NAME.fullmatch(value):
            raise ValueError("name must use lowercase letters, digits, and single hyphens")
        return value

    @field_validator("allowed_tools_hint", mode="before")
    @classmethod
    def normalize_allowed_tools(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return tuple(part for part in value.split() if part)
        if isinstance(value, (list, tuple)) and all(isinstance(part, str) for part in value):
            return tuple(part for part in value if part)
        raise ValueError("allowed-tools must be a string or list of strings")


class SkillCatalogEntry(BaseModel):
    """Small model-visible stub; the instruction body is deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    scope: SkillScope
    source_id: str


class SkillActivation(BaseModel):
    """Durable result returned only after an explicit activation tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    name: str
    description: str
    scope: SkillScope
    source_id: str
    body: str
    source_hash: str = Field(min_length=64, max_length=64)
    body_hash: str = Field(min_length=64, max_length=64)
    resources: tuple[str, ...] = ()
    allowed_tools_hint: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_body_hash(self) -> "SkillActivation":
        actual = hashlib.sha256(self.body.encode("utf-8")).hexdigest()
        if actual != self.body_hash:
            raise ValueError("Skill activation body_hash does not match body")
        return self


class SkillDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: Literal["info", "warning", "error"]
    source_id: str
    skill_name: str | None = None
    message: str


class SkillError(RuntimeError):
    pass


class SkillNotFoundError(SkillError):
    pass


class SkillChangedError(SkillError):
    pass


class SkillValidationError(SkillError):
    pass
