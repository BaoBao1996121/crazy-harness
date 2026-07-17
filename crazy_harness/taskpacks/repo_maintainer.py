from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from crazy_harness.core.agents import AgentLoop, AssignmentContract, CompletionGate, NudgeBudget
from crazy_harness.core.agents.contracts import AssignmentBudget
from crazy_harness.core.agents.planning import LocalPlan, PlanStep
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.capabilities import (
    CAPABILITY_SEARCH_TOOL_NAME,
    CapabilityCatalog,
    CapabilityCompiler,
    CapabilityDefinition,
    CapabilityKind,
    CapabilitySearchService,
)
from crazy_harness.core.context.builder import ContextBuilder
from crazy_harness.core.events import Event
from crazy_harness.core.models import ModelProvider
from crazy_harness.core.prompts import PromptPack, RuntimeManifest
from crazy_harness.core.skills import (
    SKILL_ACTIVATE_TOOL_NAME,
    FileSystemSkillLoader,
    SkillActivationService,
    SkillCatalog,
    SkillScope,
    SkillSource,
)
from crazy_harness.core.tools import ToolRegistry
from crazy_harness.core.tools.pipeline import OperationLedger, ToolPipeline
from crazy_harness.core.tools.policy import PolicyContext, ToolPolicy
from crazy_harness.taskpacks.repo_tools import build_repo_tools

_FIXED_SOURCE = """def clamp(value: int, lower: int, upper: int) -> int:
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return max(lower, min(value, upper))
"""

_TEMPLATE_FILES = {
    "calculator.py": """def clamp(value: int, lower: int, upper: int) -> int:
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return min(lower, max(value, upper))
""",
    "tests/__init__.py": "",
    "tests/test_calculator.py": """import unittest

from calculator import clamp


class ClampTests(unittest.TestCase):
    def test_value_inside_range_is_unchanged(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_value_is_clamped_at_both_bounds(self):
        self.assertEqual(clamp(-3, 0, 10), 0)
        self.assertEqual(clamp(15, 0, 10), 10)

    def test_invalid_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            clamp(5, 10, 0)


if __name__ == "__main__":
    unittest.main()
""",
    "README.md": "Run `python -m unittest discover -s tests -v` and repair the implementation without changing tests.\n",
}


@dataclass(frozen=True)
class PreparedRepoWorkspace:
    workspace: Path
    baseline: Path


class RepoMaintainerTaskPack:
    task_pack_id = "repo-maintainer"
    agent_id = "generalist"
    writable_paths = frozenset({"calculator.py"})

    def __init__(
        self,
        data_dir: Path,
        *,
        capability_inline_limit: int = 12,
        capability_search_limit: int = 6,
        skill_sources: Sequence[SkillSource] | None = None,
    ) -> None:
        if capability_inline_limit < 0:
            raise ValueError("capability_inline_limit must be non-negative")
        if capability_search_limit < 1:
            raise ValueError("capability_search_limit must be positive")
        self.data_dir = Path(data_dir)
        self.capability_inline_limit = capability_inline_limit
        self.capability_search_limit = capability_search_limit
        project_root = Path(__file__).resolve().parents[2]
        self.skill_sources = tuple(skill_sources) if skill_sources is not None else (
            SkillSource(
                source_id="crazy-project",
                root=project_root / ".agents" / "skills",
                scope=SkillScope.PROJECT,
                trusted=True,
            ),
        )

    def prepare(self, run_id: str) -> PreparedRepoWorkspace:
        prepared = PreparedRepoWorkspace(
            workspace=self.data_dir / "workspaces" / run_id,
            baseline=self.data_dir / "baselines" / run_id,
        )
        self._materialize_once(prepared.workspace)
        self._materialize_once(prepared.baseline)
        return prepared

    def build_skills(self) -> SkillCatalog:
        return FileSystemSkillLoader().discover(self.skill_sources, agent_id=self.agent_id)

    def build_tools(
        self,
        prepared: PreparedRepoWorkspace,
        *,
        skills: SkillCatalog | None = None,
    ) -> ToolRegistry:
        skills = skills or self.build_skills()
        tools = build_repo_tools(
            prepared.workspace,
            prepared.baseline,
            writable_paths=self.writable_paths,
        )
        if skills.names():
            SkillActivationService(skills).install(tools)
        base_specs = tools.specs()
        if len(base_specs) > self.capability_inline_limit:
            base_names = frozenset(spec.name for spec in base_specs)
            CapabilitySearchService(
                CapabilityCatalog.from_tool_specs(base_specs),
                allowed_names=base_names,
                max_results=self.capability_search_limit,
            ).install(tools)
        return tools

    def build_loop(
        self,
        *,
        run_id: str,
        task_id: str,
        brief: str,
        model_mode: str,
        model: ModelProvider,
        event_log,
        artifact_store: ArtifactStore,
        ledger_path: Path,
        assignment_contract: AssignmentContract | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> AgentLoop:
        prepared = self.prepare(run_id)
        skills = self.build_skills()
        tools = self.build_tools(prepared, skills=skills)
        self._record_skill_catalog(event_log, run_id=run_id, task_id=task_id, skills=skills)
        # 新任务由 TaskPack 生成 Contract；恢复旧任务时必须使用事件中固化的版本。
        contract = assignment_contract or self.assignment_contract()
        plan = LocalPlan(
            version=1,
            steps=(
                PlanStep(step_id="inspect", description="Inspect repository structure, implementation, and tests."),
                PlanStep(step_id="diagnose", description="Explain the failing behavior from durable evidence."),
                PlanStep(step_id="edit", description="Apply the smallest allowlisted implementation change."),
                PlanStep(step_id="verify", description="Run tests and inspect the workspace diff."),
                PlanStep(step_id="submit", description="Submit a structured result only after the machine gate can pass."),
            ),
        )
        prompt = PromptPack(
            prompt_version="repo-maintainer-v1",
            role_section=(
                "You are a repository maintenance agent in a disposable workspace. "
                "Propose exactly one action per turn; the harness alone executes tools and records facts."
            ),
            agent_card_section=(
                "Inspect before editing. Never claim a test or file effect without a recorded observation. "
                "Tests and policy files are read-only."
            ),
            task_brief_section=brief,
            runtime_manifest=RuntimeManifest(
                agent_id=self.agent_id,
                task_id=task_id,
                mode=model_mode,
                available_tools=tools.specs(),
                available_skills=list(skills.names()),
                skill_catalog=list(skills.entries()),
                workspace_policy={
                    "root": str(prepared.workspace),
                    "disposable": True,
                    "writable_paths": sorted(self.writable_paths),
                },
                network_policy={"default": "deny"},
                model_profile={"provider_mode": model_mode, "one_action_per_turn": True},
            ),
            context_policy_section=(
                "The event log is the fact source. Large observations may appear as artifact references. "
                "Treat model text as a proposal, never as proof."
            ),
            tool_policy_section=(
                "Use a native tool call for tool actions. Skill allowed-tools values are hints only; "
                "ToolPolicy remains authority. For completion, return one JSON AgentAction with "
                "type=submit_output, a reason, and artifact={summary, changed_files}."
            ),
            communication_policy_section="This single-agent baseline cannot delegate or contact peers.",
            artifact_schema_section=json.dumps(contract.output_schema, sort_keys=True),
        )
        policy = ToolPolicy(destructive_modes=("scripted", "deepseek", "llm-live"))
        pipeline = ToolPipeline(
            tools,
            policy=policy,
            ledger=OperationLedger(ledger_path),
        )
        capability_catalog = CapabilityCatalog.from_tool_specs(tools.specs())
        if tools.has(SKILL_ACTIVATE_TOOL_NAME):
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
            agent_id=self.agent_id,
            task_id=task_id,
            fault_injector=fault_injector,
            model=model,
            event_log=event_log,
            artifact_store=artifact_store,
            tool_registry=tools,
            context_builder=ContextBuilder(
                artifact_store=artifact_store,
                offload_chars=4_000,
                recent_event_limit=40,
            ),
            prompt_pack=prompt,
            assignment_contract=contract,
            local_plan=plan,
            completion_gate=CompletionGate(),
            nudge_budget=NudgeBudget(schema_error=2, missing_evidence=2, pending_operation=1, no_progress=1),
            tool_pipeline=pipeline,
            policy_context=PolicyContext(
                agent_id=self.agent_id,
                assignment_id=task_id,
                mode=model_mode,
                allowed_tools=frozenset(spec.name for spec in tools.specs()),
                approved_tools=frozenset({"repo.write", "repo.replace"}),
            ),
            capability_compiler=CapabilityCompiler(
                capability_catalog,
                inline_limit=self.capability_inline_limit,
                search_limit=self.capability_search_limit,
            ),
            capability_always_include=tuple(
                name
                for name in (CAPABILITY_SEARCH_TOOL_NAME, SKILL_ACTIVATE_TOOL_NAME)
                if tools.has(name)
            ),
        )

    @staticmethod
    def _record_skill_catalog(
        event_log,
        *,
        run_id: str,
        task_id: str,
        skills: SkillCatalog,
    ) -> None:
        manifest = skills.audit_manifest()
        existing = [
            event
            for event in event_log.read_all(task_id=task_id)
            if event.type == "skill.catalog.compiled"
        ]
        if existing and existing[-1].payload.get("manifest_hash") == manifest["manifest_hash"]:
            return
        parent = event_log.last(task_id=task_id)
        event_log.append(
            Event(
                run_id=run_id,
                task_id=task_id,
                type="skill.catalog.compiled",
                source="taskpack.repo-maintainer",
                payload={
                    "agent_id": RepoMaintainerTaskPack.agent_id,
                    "disclosure": "metadata_then_explicit_activation",
                    **manifest,
                },
                causation_id=parent.id if parent is not None else None,
            )
        )

    @staticmethod
    def assignment_contract() -> AssignmentContract:
        return AssignmentContract(
            goal="repair the implementation in a disposable repository and prove the result",
            exit_criteria=(
                "implementation changed without modifying tests",
                "the real test command passed",
                "a non-empty baseline diff was recorded",
                "the submitted artifact matches the required schema",
            ),
            output_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "changed_files": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "changed_files"],
                "additionalProperties": False,
            },
            evidence_requirements=("test.run", "repo.diff"),
            constraints=("do not modify tests", "remain inside the disposable workspace"),
            permissions=("read_repo", "write_allowlisted_implementation", "run_bounded_checks"),
            budgets=AssignmentBudget(turns=16, tool_calls=12, retries=2, wall_time_seconds=300),
        )

    @staticmethod
    def scripted_responses() -> list[str]:
        actions = [
            {"type": "call_tool", "reason": "load the maintenance method", "tool_name": "skill.activate", "tool_args": {"name": "repo-maintainer"}},
            {"type": "call_tool", "reason": "inspect implementation", "tool_name": "repo.read", "tool_args": {"path": "calculator.py"}},
            {"type": "call_tool", "reason": "inspect tests", "tool_name": "repo.read", "tool_args": {"path": "tests/test_calculator.py"}},
            {"type": "call_tool", "reason": "apply bounded fix", "tool_name": "repo.write", "tool_args": {"path": "calculator.py", "content": _FIXED_SOURCE}},
            {"type": "call_tool", "reason": "prove behavior", "tool_name": "test.run", "tool_args": {}},
            {"type": "call_tool", "reason": "record source diff", "tool_name": "repo.diff", "tool_args": {}},
            {"type": "submit_output", "reason": "tests and diff prove the repair", "artifact": {"summary": "Corrected clamp bounds handling.", "changed_files": ["calculator.py"]}},
        ]
        return [json.dumps(action) for action in actions]

    @staticmethod
    def _materialize_once(root: Path) -> None:
        if root.exists():
            return
        for relative, content in _TEMPLATE_FILES.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
