from __future__ import annotations

from pydantic import BaseModel


class ArtifactRef(BaseModel):
    artifact_id: str
    path: str
    kind: str
    summary: str
