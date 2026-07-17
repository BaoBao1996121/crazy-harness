from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.core.tools.schemas import ToolCall

GrantKey = tuple[str, str, str]


class PolicyContext(BaseModel):
    """Authority attached to one agent assignment in one runtime mode."""

    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)
    approved_tools: frozenset[str] = Field(default_factory=frozenset)


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str
    approval_required: bool = False


class PolicyDenied(PermissionError):
    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason)


class ToolMetadataError(RuntimeError):
    pass


class ToolPolicy:
    """Fail-closed authority check; hooks cannot change its context."""

    def __init__(
        self,
        *,
        grants: Mapping[GrantKey, Collection[str]] | None = None,
        destructive_modes: Collection[str] = ("llm-live",),
    ) -> None:
        self._grants = (
            None if grants is None else {key: frozenset(tool_names) for key, tool_names in grants.items()}
        )
        self._destructive_modes = frozenset(destructive_modes)

    def evaluate(self, call: ToolCall, spec: Any, context: PolicyContext) -> PolicyDecision:
        if call.name != getattr(spec, "name", None):
            return self._deny("tool call does not match its registered specification")
        if call.name not in context.allowed_tools:
            return self._deny(
                f"tool {call.name!r} is not allowed for assignment {context.assignment_id!r}"
            )

        grant_key = (context.agent_id, context.assignment_id, context.mode)
        if self._grants is not None and call.name not in self._grants.get(grant_key, frozenset()):
            return self._deny(f"no policy grant for {grant_key!r} and tool {call.name!r}")

        constraints = (
            ("allowed_agents", context.agent_id, "agent"),
            ("allowed_assignments", context.assignment_id, "assignment"),
            ("allowed_modes", context.mode, "mode"),
        )
        for attribute, actual, label in constraints:
            allowed_values = getattr(spec, attribute, None)
            if allowed_values is not None and actual not in allowed_values:
                return self._deny(f"tool {call.name!r} is not allowed for {label} {actual!r}")

        try:
            destructive = metadata_flag(spec, "is_destructive", call.args)
            approval_required = destructive or metadata_flag(spec, "approval_required", call.args)
        except ToolMetadataError as exc:
            return self._deny(f"tool metadata evaluation failed closed: {exc}")
        if destructive and context.mode not in self._destructive_modes:
            return self._deny(
                f"destructive tool {call.name!r} is disabled in mode {context.mode!r}"
            )

        if approval_required and not self._has_approval(call.name, context):
            return self._deny(
                f"approval is required for tool {call.name!r} in assignment {context.assignment_id!r}",
                approval_required=True,
            )

        return PolicyDecision(allowed=True, reason="authorized", approval_required=approval_required)

    def require(self, call: ToolCall, spec: Any, context: PolicyContext) -> PolicyDecision:
        decision = self.evaluate(call, spec, context)
        if not decision.allowed:
            raise PolicyDenied(decision)
        return decision

    @staticmethod
    def _has_approval(tool_name: str, context: PolicyContext) -> bool:
        keys = {
            "*",
            tool_name,
            f"{context.assignment_id}:{tool_name}",
            f"{context.agent_id}:{context.assignment_id}:{tool_name}",
        }
        return bool(keys & context.approved_tools)

    @staticmethod
    def _deny(reason: str, *, approval_required: bool = False) -> PolicyDecision:
        return PolicyDecision(allowed=False, reason=reason, approval_required=approval_required)


def metadata_flag(spec: Any, attribute: str, args: dict[str, Any]) -> bool:
    """Read provisional ToolSpec metadata without requiring schema changes."""

    value = getattr(spec, attribute, False)
    try:
        if callable(value):
            value = value(args)
        return bool(value)
    except Exception as exc:
        raise ToolMetadataError(f"{attribute}: {exc}") from exc
