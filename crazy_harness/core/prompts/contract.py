from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from crazy_harness.core.models import ModelMessage
from crazy_harness.core.skills.models import SkillCatalogEntry
from crazy_harness.core.tools import ToolSpec


class RuntimeManifest(BaseModel):
    """HANDWRITE_TODO: runtime capability contract injected before model calls."""

    agent_id: str
    task_id: str
    mode: str
    available_tools: list[ToolSpec] = Field(default_factory=list)
    available_skills: list[str] = Field(default_factory=list)
    skill_catalog: list[SkillCatalogEntry] = Field(default_factory=list)
    workspace_policy: dict[str, Any] = Field(default_factory=dict)
    network_policy: dict[str, Any] = Field(default_factory=dict)
    artifact_store_policy: dict[str, Any] = Field(default_factory=dict)
    model_profile: dict[str, Any] = Field(default_factory=dict)


class PromptPack(BaseModel):
    """Versioned prompt contract with stable, latest-only sections."""

    prompt_version: str = "mvp-0"
    role_section: str
    agent_card_section: str
    task_brief_section: str
    runtime_manifest: RuntimeManifest
    context_policy_section: str = ""
    tool_policy_section: str = ""
    communication_policy_section: str = ""
    artifact_schema_section: str = ""
    context_view: list[str] = Field(default_factory=list)

    def compile(self) -> tuple[list[ModelMessage], str]:
        manifest_json = self.runtime_manifest.model_dump_json(indent=2)
        sections = [
            ("Prompt Version", self.prompt_version),
            ("Role", self.role_section),
            ("Agent Card", self.agent_card_section),
            ("Runtime Manifest", manifest_json),
            ("Context Policy", self.context_policy_section),
            ("Tool Policy", self.tool_policy_section),
            ("Communication Policy", self.communication_policy_section),
            ("Artifact Schema", self.artifact_schema_section),
        ]
        system = "\n\n".join(f"## {name}\n{content}" for name, content in sections if content)
        user_parts = [*self.context_view, self.task_brief_section]
        messages = [
            ModelMessage(role="system", content=system),
            ModelMessage(role="user", content="\n\n".join(part for part in user_parts if part)),
        ]
        canonical = json.dumps(
            [message.model_dump(mode="json") for message in messages],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return messages, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
