from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from crazy_harness.core.agents import AgentLoop
from crazy_harness.core.agents.contracts import AssignmentContract
from crazy_harness.core.agents.completion import CompletionGate, NudgeBudget
from crazy_harness.core.agents.planning import LocalPlan, PlanStep
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.context.builder import ContextBuilder
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.hooks import HookManager
from crazy_harness.core.models import DeepSeekOpenAIProvider, FakeModelProvider, ModelProvider
from crazy_harness.core.prompts import PromptPack, RuntimeManifest
from crazy_harness.core.tools import ToolRegistry
from crazy_harness.core.tools.pipeline import OperationLedger, ToolPipeline
from crazy_harness.core.tools.policy import PolicyContext


def _mock_responses() -> list[str]:
    actions = [
        {"type": "call_tool", "reason": "inspect service", "tool_name": "repo.read", "tool_args": {"path": "app.py"}},
        {"type": "call_tool", "reason": "collect test evidence", "tool_name": "test.run", "tool_args": {}},
        {"type": "call_tool", "reason": "prepare build intent", "tool_name": "build.mock_plan", "tool_args": {}},
        {"type": "call_tool", "reason": "prepare disposable dev plan", "tool_name": "volcengine.plan", "tool_args": {}},
        {"type": "stop", "reason": "all dry-run evidence collected"},
    ]
    return [json.dumps(action) for action in actions]


def _normalize_repo_path(payload: dict) -> dict:
    if payload.get("tool_name") != "repo.read":
        return payload
    updated = dict(payload)
    updated["args"] = dict(payload["args"])
    updated["args"]["path"] = str(updated["args"]["path"]).removeprefix("./")
    return updated


@dataclass
class Runtime:
    mode: str
    repo_path: Path
    runs_dir: Path
    model: ModelProvider | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        from crazy_harness.worlds.cicd.tools import register_cicd_tools

        self.repo_path = self.repo_path.resolve()
        self.run_id = uuid4().hex
        self.run_dir = self.runs_dir / self.run_id
        self.event_log = EventLog(self.run_dir / "events.jsonl")
        self.artifact_store = ArtifactStore(self.run_dir / "artifacts")
        self.tool_registry = ToolRegistry()
        register_cicd_tools(self.tool_registry, self.repo_path)
        self.assignment_contract = AssignmentContract(
            goal="collect evidence and prepare a disposable dev release plan",
            exit_criteria=(
                "repository inspected",
                "tests passed",
                "build plan exists",
                "Volcengine dry-run plan exists",
            ),
            output_schema={"type": "object"},
            evidence_requirements=("repo.read", "test.run", "build.mock_plan", "volcengine.plan"),
            permissions=("read_repo", "run_tests", "dry_run_plan"),
        )
        self.local_plan = LocalPlan(
            version=1,
            steps=(
                PlanStep(step_id="inspect", description="Inspect repository source."),
                PlanStep(step_id="test", description="Run the toy test suite."),
                PlanStep(step_id="build", description="Prepare a container build plan."),
                PlanStep(step_id="cloud", description="Prepare a Volcengine dry-run plan."),
            ),
        )
        hooks = HookManager()
        hooks.register("pre_tool_use", _normalize_repo_path)
        self.tool_pipeline = ToolPipeline(
            self.tool_registry,
            hooks=hooks,
            ledger=OperationLedger(self.run_dir / "operations.jsonl"),
        )
        self.policy_context = PolicyContext(
            agent_id="coordinator",
            assignment_id="dev-release",
            mode=self.mode,
            allowed_tools=frozenset(spec.name for spec in self.tool_registry.specs()),
        )
        self.context_builder = ContextBuilder(
            artifact_store=self.artifact_store,
            offload_chars=500,
            recent_event_limit=24,
        )
        self.prompt_pack = PromptPack(
            prompt_version="guided-mvp-1",
            role_section="You are the Coordinator for a disposable development release.",
            agent_card_section="Inspect evidence, use safe tools, and report the dry-run result.",
            task_brief_section="Collect repository, test, build-plan, and Volcengine dry-run evidence.",
            runtime_manifest=RuntimeManifest(
                agent_id="coordinator",
                task_id="dev-release",
                mode=self.mode,
                available_tools=self.tool_registry.specs(),
                available_skills=["dev-release-check"],
                workspace_policy={"root": str(self.repo_path), "write": False},
                network_policy={"default": "deny"},
            ),
            context_policy_section="EventLog is fact; large tool results may appear as artifact references.",
            tool_policy_section="The model proposes one action; the harness validates and executes it.",
            communication_policy_section="Do not claim an effect without a recorded tool result.",
        )
        if self.model is None:
            self.model = FakeModelProvider(_mock_responses()) if self.mode == "mock" else DeepSeekOpenAIProvider()

    def seed(self) -> None:
        if self.event_log.read_all():
            return
        self.event_log.append(
            Event(
                run_id=self.run_id,
                task_id="dev-release",
                type="release.requested",
                source="worlds.cicd",
                payload={
                    "repo_path": str(self.repo_path),
                    "mode": self.mode,
                    "goal": "collect evidence and prepare a disposable dev release plan",
                },
            )
        )
        self.event_log.append(
            Event(
                run_id=self.run_id,
                task_id="dev-release",
                type="plan.created",
                source="coordinator",
                payload=self.local_plan.model_dump(mode="json"),
            )
        )

    def run(self) -> Path:
        self.seed()
        loop = AgentLoop(
            agent_id="coordinator",
            task_id="dev-release",
            model=self.model,
            event_log=self.event_log,
            artifact_store=self.artifact_store,
            tool_registry=self.tool_registry,
            context_builder=self.context_builder,
            prompt_pack=self.prompt_pack,
            assignment_contract=self.assignment_contract,
            local_plan=self.local_plan,
            completion_gate=CompletionGate(),
            nudge_budget=NudgeBudget(missing_evidence=2, pending_operation=1),
            tool_pipeline=self.tool_pipeline,
            policy_context=self.policy_context,
        )
        loop.run_until_stop(max_steps=12)
        self._write_report()
        return self.run_dir

    def _write_report(self) -> None:
        events = self.event_log.read_all()
        lines = [
            "# Crazy Dev Release Run",
            "",
            f"- Run ID: `{self.run_id}`",
            f"- Mode: `{self.mode}`",
            f"- Repository: `{self.repo_path}`",
            "- Command runtime: `GuardedLocalRuntime` (allowlisted host subprocess, not a sandbox)",
            f"- Events: `{len(events)}`",
            "",
            "## Tool Evidence",
            "",
        ]
        for event in events:
            if event.type != "tool.completed":
                continue
            result = event.payload["result"]
            output = str(result.get("output", "")).strip().replace("\n", " ")[:240]
            lines.append(f"- `{result.get('name')}`: **{result.get('status')}** - {output}")
        lines.extend(["", "## Terminal", ""])
        terminal = next((event for event in reversed(events) if event.type.startswith("agent.")), None)
        lines.append(f"- `{terminal.type if terminal else 'not-terminal'}`")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
