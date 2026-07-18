from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from crazy_harness.core.a2a.orchestration import TeamContract, TeamStageSpec
from crazy_harness.core.agents import (
    AgentAction,
    AgentLoop,
    AssignmentBudget,
    AssignmentContract,
    CompletionGate,
    LocalPlan,
    NudgeBudget,
    PlanStep,
)
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.capabilities import CapabilityCatalog, CapabilityCompiler
from crazy_harness.core.context.builder import ContextBuilder
from crazy_harness.core.models import ModelProvider
from crazy_harness.core.prompts import PromptPack, RuntimeManifest
from crazy_harness.core.tools import ToolRegistry, ToolResult, ToolSpec
from crazy_harness.core.tools.pipeline import OperationLedger, ToolPipeline
from crazy_harness.core.tools.policy import PolicyContext, ToolPolicy

MessageHandler = Callable[[AgentAction, str], dict[str, object] | None]


class ResidentDemoTeamTaskPack:
    """Replaceable Team business pack; Core remains independent of this demo story."""

    task_pack_id = "resident-demo"
    _TOOL_BY_STAGE = {
        "evidence": "team.evidence.collect",
        "risk": "team.risk.inspect",
        "artifact": "team.peer.response.inspect",
        "review": "team.artifact.inspect",
    }
    _PEER_TOOL = "team.peer.request.inspect"

    def team_contract(self) -> TeamContract:
        stages = (
            TeamStageSpec(
                stage_id="evidence",
                result_kind="evidence",
                goal="Collect verifiable evidence for the incoming task.",
                required_capabilities=frozenset({"evidence.collect"}),
                exit_criteria=(
                    "tool evidence is persisted",
                    "evidence refs are returned",
                ),
                completion_event_type="evidence.recorded",
            ),
            TeamStageSpec(
                stage_id="risk",
                result_kind="evidence",
                goal="Independently inspect runtime risks and recovery boundaries.",
                required_capabilities=frozenset({"evidence.collect"}),
                exit_criteria=(
                    "risk evidence is persisted",
                    "recovery boundaries are explicit",
                ),
                completion_event_type="evidence.recorded",
            ),
            TeamStageSpec(
                stage_id="artifact",
                result_kind="artifact",
                goal="Compose a bounded execution artifact from the collected evidence.",
                required_capabilities=frozenset({"artifact.compose", "peer.request"}),
                exit_criteria=(
                    "one peer reconciliation is complete",
                    "artifact cites evidence",
                ),
                depends_on=("evidence", "risk"),
                completion_event_type="artifact.recorded",
            ),
            TeamStageSpec(
                stage_id="review",
                result_kind="review",
                goal="Independently review the artifact and its evidence.",
                required_capabilities=frozenset({"artifact.review"}),
                exit_criteria=(
                    "review decision is explicit",
                    "decision cites evidence",
                ),
                depends_on=("artifact",),
                completion_event_type="review.recorded",
            ),
        )
        durable_stages = tuple(
            stage.model_copy(
                update={"assignment_contract": self._build_assignment_contract(stage)}
            )
            for stage in stages
        )
        return TeamContract(
            contract_id=self.task_pack_id,
            version=2,
            max_parallel_assignments=2,
            lease_seconds=30,
            stages=durable_stages,
            peer_contract=self.peer_contract(),
        )

    def stage(self, stage_id: str) -> TeamStageSpec:
        try:
            return next(
                stage
                for stage in self.team_contract().stages
                if stage.stage_id == stage_id
            )
        except StopIteration as exc:
            raise KeyError(f"unknown resident Team stage: {stage_id}") from exc

    @staticmethod
    def assignment_agent_run_id(assignment_id: str) -> str:
        return f"{assignment_id}:agent-run"

    @staticmethod
    def peer_agent_run_id(correlation_id: str) -> str:
        return f"peer:{correlation_id}:agent-run"

    def assignment_contract(self, stage_id: str) -> AssignmentContract:
        stage = self.stage(stage_id)
        if stage.assignment_contract is None:
            raise RuntimeError(f"Team stage has no durable contract: {stage_id}")
        return stage.assignment_contract

    def _build_assignment_contract(self, stage: TeamStageSpec) -> AssignmentContract:
        stage_id = stage.stage_id
        tool_name = self._TOOL_BY_STAGE[stage_id]
        schemas = {
            "evidence": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
            "risk": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
            "artifact": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "content": {
                        "type": "object",
                        "properties": {
                            "steps": {"type": "array", "items": {"type": "string"}},
                            "rollback": {"type": "string"},
                        },
                        "required": ["steps", "rollback"],
                        "additionalProperties": False,
                    },
                },
                "required": ["title", "summary", "content"],
                "additionalProperties": False,
            },
            "review": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["approved", "rejected"]},
                    "summary": {"type": "string"},
                },
                "required": ["decision", "summary"],
                "additionalProperties": False,
            },
        }
        return AssignmentContract(
            goal=stage.goal,
            exit_criteria=stage.exit_criteria,
            output_schema=schemas[stage_id],
            evidence_requirements=(tool_name,),
            constraints=(
                "use only public Team events, summaries, and evidence references",
                "never claim an effect that is absent from the EventStore",
            ),
            permissions=(
                tool_name,
                "one_hop_peer_message"
                if stage_id == "artifact"
                else "read_public_team_facts",
            ),
            budgets=AssignmentBudget(
                turns=8, tool_calls=3, retries=1, wall_time_seconds=120
            ),
        )

    def peer_contract(self) -> AssignmentContract:
        return AssignmentContract(
            goal="Answer one bounded peer request from public evidence without sharing private context.",
            exit_criteria=(
                "request inspected",
                "response contains a summary and evidence references",
            ),
            output_schema={
                "type": "object",
                "properties": {"brief": {"type": "string"}},
                "required": ["brief"],
                "additionalProperties": False,
            },
            evidence_requirements=(self._PEER_TOOL,),
            constraints=(
                "one hop only",
                "read-only",
                "do not expose local plan or full context",
            ),
            permissions=(self._PEER_TOOL,),
            budgets=AssignmentBudget(
                turns=4, tool_calls=1, retries=1, wall_time_seconds=60
            ),
        )

    def scripted_assignment_responses(
        self, stage_id: str, *, peer_receiver: str = "scout"
    ) -> list[str]:
        actions: dict[str, list[dict]] = {
            "evidence": [
                {
                    "type": "call_tool",
                    "reason": "collect authoritative Team evidence",
                    "tool_name": self._TOOL_BY_STAGE[stage_id],
                    "tool_args": {},
                },
                {
                    "type": "submit_output",
                    "reason": "the persisted tool observation satisfies the evidence contract",
                    "artifact": {
                        "summary": "Repository and runtime evidence were collected by the ToolPipeline."
                    },
                },
            ],
            "risk": [
                {
                    "type": "call_tool",
                    "reason": "inspect runtime risk and recovery boundaries independently",
                    "tool_name": self._TOOL_BY_STAGE[stage_id],
                    "tool_args": {},
                },
                {
                    "type": "submit_output",
                    "reason": "the persisted tool observation satisfies the risk contract",
                    "artifact": {
                        "summary": "Lease, recovery, and bounded-execution risks were inspected independently."
                    },
                },
            ],
            "artifact": [
                {
                    "type": "send_message",
                    "reason": "verify freshness before composing the artifact",
                    "receiver": peer_receiver,
                    "message": {
                        "brief": "Confirm that the evidence is current before artifact composition.",
                        # Scope is an authority vocabulary, not a stage list.
                        # Both evidence and risk capsules are public evidence.
                        "scope": ["evidence"],
                        "permissions": ["read"],
                        "depth": 1,
                        "peer_budget": 1,
                    },
                },
                {
                    "type": "call_tool",
                    "reason": "inspect the persisted peer response",
                    "tool_name": self._TOOL_BY_STAGE[stage_id],
                    "tool_args": {},
                },
                {
                    "type": "submit_output",
                    "reason": "peer reconciliation and persisted evidence support this bounded plan",
                    "artifact": {
                        "title": "Bounded execution plan",
                        "summary": "A reversible plan grounded in independent evidence and risk capsules.",
                        "content": {
                            "steps": [
                                "inspect evidence",
                                "apply bounded change",
                                "run checks",
                            ],
                            "rollback": "restore the previous immutable behavior version",
                        },
                    },
                },
            ],
            "review": [
                {
                    "type": "call_tool",
                    "reason": "inspect the public artifact and evidence chain",
                    "tool_name": self._TOOL_BY_STAGE[stage_id],
                    "tool_args": {},
                },
                {
                    "type": "submit_output",
                    "reason": "the machine-visible evidence supports approval",
                    "artifact": {
                        "decision": "approved",
                        "summary": "Evidence exists, A2A depth is bounded, and rollback is explicit.",
                    },
                },
            ],
        }
        try:
            return [
                json.dumps(action, ensure_ascii=False) for action in actions[stage_id]
            ]
        except KeyError as exc:
            raise KeyError(f"unknown resident Team stage: {stage_id}") from exc

    def scripted_peer_responses(self) -> list[str]:
        return [
            json.dumps(
                {
                    "type": "call_tool",
                    "reason": "inspect the bounded request and public evidence",
                    "tool_name": self._PEER_TOOL,
                    "tool_args": {},
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "type": "submit_output",
                    "reason": "the public evidence was checked without sharing private context",
                    "artifact": {
                        "brief": "Cross-check complete: the cited evidence exists and is current."
                    },
                },
                ensure_ascii=False,
            ),
        ]

    def build_assignment_loop(
        self,
        *,
        run_id: str,
        root_task_id: str,
        task_id: str,
        assignment_id: str,
        stage_id: str,
        agent_id: str,
        brief: str,
        model: ModelProvider,
        event_log,
        artifact_store: ArtifactStore,
        ledger_path: Path,
        assignment_contract: AssignmentContract,
        message_handler: MessageHandler,
        fault_injector: Callable[[str], None] | None = None,
    ) -> AgentLoop:
        tools = self._assignment_tools(
            run_id=run_id,
            assignment_id=assignment_id,
            stage_id=stage_id,
            agent_id=agent_id,
            event_log=event_log,
        )
        return self._build_loop(
            run_id=run_id,
            root_task_id=root_task_id,
            task_id=task_id,
            assignment_id=assignment_id,
            agent_id=agent_id,
            brief=brief,
            model=model,
            event_log=event_log,
            artifact_store=artifact_store,
            ledger_path=ledger_path,
            contract=assignment_contract,
            tools=tools,
            plan_steps=(
                "Inspect only the facts exposed for this Assignment.",
                "Use the authorized tool or one-hop peer request when required.",
                "Submit only after the mechanical CompletionGate can pass.",
            ),
            message_handler=message_handler,
            fault_injector=fault_injector,
        )

    def build_peer_loop(
        self,
        *,
        run_id: str,
        root_task_id: str,
        task_id: str,
        correlation_id: str,
        agent_id: str,
        brief: str,
        model: ModelProvider,
        event_log,
        artifact_store: ArtifactStore,
        ledger_path: Path,
        assignment_contract: AssignmentContract,
        fault_injector: Callable[[str], None] | None = None,
    ) -> AgentLoop:
        tools = self._peer_tools(
            run_id=run_id,
            correlation_id=correlation_id,
            agent_id=agent_id,
            event_log=event_log,
        )
        return self._build_loop(
            run_id=run_id,
            root_task_id=root_task_id,
            task_id=task_id,
            assignment_id=f"peer:{correlation_id}",
            agent_id=agent_id,
            brief=brief,
            model=model,
            event_log=event_log,
            artifact_store=artifact_store,
            ledger_path=ledger_path,
            contract=assignment_contract,
            tools=tools,
            plan_steps=(
                "Inspect the bounded peer request.",
                "Read only public evidence references.",
                "Return a concise evidence capsule.",
            ),
            message_handler=None,
            fault_injector=fault_injector,
        )

    def _build_loop(
        self,
        *,
        run_id: str,
        root_task_id: str,
        task_id: str,
        assignment_id: str,
        agent_id: str,
        brief: str,
        model: ModelProvider,
        event_log,
        artifact_store: ArtifactStore,
        ledger_path: Path,
        contract: AssignmentContract,
        tools: ToolRegistry,
        plan_steps: tuple[str, ...],
        message_handler: MessageHandler | None,
        fault_injector: Callable[[str], None] | None,
    ) -> AgentLoop:
        plan = LocalPlan(
            version=1,
            steps=tuple(
                PlanStep(step_id=f"step-{index}", description=description)
                for index, description in enumerate(plan_steps, start=1)
            ),
        )
        prompt = PromptPack(
            prompt_version="resident-team-worker-v1",
            role_section=(
                f"You are {agent_id}, one persistent logical worker in an event-driven Agent Team. "
                "Propose exactly one action per wake; the Harness alone creates facts."
            ),
            agent_card_section=(
                "Private Context and LocalPlan stay inside this AgentRun. "
                "A2A communication carries only a brief, schema, and evidence references."
            ),
            task_brief_section=brief,
            runtime_manifest=RuntimeManifest(
                agent_id=agent_id,
                task_id=task_id,
                mode="scripted",
                available_tools=tools.specs(),
                workspace_policy={
                    "root_task_id": root_task_id,
                    "fact_source": "SQLite EventStore",
                },
                network_policy={"default": "deny"},
                model_profile={
                    "provider_mode": "scripted",
                    "one_action_per_turn": True,
                },
            ),
            context_policy_section=(
                "Context is rebuilt from this AgentRun's events every turn. Large tool results are offloaded; "
                "root Team state is visible only through public capsules and references."
            ),
            tool_policy_section=(
                "Native tool calls pass Command validation, ToolPolicy, ToolPipeline, and OperationLedger. "
                "Use submit_output only when the required schema and evidence are present."
            ),
            communication_policy_section="Only one-hop, read-only peer reconciliation is allowed.",
            artifact_schema_section=json.dumps(
                contract.output_schema, ensure_ascii=False, sort_keys=True
            ),
        )
        pipeline = ToolPipeline(
            tools,
            policy=ToolPolicy(),
            ledger=OperationLedger(ledger_path),
        )
        return AgentLoop(
            agent_id=agent_id,
            task_id=task_id,
            model=model,
            event_log=event_log,
            artifact_store=artifact_store,
            tool_registry=tools,
            context_builder=ContextBuilder(
                artifact_store=artifact_store,
                offload_chars=1_200,
                recent_event_limit=40,
            ),
            prompt_pack=prompt,
            assignment_contract=contract,
            local_plan=plan,
            active_nudge="Stay inside the AssignmentContract and cite only persisted evidence.",
            completion_gate=CompletionGate(),
            nudge_budget=NudgeBudget(
                schema_error=2, missing_evidence=2, pending_operation=1, no_progress=1
            ),
            tool_pipeline=pipeline,
            policy_context=PolicyContext(
                agent_id=agent_id,
                assignment_id=assignment_id,
                mode="scripted",
                allowed_tools=frozenset(spec.name for spec in tools.specs()),
            ),
            message_handler=message_handler,
            capability_compiler=CapabilityCompiler(
                CapabilityCatalog.from_tool_specs(tools.specs())
            ),
            fault_injector=fault_injector,
        )

    def _assignment_tools(
        self,
        *,
        run_id: str,
        assignment_id: str,
        stage_id: str,
        agent_id: str,
        event_log,
    ) -> ToolRegistry:
        tool_name = self._TOOL_BY_STAGE[stage_id]
        tools = ToolRegistry()

        def inspect(_: dict) -> ToolResult:
            events = event_log.read_all(run_id=run_id)
            if stage_id in {"evidence", "risk"}:
                run = next(event for event in events if event.type == "run.created")
                payload = {
                    "lens": stage_id,
                    "title": run.payload.get("title"),
                    "brief": run.payload.get("brief"),
                    "observed_event_count": len(events),
                    "trace": [
                        f"observation[{index:03d}] persisted runtime boundary verified"
                        for index in range(80)
                    ],
                }
            elif stage_id == "artifact":
                responses = [
                    event
                    for event in events
                    if event.type == "a2a.peer.responded"
                    and event.payload.get("assignment_id") == assignment_id
                ]
                if not responses:
                    return ToolResult(
                        name=tool_name,
                        status="error",
                        error="peer response is not persisted",
                    )
                response = responses[-1]
                payload = {
                    "brief": response.payload.get("brief"),
                    "correlation_id": response.payload.get("correlation_id"),
                    "evidence_refs": response.payload.get("evidence_refs", []),
                }
            else:
                public = [
                    event
                    for event in events
                    if event.type
                    in {"evidence.recorded", "artifact.recorded", "a2a.peer.responded"}
                ]
                payload = {
                    "public_event_refs": [event.id for event in public],
                    "artifact_has_rollback": any(
                        bool(event.payload.get("content", {}).get("rollback"))
                        for event in public
                        if event.type == "artifact.recorded"
                    ),
                }
            return ToolResult(
                name=tool_name,
                status="ok",
                output=json.dumps(payload, ensure_ascii=False),
            )

        tools.register(self._read_only_spec(tool_name, agent_id), inspect)
        return tools

    def _peer_tools(
        self, *, run_id: str, correlation_id: str, agent_id: str, event_log
    ) -> ToolRegistry:
        tools = ToolRegistry()

        def inspect(_: dict) -> ToolResult:
            events = event_log.read_all(run_id=run_id)
            request = next(
                (
                    event
                    for event in reversed(events)
                    if event.type == "a2a.peer.requested"
                    and event.payload.get("correlation_id") == correlation_id
                ),
                None,
            )
            if request is None:
                return ToolResult(
                    name=self._PEER_TOOL,
                    status="error",
                    error="peer request is not persisted",
                )
            public = [event for event in events if event.type == "evidence.recorded"]
            payload = {
                "brief": request.payload.get("brief"),
                "scope": request.payload.get("scope", []),
                "evidence_refs": [event.id for event in public],
            }
            return ToolResult(
                name=self._PEER_TOOL,
                status="ok",
                output=json.dumps(payload, ensure_ascii=False),
            )

        tools.register(self._read_only_spec(self._PEER_TOOL, agent_id), inspect)
        return tools

    @staticmethod
    def _read_only_spec(name: str, agent_id: str) -> ToolSpec:
        return ToolSpec(
            name=name,
            description="Read a bounded, public Team evidence capsule from the persistent EventStore.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            use_when="The Assignment requires authoritative Team evidence.",
            do_not_use_when="Private context or an unauthorized side effect would be required.",
            side_effect_level="none",
            is_read_only=True,
            is_concurrency_safe=True,
            allowed_agents={agent_id},
            allowed_modes={"scripted"},
        )
