from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError
from yaml import YAMLError, safe_load

from crazy_harness.core.skills.models import (
    SkillActivation,
    SkillCatalogEntry,
    SkillChangedError,
    SkillDiagnostic,
    SkillMetadata,
    SkillNotFoundError,
    SkillScope,
    SkillSource,
    SkillValidationError,
)

_SCOPE_RANK = {
    SkillScope.GLOBAL: 1,
    SkillScope.PROJECT: 2,
    SkillScope.AGENT: 3,
}
_RESOURCE_DIRS = ("scripts", "references", "assets")


@dataclass(frozen=True)
class _SkillRecord:
    metadata: SkillMetadata
    entry: SkillCatalogEntry
    source: SkillSource
    skill_file: Path
    source_hash: str


class SkillCatalog:
    """Resolved Skill stubs plus private file records used for explicit activation."""

    def __init__(
        self,
        records: dict[str, _SkillRecord],
        diagnostics: Sequence[SkillDiagnostic],
        *,
        max_skill_bytes: int,
        max_resources: int,
    ) -> None:
        self._records = dict(records)
        self.diagnostics = tuple(diagnostics)
        self.max_skill_bytes = max_skill_bytes
        self.max_resources = max_resources

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._records))

    def entries(self) -> tuple[SkillCatalogEntry, ...]:
        return tuple(self._records[name].entry for name in self.names())

    def select(self, names: Sequence[str]) -> "SkillCatalog":
        """Return a task-specific catalog without exposing unrelated Skill metadata."""

        requested = tuple(dict.fromkeys(names))
        missing = tuple(name for name in requested if name not in self._records)
        if missing:
            joined = ", ".join(repr(name) for name in missing)
            raise SkillNotFoundError(f"unknown or unauthorized Skill: {joined}")
        requested_set = set(requested)
        diagnostics = tuple(
            item
            for item in self.diagnostics
            if item.skill_name is None or item.skill_name in requested_set
        )
        return SkillCatalog(
            {name: self._records[name] for name in requested},
            diagnostics,
            max_skill_bytes=self.max_skill_bytes,
            max_resources=self.max_resources,
        )

    def audit_manifest(self) -> dict[str, object]:
        """Return body-free, stable evidence of the resolved catalog and diagnostics."""

        data: dict[str, object] = {
            "version": 1,
            "entries": [entry.model_dump(mode="json") for entry in self.entries()],
            "diagnostics": [item.model_dump(mode="json") for item in self.diagnostics],
            "source_hashes": {
                name: self._records[name].source_hash for name in self.names()
            },
        }
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {**data, "manifest_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}

    def activate(self, name: str) -> SkillActivation:
        record = self._records.get(name)
        if record is None:
            raise SkillNotFoundError(f"unknown or unauthorized Skill: {name}")
        raw = _read_bounded(record.skill_file, self.max_skill_bytes)
        source_hash = _skill_source_hash(raw)
        if source_hash != record.source_hash:
            raise SkillChangedError(
                f"Skill {name!r} changed after discovery; refresh the catalog before activation"
            )
        metadata, body = _parse_skill(record.skill_file, raw)
        if metadata != record.metadata:
            raise SkillChangedError(
                f"Skill {name!r} metadata changed after discovery; refresh the catalog"
            )
        resources = _list_resources(record.skill_file.parent, limit=self.max_resources)
        return SkillActivation(
            name=metadata.name,
            description=metadata.description,
            scope=record.source.scope,
            source_id=record.source.source_id,
            body=body,
            source_hash=source_hash,
            body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            resources=resources,
            allowed_tools_hint=metadata.allowed_tools_hint,
        )


class SkillLoaderPort(Protocol):
    def discover(self, sources: Sequence[SkillSource], *, agent_id: str) -> SkillCatalog:
        ...


class FileSystemSkillLoader:
    """Discover immediate-child Agent Skills without disclosing their bodies."""

    def __init__(
        self,
        *,
        max_skills: int = 2000,
        max_skill_bytes: int = 1_000_000,
        max_resources: int = 200,
        recommended_body_lines: int = 500,
    ) -> None:
        if min(max_skills, max_skill_bytes, max_resources, recommended_body_lines) < 1:
            raise ValueError("Skill loader limits must be positive")
        self.max_skills = max_skills
        self.max_skill_bytes = max_skill_bytes
        self.max_resources = max_resources
        self.recommended_body_lines = recommended_body_lines

    def discover(self, sources: Sequence[SkillSource], *, agent_id: str) -> SkillCatalog:
        _validate_unique_source_ids(sources)
        candidates: dict[str, list[_SkillRecord]] = defaultdict(list)
        diagnostics: list[SkillDiagnostic] = []
        scanned = 0
        catalog_limit_reached = False

        for source in sorted(sources, key=lambda item: item.source_id):
            if not source.trusted:
                diagnostics.append(
                    _diagnostic(
                        "source_untrusted",
                        "error",
                        source,
                        "source was rejected before Skill metadata disclosure",
                    )
                )
                continue
            if source.scope is SkillScope.AGENT and source.agent_id != agent_id:
                diagnostics.append(
                    _diagnostic(
                        "source_agent_mismatch",
                        "warning",
                        source,
                        f"source belongs to agent {source.agent_id!r}, not {agent_id!r}",
                    )
                )
                continue
            root = source.root.resolve()
            if not root.is_dir():
                diagnostics.append(
                    _diagnostic("source_missing", "info", source, "Skill root does not exist")
                )
                continue

            for child in sorted(root.iterdir(), key=lambda item: item.name):
                skill_file = child / "SKILL.md"
                if not child.is_dir() or not skill_file.is_file():
                    continue
                if scanned >= self.max_skills:
                    diagnostics.append(
                        _diagnostic(
                            "catalog_limit_reached",
                            "error",
                            source,
                            f"stopped after {self.max_skills} Skill files",
                        )
                    )
                    catalog_limit_reached = True
                    break
                scanned += 1
                resolved = skill_file.resolve()
                if not resolved.is_relative_to(root):
                    diagnostics.append(
                        _diagnostic(
                            "skill_path_escape",
                            "error",
                            source,
                            "SKILL.md resolves outside its configured root",
                            child.name,
                        )
                    )
                    continue
                try:
                    raw = _read_bounded(resolved, self.max_skill_bytes)
                    metadata, body = _parse_skill(resolved, raw)
                except (OSError, UnicodeError, YAMLError, ValidationError, SkillValidationError) as exc:
                    diagnostics.append(
                        _diagnostic("skill_invalid", "error", source, str(exc), child.name)
                    )
                    continue
                if len(body.splitlines()) > self.recommended_body_lines:
                    diagnostics.append(
                        _diagnostic(
                            "skill_body_large",
                            "warning",
                            source,
                            f"body exceeds the recommended {self.recommended_body_lines} lines",
                            metadata.name,
                        )
                    )
                entry = SkillCatalogEntry(
                    name=metadata.name,
                    description=metadata.description,
                    scope=source.scope,
                    source_id=source.source_id,
                )
                candidates[metadata.name].append(
                    _SkillRecord(
                        metadata=metadata,
                        entry=entry,
                        source=source,
                        skill_file=resolved,
                        source_hash=_skill_source_hash(raw),
                    )
                )
            if catalog_limit_reached:
                break

        winners: dict[str, _SkillRecord] = {}
        for name, records in sorted(candidates.items()):
            winner = max(records, key=_precedence_key)
            winners[name] = winner
            for loser in records:
                if loser is winner:
                    continue
                diagnostics.append(
                    _diagnostic(
                        "skill_shadowed",
                        "warning",
                        loser.source,
                        f"shadowed by source {winner.source.source_id!r} ({winner.source.scope.value})",
                        name,
                    )
                )
        return SkillCatalog(
            winners,
            diagnostics,
            max_skill_bytes=self.max_skill_bytes,
            max_resources=self.max_resources,
        )


def _precedence_key(record: _SkillRecord) -> tuple[int, int, str]:
    source = record.source
    return _SCOPE_RANK[source.scope], source.priority, source.source_id


def _validate_unique_source_ids(sources: Sequence[SkillSource]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for source in sources:
        if source.source_id in seen:
            duplicates.add(source.source_id)
        seen.add(source.source_id)
    if duplicates:
        joined = ", ".join(repr(source_id) for source_id in sorted(duplicates))
        raise ValueError(f"duplicate Skill source_id: {joined}")


def _read_bounded(path: Path, limit: int) -> bytes:
    size = path.stat().st_size
    if size > limit:
        raise SkillValidationError(f"SKILL.md is {size} bytes; limit is {limit}")
    return path.read_bytes()


def _parse_skill(path: Path, raw: bytes) -> tuple[SkillMetadata, str]:
    text = _normalize_newlines(raw.decode("utf-8-sig"))
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillValidationError("SKILL.md must start with YAML frontmatter")
    end = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end is None:
        raise SkillValidationError("SKILL.md frontmatter is not closed")
    frontmatter = safe_load("".join(lines[1:end]))
    if not isinstance(frontmatter, dict):
        raise SkillValidationError("SKILL.md frontmatter must be a mapping")
    metadata = SkillMetadata.model_validate(frontmatter)
    body = "".join(lines[end + 1 :]).lstrip("\r\n")
    if metadata.name != path.parent.name:
        raise SkillValidationError(
            f"frontmatter name {metadata.name!r} must match directory {path.parent.name!r}"
        )
    return metadata, body


def _skill_source_hash(raw: bytes) -> str:
    canonical = _normalize_newlines(raw.decode("utf-8-sig")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _list_resources(skill_root: Path, *, limit: int) -> tuple[str, ...]:
    resolved_root = skill_root.resolve()
    resources: list[str] = []
    for directory_name in _RESOURCE_DIRS:
        directory = skill_root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(resolved_root):
                raise SkillValidationError(f"resource escapes Skill directory: {path}")
            resources.append(resolved.relative_to(resolved_root).as_posix())
            if len(resources) > limit:
                raise SkillValidationError(f"Skill resource count exceeds limit {limit}")
    return tuple(resources)


def _diagnostic(
    code: str,
    severity: Literal["info", "warning", "error"],
    source: SkillSource,
    message: str,
    skill_name: str | None = None,
) -> SkillDiagnostic:
    return SkillDiagnostic(
        code=code,
        severity=severity,
        source_id=source.source_id,
        skill_name=skill_name,
        message=message,
    )
