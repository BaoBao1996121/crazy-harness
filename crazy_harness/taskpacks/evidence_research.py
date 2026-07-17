from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crazy_harness.core.agents import (
    AgentLoop,
    AssignmentContract,
    CompletionGate,
    NudgeBudget,
)
from crazy_harness.core.agents.completion import (
    CompletionFinding,
    CompletionFindingCode,
    CompletionGateResult,
)
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
from crazy_harness.taskpacks.base import record_skill_catalog
from crazy_harness.taskpacks.research_tools import (
    REPORT_VALIDATE_TOOL_NAME,
    REPORT_WRITE_TOOL_NAME,
    SOURCE_OPEN_TOOL_NAME,
    ResearchEvidenceItem,
    ResearchSource,
    build_research_tools,
    render_source_html,
)

_SOURCES = (
    ResearchSource(
        source_id="experiment",
        title="Nova deployment drill results",
        summary="Controlled observations from three deployment drills.",
        tags=("experiment", "canary", "rolling"),
        evidence=(
            ResearchEvidenceItem(
                "canary-result",
                "A ten-percent canary surfaced a five-percent error regression within three minutes.",
            ),
            ResearchEvidenceItem(
                "rolling-risk",
                "The previous rolling drill exposed a schema compatibility fault before completion.",
            ),
        ),
    ),
    ResearchSource(
        source_id="policy",
        title="Production traffic change policy",
        summary="Mandatory controls for customer-facing deployment changes.",
        tags=("policy", "rollback", "ownership"),
        evidence=(
            ResearchEvidenceItem(
                "rollback-plan",
                "Every customer traffic change requires a documented rollback trigger and named owner.",
            ),
            ResearchEvidenceItem(
                "independent-evidence",
                "A deployment recommendation must cite at least two independent sources.",
            ),
        ),
    ),
    ResearchSource(
        source_id="requirements",
        title="Nova release requirements",
        summary="Availability and recovery constraints for the Nova service.",
        tags=("requirements", "availability", "rto"),
        evidence=(
            ResearchEvidenceItem(
                "rto",
                "Production changes must support rollback within ten minutes.",
            ),
            ResearchEvidenceItem(
                "zero-downtime",
                "The service must remain available while a new release is introduced.",
            ),
        ),
    ),
)

_SCRIPTED_REPORT = """# Recommendation

Use a ten-percent canary with an explicit rollback trigger.

## Findings

The service requires rollback within ten minutes [source:requirements#rto].
The canary drill exposed a regression within three minutes [source:experiment#canary-result].

## Risks

Every traffic change needs an owned rollback plan [source:policy#rollback-plan].

## Sources

- requirements
- experiment
- policy
"""
_SCRIPTED_REPORT_SHA = hashlib.sha256(_SCRIPTED_REPORT.encode("utf-8")).hexdigest()
_SCRIPTED_CITATIONS = (
    "source:requirements#rto",
    "source:experiment#canary-result",
    "source:policy#rollback-plan",
)


@dataclass(frozen=True)
class PreparedResearchWorkspace:
    workspace: Path


class EvidenceResearchCompletionGate(CompletionGate):
    """Bind the submitted artifact to browser facts and the latest validated report."""

    def __init__(self, event_log, task_id: str) -> None:
        self.event_log = event_log
        self.task_id = task_id

    def evaluate(
        self,
        contract: AssignmentContract,
        *,
        output: Any,
        evidence: Mapping[str, Collection[str] | str] | None = None,
        pending_operations: Collection[str] = (),
    ) -> CompletionGateResult:
        base = super().evaluate(
            contract,
            output=output,
            evidence=evidence,
            pending_operations=pending_operations,
        )
        findings = list(base.findings)
        events = self.event_log.read_all(task_id=self.task_id)
        opened_sources: set[str] = set()
        validation: dict[str, object] | None = None
        latest_report_write = -1
        latest_validation = -1
        for event_index, event in enumerate(events):
            if event.type != "tool.completed":
                continue
            result = event.payload.get("result")
            if not isinstance(result, dict):
                continue
            tool_name = result.get("name")
            if tool_name == REPORT_WRITE_TOOL_NAME:
                latest_report_write = event_index
                continue
            if not isinstance(result.get("output"), str):
                continue
            try:
                payload = json.loads(result["output"])
            except (TypeError, ValueError):
                continue
            if tool_name == SOURCE_OPEN_TOOL_NAME and isinstance(payload, dict):
                source_id = payload.get("source_id")
                if isinstance(source_id, str):
                    opened_sources.add(source_id)
            elif tool_name == REPORT_VALIDATE_TOOL_NAME and isinstance(payload, dict):
                validation = payload
                latest_validation = event_index
        if len(opened_sources) < 2:
            findings.append(
                CompletionFinding(
                    code=CompletionFindingCode.EVIDENCE,
                    message="research completion requires browser evidence from at least two sources",
                    path=SOURCE_OPEN_TOOL_NAME,
                )
            )
        if validation is not None and latest_report_write > latest_validation:
            findings.append(
                CompletionFinding(
                    code=CompletionFindingCode.EVIDENCE,
                    message="latest validation is stale because a newer report write exists",
                    path=REPORT_VALIDATE_TOOL_NAME,
                )
            )
        if validation is not None and isinstance(output, Mapping):
            submitted_citations = output.get("citations")
            expected_citations = validation.get("citations")
            bindings = (
                (
                    "report_path",
                    output.get("report_path"),
                    validation.get("report_path"),
                ),
                (
                    "report_sha256",
                    output.get("report_sha256"),
                    validation.get("report_sha256"),
                ),
                ("citations", submitted_citations, expected_citations),
            )
            for field, submitted, expected in bindings:
                if submitted != expected:
                    findings.append(
                        CompletionFinding(
                            code=CompletionFindingCode.EVIDENCE,
                            message=f"submitted {field} does not match validated report evidence",
                            path=f"$.{field}",
                        )
                    )
        return CompletionGateResult(passed=not findings, findings=tuple(findings))


class EvidenceResearchTaskPack:
    task_pack_id = "evidence-research"
    agent_id = "generalist"

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
        self.skill_sources = (
            tuple(skill_sources)
            if skill_sources is not None
            else (
                SkillSource(
                    source_id="crazy-project",
                    root=project_root / ".agents" / "skills",
                    scope=SkillScope.PROJECT,
                    trusted=True,
                ),
            )
        )

    def prepare(self, run_id: str) -> PreparedResearchWorkspace:
        prepared = PreparedResearchWorkspace(self.data_dir / "workspaces" / run_id)
        prepared.workspace.mkdir(parents=True, exist_ok=True)
        source_dir = prepared.workspace / "sources"
        source_dir.mkdir(parents=True, exist_ok=True)
        for source in _SOURCES:
            target = source_dir / f"{source.source_id}.html"
            if not target.exists():
                target.write_text(
                    render_source_html(source), encoding="utf-8", newline="\n"
                )
        return prepared

    def build_skills(self) -> SkillCatalog:
        discovered = FileSystemSkillLoader().discover(
            self.skill_sources, agent_id=self.agent_id
        )
        return discovered.select(("evidence-research",))

    def build_tools(
        self,
        prepared: PreparedResearchWorkspace,
        *,
        skills: SkillCatalog | None = None,
    ) -> ToolRegistry:
        skills = skills or self.build_skills()
        tools = build_research_tools(prepared.workspace, _SOURCES)
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
        record_skill_catalog(
            event_log,
            run_id=run_id,
            task_id=task_id,
            agent_id=self.agent_id,
            source="taskpack.evidence-research",
            skills=skills,
        )
        contract = assignment_contract or self.assignment_contract()
        plan = LocalPlan(
            version=1,
            steps=(
                PlanStep(
                    step_id="discover",
                    description="Inspect source metadata before opening pages.",
                ),
                PlanStep(
                    step_id="collect",
                    description="Open independent sources and preserve browser evidence.",
                ),
                PlanStep(
                    step_id="synthesize",
                    description="Write a bounded report with canonical citations.",
                ),
                PlanStep(
                    step_id="validate",
                    description="Run deterministic citation and structure validation.",
                ),
                PlanStep(
                    step_id="submit",
                    description="Submit only the artifact bound to validated evidence.",
                ),
            ),
        )
        prompt = PromptPack(
            prompt_version="evidence-research-v1",
            role_section=(
                "You are an evidence research agent. Propose exactly one action per turn; "
                "the harness alone opens pages, writes reports, validates citations, and records facts."
            ),
            agent_card_section=(
                "Inspect source metadata first. Open at least two independent sources in the browser. "
                "Never invent a source or evidence identifier."
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
                    "writable_paths": ["report.md"],
                    "immutable_paths": ["sources/**"],
                },
                network_policy={"default": "deny", "browser_hosts": ["127.0.0.1"]},
                model_profile={
                    "provider_mode": model_mode,
                    "one_action_per_turn": True,
                },
            ),
            context_policy_section=(
                "Source metadata is progressively disclosed. Full page evidence enters Context only after a "
                "successful research.source.open observation; large results may be offloaded to artifacts."
            ),
            tool_policy_section=(
                "Use native tool calls. Write only report.md. Before submission, run research.report.validate "
                "and copy its report_sha256 and citations exactly into the submitted artifact."
            ),
            communication_policy_section="This v1 single-agent baseline cannot delegate or contact peers.",
            artifact_schema_section=json.dumps(contract.output_schema, sort_keys=True),
        )
        pipeline = ToolPipeline(
            tools,
            policy=ToolPolicy(destructive_modes=("scripted", "deepseek", "llm-live")),
            ledger=OperationLedger(ledger_path),
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
            completion_gate=EvidenceResearchCompletionGate(event_log, task_id),
            nudge_budget=NudgeBudget(
                schema_error=2, missing_evidence=2, pending_operation=1, no_progress=1
            ),
            tool_pipeline=pipeline,
            policy_context=PolicyContext(
                agent_id=self.agent_id,
                assignment_id=task_id,
                mode=model_mode,
                allowed_tools=frozenset(spec.name for spec in tools.specs()),
                approved_tools=frozenset({REPORT_WRITE_TOOL_NAME}),
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
    def assignment_contract() -> AssignmentContract:
        return AssignmentContract(
            goal="synthesize a deployment recommendation from independent browser evidence",
            exit_criteria=(
                "at least two independent sources were opened through BrowserRuntime",
                "report.md contains Recommendation, Findings, Risks, and Sources sections",
                "all canonical citations resolve to the immutable source catalog",
                "the submitted artifact matches the latest validated report",
            ),
            output_schema={
                "type": "object",
                "properties": {
                    "recommendation": {"type": "string"},
                    "report_path": {"type": "string", "enum": ["report.md"]},
                    "report_sha256": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "recommendation",
                    "report_path",
                    "report_sha256",
                    "citations",
                ],
                "additionalProperties": False,
            },
            evidence_requirements=(SOURCE_OPEN_TOOL_NAME, REPORT_VALIDATE_TOOL_NAME),
            constraints=(
                "do not browse sources outside the allowlisted catalog",
                "write only report.md in the disposable workspace",
                "do not claim semantic certainty beyond the supplied evidence",
            ),
            permissions=(
                "list_sources",
                "open_allowlisted_sources",
                "write_report",
                "validate_report",
            ),
            budgets=AssignmentBudget(
                turns=18, tool_calls=14, retries=2, wall_time_seconds=300
            ),
        )

    @staticmethod
    def scripted_responses() -> list[str]:
        actions = [
            {
                "type": "call_tool",
                "reason": "load the research method",
                "tool_name": "skill.activate",
                "tool_args": {"name": "evidence-research"},
            },
            {
                "type": "call_tool",
                "reason": "discover independent sources",
                "tool_name": "research.sources.list",
                "tool_args": {},
            },
            {
                "type": "call_tool",
                "reason": "inspect release constraints",
                "tool_name": "research.source.open",
                "tool_args": {"source_id": "requirements"},
            },
            {
                "type": "call_tool",
                "reason": "inspect deployment experiment",
                "tool_name": "research.source.open",
                "tool_args": {"source_id": "experiment"},
            },
            {
                "type": "call_tool",
                "reason": "inspect traffic policy",
                "tool_name": "research.source.open",
                "tool_args": {"source_id": "policy"},
            },
            {
                "type": "call_tool",
                "reason": "write cited report",
                "tool_name": "research.report.write",
                "tool_args": {"content": _SCRIPTED_REPORT},
            },
            {
                "type": "call_tool",
                "reason": "validate citations and bind report hash",
                "tool_name": "research.report.validate",
                "tool_args": {},
            },
            {
                "type": "submit_output",
                "reason": "browser sources and report validator prove the artifact",
                "artifact": {
                    "recommendation": "Use a ten-percent canary with an explicit rollback trigger.",
                    "report_path": "report.md",
                    "report_sha256": _SCRIPTED_REPORT_SHA,
                    "citations": list(_SCRIPTED_CITATIONS),
                },
            },
        ]
        return [json.dumps(action) for action in actions]
