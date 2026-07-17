from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import AliasChoices, BaseModel, Field, model_validator


class ContextRepresentation(StrEnum):
    INLINE = "inline"
    REF = "ref"
    REFERENCE = "ref"
    SUMMARY = "summary"
    DISCARD = "discard"


class ContextTransform(BaseModel):
    ref: str = Field(min_length=1)
    representation: ContextRepresentation
    reason: str = Field(min_length=1)


class ContextManifest(BaseModel):
    """Audit record for one compiled prompt; it is not model-visible context."""

    included_refs: list[str] = Field(default_factory=list)
    excluded_refs: list[str] = Field(default_factory=list)
    transform: list[ContextTransform] = Field(
        default_factory=list,
        validation_alias=AliasChoices("transform", "transforms"),
    )
    token_estimate: int = Field(ge=0)
    contract_version: int = Field(ge=1)
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def transforms(self) -> list[ContextTransform]:
        return self.transform

    @model_validator(mode="after")
    def validate_ref_accounting(self) -> ContextManifest:
        included = set(self.included_refs)
        excluded = set(self.excluded_refs)
        if overlap := included & excluded:
            raise ValueError(f"refs cannot be both included and excluded: {sorted(overlap)}")

        for change in self.transform:
            expected = excluded if change.representation is ContextRepresentation.DISCARD else included
            if change.ref not in expected:
                bucket = "excluded_refs" if change.representation is ContextRepresentation.DISCARD else "included_refs"
                raise ValueError(f"transform ref {change.ref!r} must appear in {bucket}")
        return self

    @classmethod
    def from_messages(
        cls,
        messages: Sequence[Mapping[str, str]],
        *,
        included_refs: list[str],
        excluded_refs: list[str],
        transform: list[ContextTransform],
        contract_version: int,
    ) -> ContextManifest:
        canonical = json.dumps(
            [dict(message) for message in messages],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            included_refs=included_refs,
            excluded_refs=excluded_refs,
            transform=transform,
            token_estimate=(len(canonical) + 3) // 4,
            contract_version=contract_version,
            prompt_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
