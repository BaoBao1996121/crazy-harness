from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    reason: str = Field(min_length=1, max_length=500)


class RunPauseRequest(_ControlRequest):
    pass


class RunResumeRequest(_ControlRequest):
    pass


class RunNudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    message: str = Field(min_length=1, max_length=2000)


class RunNudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    request_id: str
    status: Literal["pending_next_turn"] = "pending_next_turn"
    nudge_event_id: str
    supersedes_event_id: str | None = None


class RunForkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    label: str = Field(default="", max_length=200)


class AgentRunBranchView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    parent_run_id: str | None = None
    checkpoint_id: str | None = None
    source_event_id: str | None = None
    source_turn_id: str | None = None
    source_phase: str | None = None
    children_run_ids: tuple[str, ...] = ()


class RunControlResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    request_id: str
    action: Literal["pause", "resume"]
    status: Literal["pausing", "paused", "running"]
    request_event_id: str
    applied_event_id: str | None = None
