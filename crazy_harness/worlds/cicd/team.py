from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from crazy_harness.core.a2a.coordinator import Assignment, Coordinator
from crazy_harness.core.a2a.messages import A2AMessage
from crazy_harness.core.a2a.policy import PeerContract, PeerPolicy, PeerRequest
from crazy_harness.core.a2a.review import EvidencePack, ReviewerGate
from crazy_harness.core.agents import AgentAction, AgentLoop
from crazy_harness.core.agents.completion import CompletionGate, NudgeBudget
from crazy_harness.core.agents.contracts import AssignmentContract
from crazy_harness.core.agents.planning import LocalPlan, PlanStep
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.context.builder import ContextBuilder
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.hooks import HookManager
from crazy_harness.core.models import FakeModelProvider
from crazy_harness.core.prompts import PromptPack, RuntimeManifest
from crazy_harness.core.runtime.mailbox import Delivery, DurableMailbox
from crazy_harness.core.runtime.scheduler import CooperativeScheduler
from crazy_harness.core.tools import ToolRegistry
from crazy_harness.core.tools.pipeline import OperationLedger, ToolPipeline
from crazy_harness.core.tools.policy import PolicyContext
from crazy_harness.worlds.cicd.agents import release_team_cards
from crazy_harness.worlds.cicd.tools import register_cicd_tools


def _normalize_repo_path(payload: dict) -> dict:
    if payload.get("tool_name") != "repo.read":
        return payload
    updated = dict(payload)
    updated["args"] = dict(payload["args"])
    updated["args"]["path"] = str(updated["args"]["path"]).removeprefix("./")
    return updated


@dataclass
class _AgentJob:
    agent_id: str
    assignment: Assignment
    responses: list[dict]
    loop: AgentLoop | None = None


@dataclass
class DevReleaseTeamRuntime:
    repo_path: Path
    runs_dir: Path

    def __post_init__(self) -> None:
        self.repo_path = self.repo_path.resolve()
        self.run_id = uuid4().hex
        self.root_task_id = "dev-release-team"
        self.run_dir = self.runs_dir / self.run_id
        self.event_log = EventLog(self.run_dir / "events.jsonl")
        self.artifact_store = ArtifactStore(self.run_dir / "artifacts")
        self.tools = ToolRegistry()
        register_cicd_tools(self.tools, self.repo_path)
        self.cards = release_team_cards()
        self.coordinator = Coordinator(self.cards)
        self.peer_policy = PeerPolicy()
        self.reviewer = ReviewerGate()
        self.mailboxes = {
            card.agent_id: DurableMailbox(card.agent_id, self.event_log)
            for card in self.cards
        }
        self.hooks = HookManager()
        self.hooks.register("pre_tool_use", _normalize_repo_path)
        self.scheduler = CooperativeScheduler(self.event_log)
        self._jobs: dict[str, _AgentJob] = {}
        self._job_results: dict[str, list[Event]] = {}
        for card in self.cards:
            self.scheduler.register(
                card.agent_id,
                self.mailboxes[card.agent_id],
                lambda delivery, agent_id=card.agent_id: self._on_delivery(agent_id, delivery),
            )

    def run(self) -> Path:
        self._append_root("release.requested", {"repo_path": str(self.repo_path)})

        scout_assignment = Assignment(
            assignment_id="inspect-source",
            task_id=self.root_task_id,
            goal="inspect the service source",
            required_capabilities={"repo_reading"},
            exit_criteria=["source inspected"],
        )
        scout_step = self._delegate(scout_assignment)
        scout_events = self._dispatch_agent(
            agent_id=scout_step.agent_id or "scout",
            assignment=scout_assignment,
            responses=[
                {"type": "call_tool", "reason": "inspect source", "tool_name": "repo.read", "tool_args": {"path": "./app.py"}},
                {"type": "stop", "reason": "source evidence collected"},
            ],
        )
        scout_ref = next(event.id for event in scout_events if event.type == "tool.completed")

        builder_assignment = Assignment(
            assignment_id="prepare-release",
            task_id=self.root_task_id,
            goal="test and prepare a disposable dev release plan",
            required_capabilities={"test_run", "build_plan", "release_plan"},
            exit_criteria=["tests passed", "build plan exists", "Volcengine plan exists"],
        )
        builder_step = self._delegate(builder_assignment)
        builder_events = self._dispatch_agent(
            agent_id=builder_step.agent_id or "builder",
            assignment=builder_assignment,
            responses=[
                {
                    "type": "send_message",
                    "reason": "confirm source evidence is current",
                    "receiver": "scout",
                    "message": {
                        "brief": "one-hop evidence check",
                        "scope": ["repository_evidence"],
                        "permissions": ["read_evidence"],
                        "artifact_refs": [scout_ref],
                    },
                },
                {"type": "call_tool", "reason": "run tests", "tool_name": "test.run", "tool_args": {}},
                {"type": "call_tool", "reason": "build plan", "tool_name": "build.mock_plan", "tool_args": {}},
                {"type": "call_tool", "reason": "cloud dry run", "tool_name": "volcengine.plan", "tool_args": {}},
                {"type": "stop", "reason": "release evidence collected"},
            ],
        )
        evidence_by_name = {
            event.payload["result"]["name"]: event.id
            for event in builder_events
            if event.type == "tool.completed"
        }

        contract = AssignmentContract(
            goal=builder_assignment.goal,
            exit_criteria=tuple(builder_assignment.exit_criteria),
            output_schema={
                "type": "object",
                "properties": {
                    "risk_level": {"type": "string"},
                    "approval_required": {"type": "boolean"},
                },
                "required": ["risk_level", "approval_required"],
                "additionalProperties": False,
            },
            evidence_requirements=("tests", "build_plan", "volcengine_plan"),
            permissions=("read_repo", "run_tests", "dry_run_plan"),
        )
        evidence = {
            "tests": [evidence_by_name["test.run"]],
            "build_plan": [evidence_by_name["build.mock_plan"]],
            "volcengine_plan": [evidence_by_name["volcengine.plan"]],
        }
        gate = CompletionGate().evaluate(
            contract,
            output={"risk_level": "low", "approval_required": False},
            evidence=evidence,
            pending_operations=(),
        )
        self._append_root(
            "completion.gate.passed" if gate.passed else "completion.gate.failed",
            {"assignment_id": "prepare-release", "findings": [item.model_dump(mode="json") for item in gate.findings]},
        )

        review_assignment = Assignment(
            assignment_id="review-release",
            task_id=self.root_task_id,
            goal="review release evidence",
            required_capabilities={"rubric_review", "evidence_check"},
            exit_criteria=builder_assignment.exit_criteria,
        )
        review_step = self._delegate(review_assignment)
        self._dispatch_agent(
            agent_id=review_step.agent_id or "reviewer",
            assignment=review_assignment,
            responses=[{"type": "stop", "reason": "ready to review the evidence pack"}],
        )
        pack = EvidencePack(
            assignment_id="prepare-release",
            goal=builder_assignment.goal,
            exit_criteria=builder_assignment.exit_criteria,
            candidate_artifact_refs=list(evidence_by_name.values()),
            evidence_by_criterion={
                "tests passed": [evidence_by_name["test.run"]],
                "build plan exists": [evidence_by_name["build.mock_plan"]],
                "Volcengine plan exists": [evidence_by_name["volcengine.plan"]],
            },
        )
        decision = self.reviewer.review(pack)
        self._append_root("review.completed", decision.model_dump(mode="json"))
        self._append_root("team.completed", {"verdict": decision.verdict, "gate_passed": gate.passed})
        self._write_report(decision.verdict)
        return self.run_dir

    def _delegate(self, assignment: Assignment):
        step = self.coordinator.assign(assignment)
        if step.agent_id is None:
            raise RuntimeError(f"no capable agent for {assignment.assignment_id}")
        self._append_root(
            "team.assignment.delegated",
            {"assignment_id": assignment.assignment_id, "agent_id": step.agent_id, "reason": step.reason},
        )
        self._send(
            A2AMessage(
                task_id=self.root_task_id,
                context_id=assignment.assignment_id,
                sender="coordinator",
                receiver=step.agent_id,
                performative="request",
                instruction=assignment.goal,
                brief=assignment.goal,
                expected_output={"exit_criteria": assignment.exit_criteria},
                intent="delegate",
                depth=0,
            )
        )
        return step

    def _build_agent_loop(self, *, agent_id: str, assignment: Assignment, responses: list[dict]) -> AgentLoop:
        assignment_id = assignment.assignment_id
        self.event_log.append(
            Event(
                run_id=self.run_id,
                task_id=assignment_id,
                type="assignment.created",
                source="coordinator",
                payload={"agent_id": agent_id, "contract_version": 1},
            )
        )
        plan = LocalPlan(
            version=1,
            steps=tuple(
                PlanStep(step_id=f"step-{index}", description=str(item["reason"]))
                for index, item in enumerate(responses, start=1)
                if item["type"] not in {"stop", "report_blocked"}
            ),
        )
        self.event_log.append(
            Event(
                run_id=self.run_id,
                task_id=assignment_id,
                type="plan.created",
                source=agent_id,
                payload=plan.model_dump(mode="json"),
            )
        )
        allowed_tools = frozenset(
            str(item["tool_name"])
            for item in responses
            if item["type"] == "call_tool"
        )
        contract = AssignmentContract(
            goal=assignment.goal,
            exit_criteria=tuple(assignment.exit_criteria),
            output_schema={"type": "object"},
            evidence_requirements=tuple(sorted(allowed_tools)),
            permissions=tuple(
                str(item["tool_name"])
                for item in responses
                if item["type"] == "call_tool"
            ),
        )
        tool_pipeline = ToolPipeline(
            self.tools,
            hooks=self.hooks,
            ledger=OperationLedger(self.run_dir / "operations" / f"{assignment_id}.jsonl"),
        )
        policy_context = PolicyContext(
            agent_id=agent_id,
            assignment_id=assignment_id,
            mode="mock",
            allowed_tools=allowed_tools,
        )
        context_builder = ContextBuilder(artifact_store=self.artifact_store, offload_chars=500)
        prompt_pack = PromptPack(
            prompt_version="team-mvp-1",
            role_section=f"You are the {agent_id} AgentInstance.",
            agent_card_section=next(card.role for card in self.cards if card.agent_id == agent_id),
            task_brief_section=f"Execute assignment {assignment_id} within its contract.",
            runtime_manifest=RuntimeManifest(
                agent_id=agent_id,
                task_id=assignment_id,
                mode="mock",
                available_tools=self.tools.specs(),
                workspace_policy={"root": str(self.repo_path)},
                network_policy={"default": "deny"},
            ),
        )
        return AgentLoop(
            agent_id=agent_id,
            task_id=assignment_id,
            model=FakeModelProvider([json.dumps(item) for item in responses]),
            event_log=self.event_log,
            artifact_store=self.artifact_store,
            tool_registry=self.tools,
            context_builder=context_builder,
            prompt_pack=prompt_pack,
            assignment_contract=contract,
            local_plan=plan,
            active_nudge="Keep the goal and exit criteria visible; claim only recorded evidence.",
            completion_gate=CompletionGate(),
            nudge_budget=NudgeBudget(missing_evidence=1, pending_operation=1),
            tool_pipeline=tool_pipeline,
            policy_context=policy_context,
            message_handler=lambda action, turn_id: self._handle_peer_action(
                agent_id,
                assignment,
                action,
                turn_id,
            ),
        )

    def _dispatch_agent(
        self,
        *,
        agent_id: str,
        assignment: Assignment,
        responses: list[dict],
    ) -> list[Event]:
        self._jobs[assignment.assignment_id] = _AgentJob(
            agent_id=agent_id,
            assignment=assignment,
            responses=responses,
        )
        self._consume_all(agent_id)
        self._pump_mailboxes()
        try:
            return self._job_results.pop(assignment.assignment_id)
        except KeyError as exc:
            raise RuntimeError(f"scheduler did not execute assignment {assignment.assignment_id}") from exc

    def _resume_job(self, assignment_id: str) -> None:
        job = self._jobs[assignment_id]
        if job.loop is None:
            job.loop = self._build_agent_loop(
                agent_id=job.agent_id,
                assignment=job.assignment,
                responses=job.responses,
            )
        job.loop.run_until_stop(max_steps=10)
        events = self.event_log.read_all(task_id=assignment_id)
        if any(event.type in {"agent.stopped", "agent.submitted", "agent.failed"} for event in events):
            self._job_results[assignment_id] = events
            self._jobs.pop(assignment_id)

    def _pump_mailboxes(self) -> None:
        while True:
            pending = next(
                (agent_id for agent_id, mailbox in self.mailboxes.items() if mailbox.peek() is not None),
                None,
            )
            if pending is None:
                return
            self._consume_all(pending)

    def _send(self, message: A2AMessage) -> None:
        event = Event(
            run_id=self.run_id,
            task_id=self.root_task_id,
            type="a2a.message",
            source=message.sender,
            payload=message.model_dump(mode="json"),
        )
        self.event_log.append(event)
        self.mailboxes[message.receiver].send(event, delivery_id=message.message_id)

    def _handle_peer_action(
        self,
        agent_id: str,
        assignment: Assignment,
        action: AgentAction,
        turn_id: str,
    ) -> dict[str, object]:
        requested_scope = set(action.message.get("scope", []))
        requested_permissions = set(action.message.get("permissions", []))
        contract = PeerContract(
            assignment_id=assignment.assignment_id,
            task_id=self.root_task_id,
            scope={"repository_evidence"},
            permissions={"read_evidence"},
            peer_budget=1,
            max_depth=1,
        )
        message = A2AMessage(
            task_id=self.root_task_id,
            context_id=assignment.assignment_id,
            sender=agent_id,
            receiver=action.receiver or "",
            performative="request",
            instruction=action.reason,
            brief=str(action.message.get("brief", "")),
            artifact_refs=[str(ref) for ref in action.message.get("artifact_refs", [])],
            contract_version=contract.contract_version,
            depth=1,
            intent="evidence",
        )
        decision = self.peer_policy.authorize(
            PeerRequest(
                message=message,
                scope=requested_scope,
                permissions=requested_permissions,
                budget_cost=1,
            ),
            contract,
        )
        self._append_root(
            "a2a.peer.authorized" if decision.allowed else "a2a.peer.denied",
            {
                "turn_id": turn_id,
                "sender": agent_id,
                "receiver": message.receiver,
                **decision.model_dump(mode="json"),
            },
        )
        if not decision.allowed:
            raise PermissionError(decision.reason)
        self._send(message)
        return {"message_id": message.message_id, "correlation_id": message.message_id}

    def _consume_all(self, agent_id: str) -> None:
        mailbox = self.mailboxes[agent_id]
        while mailbox.peek() is not None:
            if not self.scheduler.wake(agent_id):
                raise RuntimeError(f"mailbox delivery disappeared for {agent_id}")
            if not self.scheduler.run_once():
                raise RuntimeError(f"scheduler did not wake {agent_id}")

    def _on_delivery(self, agent_id: str, delivery: Delivery | None):
        if delivery is not None:
            self._append_root(
                "a2a.message.consumed",
                {
                    "agent_id": agent_id,
                    "delivery_id": delivery.delivery_id,
                    "message_event_id": delivery.event.id,
                },
            )
            assignment_id = str(delivery.event.payload.get("context_id", ""))
            if delivery.event.payload.get("intent") == "delegate" and assignment_id in self._jobs:
                self._resume_job(assignment_id)
            elif delivery.event.payload.get("performative") == "request":
                request = A2AMessage.model_validate(delivery.event.payload)
                self._send(
                    A2AMessage(
                        task_id=request.task_id,
                        context_id=request.context_id,
                        sender=agent_id,
                        receiver=request.sender,
                        performative="inform",
                        instruction="source evidence confirmed",
                        brief="evidence reference is current",
                        context_refs=[request.message_id],
                        artifact_refs=request.artifact_refs,
                        contract_version=request.contract_version,
                        depth=request.depth,
                        intent="evidence",
                    )
                )
            elif delivery.event.payload.get("performative") == "inform":
                response = A2AMessage.model_validate(delivery.event.payload)
                correlation_id = response.context_refs[0] if response.context_refs else response.message_id
                self.event_log.append(
                    Event(
                        run_id=self.run_id,
                        task_id=response.context_id,
                        type="a2a.peer.responded",
                        source=response.sender,
                        payload={
                            "correlation_id": correlation_id,
                            "brief": response.brief,
                            "artifact_refs": response.artifact_refs,
                        },
                    )
                )
                if response.context_id in self._jobs:
                    self._resume_job(response.context_id)
        return None

    def _append_root(self, event_type: str, payload: dict) -> Event:
        return self.event_log.append(
            Event(
                run_id=self.run_id,
                task_id=self.root_task_id,
                type=event_type,
                source="team.runtime",
                payload=payload,
            )
        )

    def _write_report(self, verdict: str) -> None:
        events = self.event_log.read_all()
        lines = [
            "# Crazy Agent Team Run",
            "",
            f"- Run ID: `{self.run_id}`",
            f"- Verdict: **{verdict}**",
            f"- Events: `{len(events)}`",
            "",
            "## Dynamic Assignments",
            "",
        ]
        for event in events:
            if event.type == "team.assignment.delegated":
                lines.append(f"- `{event.payload['assignment_id']}` -> `{event.payload['agent_id']}`")
        lines.extend(
            [
                "",
                "## Harness Evidence",
                "",
                f"- Persistent mailbox deliveries: `{sum(e.type == 'mailbox.delivery.sent' for e in events)}`",
                f"- Tool results: `{sum(e.type == 'tool.completed' for e in events)}`",
                f"- Peer checks: `{sum(e.type == 'a2a.peer.authorized' for e in events)}`",
            ]
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "team_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_dev_release_team_runtime(*, repo_path: Path, runs_dir: Path) -> DevReleaseTeamRuntime:
    return DevReleaseTeamRuntime(repo_path=repo_path, runs_dir=runs_dir)
