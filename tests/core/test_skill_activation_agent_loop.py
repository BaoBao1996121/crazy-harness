from __future__ import annotations

import json

from crazy_harness.core.agents import AgentLoop
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.capabilities import (
    CapabilityCatalog,
    CapabilityCompiler,
    CapabilityDefinition,
    CapabilityKind,
)
from crazy_harness.core.context import ContextBuilder
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.models import ModelResponse
from crazy_harness.core.prompts import PromptPack, RuntimeManifest
from crazy_harness.core.skills import (
    SKILL_ACTIVATE_TOOL_NAME,
    FileSystemSkillLoader,
    SkillActivationService,
    SkillScope,
    SkillSource,
    active_skill_activations,
)
from crazy_harness.core.tools import ToolRegistry


class _RecordingModel:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = []
        self.tools = []

    def complete(self, messages, *, tools=None, response_schema=None):
        self.messages = messages
        self.tools = tools or []
        return ModelResponse(content=self.response)


def _make_loop(tmp_path, *, event_log, model, tools, skills):
    prompt = PromptPack(
        role_section="Repository maintainer",
        agent_card_section="Follow an activated Skill when one is present.",
        task_brief_section="Review repository evidence.",
        runtime_manifest=RuntimeManifest(
            agent_id="generalist",
            task_id="task-skill",
            mode="scripted",
            available_tools=tools.specs(),
            available_skills=list(skills.names()),
            skill_catalog=list(skills.entries()),
        ),
    )
    capability_catalog = CapabilityCatalog.from_tool_specs(tools.specs())
    activation_spec = tools.spec(SKILL_ACTIVATE_TOOL_NAME)
    capability_catalog.register(
        CapabilityDefinition(
            name=activation_spec.name,
            kind=CapabilityKind.SKILL,
            description=activation_spec.description,
            input_schema=activation_spec.input_schema,
            provider="local:skills",
        )
    )
    return AgentLoop(
        agent_id="generalist",
        task_id="task-skill",
        model=model,
        event_log=event_log,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        tool_registry=tools,
        context_builder=ContextBuilder(artifact_store=ArtifactStore(tmp_path / "context-artifacts")),
        prompt_pack=prompt,
        capability_compiler=CapabilityCompiler(capability_catalog),
    )


def test_skill_activation_is_recovered_into_one_protected_latest_only_slot(tmp_path):
    root = tmp_path / "project-skills"
    skill_dir = root / "repo-review"
    skill_dir.mkdir(parents=True)
    body_marker = "BODY_MARKER: inspect, test, then cite durable evidence."
    (skill_dir / "SKILL.md").write_text(
        "---\nname: repo-review\ndescription: Use for evidence-driven repository review.\n---\n\n"
        f"{body_marker}\n",
        encoding="utf-8",
    )
    skills = FileSystemSkillLoader().discover(
        [SkillSource(source_id="project", root=root, scope=SkillScope.PROJECT, trusted=True)],
        agent_id="generalist",
    )
    tools = ToolRegistry()
    SkillActivationService(skills).install(tools)
    event_log = EventLog(tmp_path / "events.jsonl")
    event_log.append(
        Event(run_id="run-skill", task_id="task-skill", type="assignment.created", source="coordinator")
    )

    first_model = _RecordingModel(
        json.dumps(
            {
                "type": "call_tool",
                "reason": "activate the relevant method",
                "tool_name": SKILL_ACTIVATE_TOOL_NAME,
                "tool_args": {"name": "repo-review"},
            }
        )
    )
    _make_loop(tmp_path, event_log=event_log, model=first_model, tools=tools, skills=skills).run_once()
    assert body_marker not in "\n".join(message.content for message in first_model.messages)

    second_model = _RecordingModel(json.dumps({"type": "stop", "reason": "method loaded"}))
    _make_loop(tmp_path, event_log=event_log, model=second_model, tools=tools, skills=skills).run_once()

    prompt_text = "\n".join(message.content for message in second_model.messages)
    events = event_log.read_all(task_id="task-skill")
    activation_event = next(
        event
        for event in events
        if event.type == "tool.completed" and event.payload["result"]["name"] == SKILL_ACTIVATE_TOOL_NAME
    )
    second_manifest = [
        event.payload["manifest"]
        for event in events
        if event.type == "context.manifest.compiled"
    ][-1]

    assert prompt_text.count(body_marker) == 1
    assert activation_event.id in second_manifest["excluded_refs"]
    assert active_skill_activations(events)[0].name == "repo-review"
    assert active_skill_activations(events)[0].source_id == "project"
    assert [tool["function"]["name"] for tool in first_model.tools] == [SKILL_ACTIVATE_TOOL_NAME]
