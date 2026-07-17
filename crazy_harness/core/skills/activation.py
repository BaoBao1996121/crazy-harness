from __future__ import annotations

from collections.abc import Collection
from typing import Any

from pydantic import ValidationError

from crazy_harness.core.events import Event
from crazy_harness.core.skills.loader import SkillCatalog
from crazy_harness.core.skills.models import SkillActivation, SkillError
from crazy_harness.core.tools import ToolRegistry, ToolResult, ToolSpec

SKILL_ACTIVATE_TOOL_NAME = "skill.activate"
_SUCCESS = {"ok", "success", "succeeded"}


class SkillActivationService:
    """Expose explicit Skill activation through the normal Tool trust boundary."""

    def __init__(
        self,
        catalog: SkillCatalog,
        *,
        allowed_names: Collection[str] | None = None,
    ) -> None:
        self.catalog = catalog
        catalog_names = frozenset(catalog.names())
        self.allowed_names = (
            catalog_names
            if allowed_names is None
            else catalog_names & frozenset(allowed_names)
        )

    def tool_spec(self) -> ToolSpec:
        return ToolSpec(
            name=SKILL_ACTIVATE_TOOL_NAME,
            description=(
                "Load the full instructions for one catalogued Skill. The Skill may guide "
                "reasoning but never grants Tool permissions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": sorted(self.allowed_names),
                        "description": "Skill name from runtime_manifest.skill_catalog.",
                    }
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            side_effect_level="none",
            output_offload_policy="inline",
            is_read_only=True,
            is_concurrency_safe=True,
        )

    def install(self, registry: ToolRegistry) -> None:
        if registry.has(SKILL_ACTIVATE_TOOL_NAME):
            raise ValueError(f"reserved Skill tool is already registered: {SKILL_ACTIVATE_TOOL_NAME}")
        registry.register(self.tool_spec(), self.handle)

    def handle(self, args: dict[str, Any]) -> ToolResult:
        if set(args) != {"name"} or not isinstance(args.get("name"), str):
            return ToolResult(
                name=SKILL_ACTIVATE_TOOL_NAME,
                status="error",
                error="name is required and must be the only argument",
            )
        name = args["name"]
        if name not in self.allowed_names:
            return ToolResult(
                name=SKILL_ACTIVATE_TOOL_NAME,
                status="error",
                error=f"unknown or unauthorized Skill: {name}",
            )
        try:
            activation = self.catalog.activate(name)
        except (SkillError, OSError, UnicodeError) as exc:
            return ToolResult(name=SKILL_ACTIVATE_TOOL_NAME, status="error", error=str(exc))
        return ToolResult(
            name=SKILL_ACTIVATE_TOOL_NAME,
            status="ok",
            output=activation.model_dump_json(),
        )


def skill_activation_from_event(event: Event) -> SkillActivation | None:
    if event.type != "tool.completed":
        return None
    result = event.payload.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("name") != SKILL_ACTIVATE_TOOL_NAME:
        return None
    if str(result.get("status", "")).casefold() not in _SUCCESS:
        return None
    output = result.get("output")
    if not isinstance(output, str):
        return None
    try:
        return SkillActivation.model_validate_json(output)
    except (ValidationError, ValueError, TypeError):
        return None


def active_skill_activations(events: list[Event]) -> tuple[SkillActivation, ...]:
    latest: dict[str, SkillActivation] = {}
    for event in events:
        activation = skill_activation_from_event(event)
        if activation is not None:
            latest[activation.name] = activation
    return tuple(latest[name] for name in sorted(latest))
