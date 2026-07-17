from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.core.capabilities.catalog import CapabilityCatalog, CapabilityDefinition


class DisclosureStrategy(StrEnum):
    INLINE_ALL = "inline_all"
    SEARCH_RANKED = "search_ranked"


class CapabilityCompileRequest(BaseModel):
    """Deterministic inputs used to compile the model-visible capability set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    query: str = ""
    allowed_names: frozenset[str] = Field(default_factory=frozenset)
    always_include: tuple[str, ...] = ()
    explicit_names: tuple[str, ...] = ()
    explicit_sources: dict[str, str] = Field(default_factory=dict)


class CapabilityManifest(BaseModel):
    """Durable proof of what the model could and could not see for one turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 2
    agent_id: str
    assignment_id: str
    mode: str
    strategy: DisclosureStrategy
    query: str
    catalog_size: int
    authorized_names: tuple[str, ...]
    disclosed_names: tuple[str, ...]
    withheld_names: tuple[str, ...]
    excluded_names: tuple[str, ...]
    reasons: dict[str, str]
    recall_sources: dict[str, str]
    kinds: dict[str, str] = Field(default_factory=dict)
    providers: dict[str, str] = Field(default_factory=dict)
    definition_hashes: dict[str, str]
    manifest_hash: str


@dataclass(frozen=True)
class CompiledCapabilities:
    definitions: tuple[CapabilityDefinition, ...]
    manifest: CapabilityManifest


class CapabilityCompiler:
    """Authority-first, hybrid disclosure for native tools, Skills, and MCP capabilities."""

    def __init__(
        self,
        catalog: CapabilityCatalog,
        *,
        inline_limit: int = 12,
        search_limit: int = 6,
    ) -> None:
        if inline_limit < 0:
            raise ValueError("inline_limit must be non-negative")
        if search_limit < 1:
            raise ValueError("search_limit must be positive")
        self.catalog = catalog
        self.inline_limit = inline_limit
        self.search_limit = search_limit

    def compile(self, request: CapabilityCompileRequest) -> CompiledCapabilities:
        catalog_names = tuple(sorted(stub.name for stub in self.catalog.stubs()))
        authorized = tuple(name for name in catalog_names if name in request.allowed_names)
        excluded = tuple(name for name in catalog_names if name not in request.allowed_names)
        reasons = {name: "policy_denied" for name in excluded}
        recall_sources: dict[str, str] = {}

        if len(authorized) <= self.inline_limit:
            strategy = DisclosureStrategy.INLINE_ALL
            disclosed = authorized
            reasons.update({name: "small_authorized_catalog" for name in disclosed})
        else:
            strategy = DisclosureStrategy.SEARCH_RANKED
            selected: dict[str, str] = {}
            authorized_set = frozenset(authorized)
            for name in request.always_include:
                if name in authorized_set:
                    selected[name] = "always_include"
            for name in request.explicit_names:
                if name in authorized_set:
                    selected[name] = "explicit_recall"
                    source = request.explicit_sources.get(name)
                    if source:
                        recall_sources[name] = source

            remaining = authorized_set - selected.keys()
            for match in self.catalog.search(
                request.query,
                limit=self.search_limit,
                names=remaining,
            ):
                selected.setdefault(match.name, "query_match")

            # An empty or unmatched query must still expose a deterministic starter set.
            if not selected:
                for name in authorized[: self.search_limit]:
                    selected[name] = "deterministic_fallback"
            disclosed = tuple(sorted(selected))
            reasons.update(selected)

        withheld = tuple(name for name in authorized if name not in disclosed)
        reasons.update({name: "not_selected" for name in withheld})
        definitions = tuple(self.catalog.disclose(list(disclosed)))
        definition_hashes = {
            definition.name: _stable_hash(definition.model_dump(mode="json"))
            for definition in definitions
        }
        kinds = {definition.name: definition.kind.value for definition in definitions}
        providers = {definition.name: definition.provider for definition in definitions}
        manifest_data = {
            "version": 2,
            "agent_id": request.agent_id,
            "assignment_id": request.assignment_id,
            "mode": request.mode,
            "strategy": strategy,
            "query": request.query,
            "catalog_size": len(catalog_names),
            "authorized_names": authorized,
            "disclosed_names": disclosed,
            "withheld_names": withheld,
            "excluded_names": excluded,
            "reasons": dict(sorted(reasons.items())),
            "recall_sources": dict(sorted(recall_sources.items())),
            "kinds": dict(sorted(kinds.items())),
            "providers": dict(sorted(providers.items())),
            "definition_hashes": dict(sorted(definition_hashes.items())),
        }
        manifest = CapabilityManifest(
            **manifest_data,
            manifest_hash=_stable_hash(manifest_data),
        )
        return CompiledCapabilities(definitions=definitions, manifest=manifest)


def _stable_hash(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
