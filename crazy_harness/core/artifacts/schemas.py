from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ArtifactRef(BaseModel):
    uri: str
    kind: str
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
