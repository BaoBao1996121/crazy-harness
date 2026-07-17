from __future__ import annotations

from collections.abc import Collection
from typing import Any

from pydantic import BaseModel, ConfigDict

from crazy_harness.core.capabilities.catalog import (
    CapabilityCatalog,
    CapabilityKind,
)
from crazy_harness.core.tools import ToolRegistry, ToolResult, ToolSpec

CAPABILITY_SEARCH_TOOL_NAME = "capability.search"


class CapabilitySearchHit(BaseModel):
    """Short metadata returned before a full capability schema is disclosed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: CapabilityKind
    description: str
    tags: tuple[str, ...] = ()


class CapabilitySearchResult(BaseModel):
    """Schema-bound, authority-scoped output of one capability search."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    total_authorized: int
    matches: tuple[CapabilitySearchHit, ...] = ()


class CapabilitySearchService:
    """Expose catalog search as a normal read-only tool with no execution bypass."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        allowed_names: Collection[str],
        max_results: int = 6,
    ) -> None:
        if max_results < 1:
            raise ValueError("max_results must be positive")
        self.catalog = catalog
        self.allowed_names = frozenset(allowed_names)
        self.max_results = max_results

    def tool_spec(self) -> ToolSpec:
        return ToolSpec(
            name=CAPABILITY_SEARCH_TOOL_NAME,
            description=(
                "Search additional capabilities authorized for this assignment. "
                "Returns short metadata only; matched full schemas become available "
                "in the next model turn."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                        "description": "Keywords describing the capability needed.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.max_results,
                        "description": f"Maximum matches to return; default {self.max_results}.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            side_effect_level="none",
            output_offload_policy="inline",
            is_read_only=True,
            is_concurrency_safe=True,
        )

    def install(self, registry: ToolRegistry) -> None:
        if registry.has(CAPABILITY_SEARCH_TOOL_NAME):
            raise ValueError(
                f"reserved capability tool is already registered: {CAPABILITY_SEARCH_TOOL_NAME}"
            )
        registry.register(self.tool_spec(), self.handle)

    def handle(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(
                name=CAPABILITY_SEARCH_TOOL_NAME,
                status="error",
                error="query is required",
            )
        raw_limit = args.get("limit", self.max_results)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            return ToolResult(
                name=CAPABILITY_SEARCH_TOOL_NAME,
                status="error",
                error="limit must be an integer",
            )
        if raw_limit < 1 or raw_limit > self.max_results:
            return ToolResult(
                name=CAPABILITY_SEARCH_TOOL_NAME,
                status="error",
                error=f"limit must be between 1 and {self.max_results}",
            )

        catalog_names = frozenset(stub.name for stub in self.catalog.stubs())
        authorized = (catalog_names & self.allowed_names) - {
            CAPABILITY_SEARCH_TOOL_NAME
        }
        matches = self.catalog.search(
            query,
            limit=raw_limit,
            names=authorized,
        )
        result = CapabilitySearchResult(
            query=query,
            total_authorized=len(authorized),
            matches=tuple(
                CapabilitySearchHit(
                    name=match.name,
                    kind=match.kind,
                    description=match.description[:400],
                    tags=tuple(match.tags),
                )
                for match in matches
            ),
        )
        return ToolResult(
            name=CAPABILITY_SEARCH_TOOL_NAME,
            status="ok",
            output=result.model_dump_json(),
        )
