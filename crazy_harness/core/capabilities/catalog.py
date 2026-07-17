from __future__ import annotations

import re
from collections.abc import Collection
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from crazy_harness.core.tools.schemas import ToolSpec


class CapabilityKind(StrEnum):
    SKILL = "skill"
    FUNCTION = "function"
    MCP = "mcp"


class CapabilityStub(BaseModel):
    name: str
    kind: CapabilityKind
    description: str
    tags: list[str] = Field(default_factory=list)


class CapabilityDefinition(CapabilityStub):
    input_schema: dict[str, Any] = Field(default_factory=dict)
    instructions: str = ""
    provider: str = "local"


class SkillDefinition(BaseModel):
    name: str
    description: str
    steps: list[str] = Field(default_factory=list)
    capability_aliases: list[str] = Field(default_factory=list)


class CapabilityCatalog:
    def __init__(self) -> None:
        self._definitions: dict[str, CapabilityDefinition] = {}

    def register(self, definition: CapabilityDefinition) -> None:
        self._definitions[definition.name] = definition

    def has(self, name: str) -> bool:
        return name in self._definitions

    def unregister(self, name: str) -> None:
        self._definitions.pop(name, None)

    @classmethod
    def from_tool_specs(
        cls,
        specs: Collection[ToolSpec],
        *,
        kind: CapabilityKind = CapabilityKind.FUNCTION,
        provider: str = "local",
    ) -> "CapabilityCatalog":
        catalog = cls()
        for spec in specs:
            tags = [spec.side_effect_level]
            if spec.is_read_only:
                tags.append("read_only")
            if spec.is_destructive:
                tags.append("destructive")
            instructions = " ".join(
                part
                for part in (
                    f"Use when: {spec.use_when}" if spec.use_when else "",
                    f"Do not use when: {spec.do_not_use_when}" if spec.do_not_use_when else "",
                )
                if part
            )
            catalog.register(
                CapabilityDefinition(
                    name=spec.name,
                    kind=kind,
                    description=spec.description,
                    tags=tags,
                    input_schema=spec.input_schema,
                    instructions=instructions,
                    provider=provider,
                )
            )
        return catalog

    def stubs(self) -> list[CapabilityStub]:
        return [
            CapabilityStub(name=item.name, kind=item.kind, description=item.description, tags=item.tags)
            for item in self._definitions.values()
        ]

    def disclose(self, names: list[str]) -> list[CapabilityDefinition]:
        return [self._definitions[name] for name in names if name in self._definitions]

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        names: Collection[str] | None = None,
    ) -> list[CapabilityStub]:
        terms = {term for term in re.findall(r"[a-zA-Z0-9_.-]+", query.lower()) if term}

        def score(item: CapabilityDefinition) -> tuple[int, str]:
            haystack = f"{item.name} {item.description} {item.instructions} {' '.join(item.tags)}".lower()
            return sum(1 for term in terms if term in haystack), item.name

        allowed = None if names is None else frozenset(names)
        candidates = [
            item
            for item in self._definitions.values()
            if allowed is None or item.name in allowed
        ]
        ranked = sorted(candidates, key=lambda item: (-score(item)[0], score(item)[1]))
        return [
            CapabilityStub(name=item.name, kind=item.kind, description=item.description, tags=item.tags)
            for item in ranked[:limit]
            if score(item)[0] > 0
        ]
