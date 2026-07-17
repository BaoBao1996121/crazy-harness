from __future__ import annotations

import hashlib
import json

from crazy_harness.core.models import ModelMessage


def compile(self):
    manifest_json = self.runtime_manifest.model_dump_json(indent=2)
    system = "\n\n".join(
        [
            f"Prompt version: {self.prompt_version}",
            "## Role\n" + self.role_section,
            "## Agent Card\n" + self.agent_card_section,
            "## Runtime Manifest\n" + manifest_json,
            "## Context Policy\n" + self.context_policy_section,
            "## Tool Policy\n" + self.tool_policy_section,
            "## Communication Policy\n" + self.communication_policy_section,
            "## Artifact Schema\n" + self.artifact_schema_section,
        ]
    )
    user = "\n".join(self.context_view + [self.task_brief_section])
    messages = [ModelMessage(role="system", content=system), ModelMessage(role="user", content=user)]
    payload = json.dumps([m.model_dump() for m in messages], ensure_ascii=False, sort_keys=True)
    return messages, hashlib.sha256(payload.encode("utf-8")).hexdigest()
