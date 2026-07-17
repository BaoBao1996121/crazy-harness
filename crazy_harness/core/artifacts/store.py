from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from crazy_harness.core.artifacts.schemas import ArtifactRef


class ArtifactStore:
    """Filesystem-backed artifact store."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def write_json(self, kind: str, value: BaseModel | dict[str, Any], summary: str = "") -> ArtifactRef:
        path = self.root / f"{kind}_{uuid4().hex}.json"
        data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return ArtifactRef(uri=str(path), kind=kind, summary=summary)

    def write_text(self, kind: str, text: str, summary: str = "") -> ArtifactRef:
        path = self.root / f"{kind}_{uuid4().hex}.txt"
        path.write_text(text, encoding="utf-8")
        return ArtifactRef(uri=str(path), kind=kind, summary=summary)

    def read_text(self, ref: ArtifactRef) -> str:
        return Path(ref.uri).read_text(encoding="utf-8")
