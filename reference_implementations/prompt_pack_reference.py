from __future__ import annotations

import hashlib
import json


def compile_messages(self) -> list[dict[str, str]]:
    sections = [
        ("ROLE", self.role_section),
        ("AGENT CARD", self.agent_card_section),
        ("TASK BRIEF", self.task_brief_section),
        ("RUNTIME MANIFEST", self.runtime_manifest_section),
        ("CONTEXT POLICY", self.context_policy_section),
        ("TOOL POLICY", self.tool_policy_section),
        ("ARTIFACT SCHEMA", self.artifact_schema_section),
    ]
    content = "\n\n".join(f"## {title}\n{body.strip()}" for title, body in sections)
    return [{"role": "system", "content": content}]


def prompt_hash(self) -> str:
    messages = self.compile_messages()
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
