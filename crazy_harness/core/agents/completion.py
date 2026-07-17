from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from crazy_harness.core.agents.contracts import AssignmentContract


class CompletionFindingCode(StrEnum):
    SCHEMA = "schema"
    EVIDENCE = "evidence"
    PENDING_OPERATION = "pending_operation"


class NudgeKind(StrEnum):
    SCHEMA = "schema"
    EVIDENCE = "evidence"
    PENDING_OPERATION = "pending_operation"
    NO_PROGRESS = "no_progress"


class NudgeBudget(BaseModel):
    """Independent remaining budgets for each repairable nudge reason."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_error: int = Field(default=1, ge=0, validation_alias=AliasChoices("schema_error", "schema"))
    missing_evidence: int = Field(default=1, ge=0, validation_alias=AliasChoices("missing_evidence", "evidence"))
    pending_operation: int = Field(default=1, ge=0)
    no_progress: int = Field(default=1, ge=0)
    _field_by_kind: ClassVar[dict[NudgeKind, str]] = {
        NudgeKind.SCHEMA: "schema_error",
        NudgeKind.EVIDENCE: "missing_evidence",
        NudgeKind.PENDING_OPERATION: "pending_operation",
        NudgeKind.NO_PROGRESS: "no_progress",
    }

    def remaining(self, kind: NudgeKind) -> int:
        return int(getattr(self, self._field_by_kind[NudgeKind(kind)]))

    def consume(self, kind: NudgeKind) -> bool:
        typed_kind = NudgeKind(kind)
        remaining = self.remaining(typed_kind)
        if remaining == 0:
            return False
        setattr(self, self._field_by_kind[typed_kind], remaining - 1)
        return True


class CompletionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: CompletionFindingCode
    message: str
    path: str | None = None


class CompletionGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    findings: tuple[CompletionFinding, ...] = ()


class ProgressAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_fingerprint: str
    repeated_action: bool
    evidence_delta: tuple[str, ...]
    made_progress: bool


class ProgressDetector:
    """Detect evidence progress and repeated structured actions across observations."""

    def __init__(self) -> None:
        self._seen_action_fingerprints: set[str] = set()
        self._seen_evidence_refs: set[str] = set()

    def observe(self, action: Any, *, evidence_refs: Collection[str] = ()) -> ProgressAssessment:
        fingerprint = self.fingerprint(action)
        repeated = fingerprint in self._seen_action_fingerprints
        evidence = tuple(dict.fromkeys(ref.strip() for ref in evidence_refs if ref.strip()))
        delta = tuple(ref for ref in evidence if ref not in self._seen_evidence_refs)

        self._seen_action_fingerprints.add(fingerprint)
        self._seen_evidence_refs.update(evidence)
        return ProgressAssessment(
            action_fingerprint=fingerprint,
            repeated_action=repeated,
            evidence_delta=delta,
            made_progress=bool(delta),
        )

    @staticmethod
    def fingerprint(action: Any) -> str:
        payload = action.model_dump(mode="json", exclude_none=True) if isinstance(action, BaseModel) else action
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CompletionGate:
    """Mechanical pre-review gate; passing it does not complete the assignment."""

    def evaluate(
        self,
        contract: AssignmentContract,
        *,
        output: Any,
        evidence: Mapping[str, Collection[str] | str] | None = None,
        pending_operations: Collection[str] = (),
    ) -> CompletionGateResult:
        if isinstance(output, BaseModel):
            output = output.model_dump(mode="json")

        findings = [
            CompletionFinding(code=CompletionFindingCode.SCHEMA, message=message, path=path)
            for path, message in _schema_errors(contract.output_schema, output)
        ]
        supplied_evidence = evidence or {}
        for requirement in contract.evidence_requirements:
            if not _has_evidence(supplied_evidence.get(requirement)):
                findings.append(
                    CompletionFinding(
                        code=CompletionFindingCode.EVIDENCE,
                        message=f"missing evidence for {requirement}",
                        path=requirement,
                    )
                )

        pending = tuple(pending_operations)
        if pending:
            findings.append(
                CompletionFinding(
                    code=CompletionFindingCode.PENDING_OPERATION,
                    message=f"pending operations: {', '.join(pending)}",
                )
            )
        return CompletionGateResult(passed=not findings, findings=tuple(findings))


def _has_evidence(refs: Collection[str] | str | None) -> bool:
    if isinstance(refs, str):
        return bool(refs.strip())
    return refs is not None and any(isinstance(ref, str) and ref.strip() for ref in refs)


def _schema_errors(schema: Mapping[str, Any], value: Any, path: str = "$") -> list[tuple[str, str]]:
    if not schema:
        return [(path, "output schema is missing")]

    expected = schema.get("type")
    expected_types = [expected] if isinstance(expected, str) else expected
    if expected_types and not any(_matches_json_type(value, item) for item in expected_types):
        return [(path, f"expected {expected}")]
    if "enum" in schema and value not in schema["enum"]:
        return [(path, "value is not in the allowed enum")]

    errors: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                errors.append((f"{path}.{name}", "required field is missing"))
        for name, item in value.items():
            child_schema = properties.get(name)
            if isinstance(child_schema, Mapping):
                errors.extend(_schema_errors(child_schema, item, f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append((f"{path}.{name}", "additional field is not allowed"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item_schema, item, f"{path}[{index}]"))
    return errors


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False
