from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from crazy_harness.core.a2a.orchestration import TeamContract, TeamStageSpec
from crazy_harness.core.agents import AssignmentBudget, AssignmentContract
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.models import ModelProvider
from crazy_harness.core.tools import ToolRegistry
from crazy_harness.taskpacks.repo_maintainer import (
    PreparedRepoWorkspace,
    RepoMaintainerTaskPack,
)
from crazy_harness.taskpacks.repo_tools import build_repo_tools
from crazy_harness.taskpacks.resident_team import MessageHandler, ResidentDemoTeamTaskPack


class RepoMaintainerTeamTaskPack(ResidentDemoTeamTaskPack):
    """Scout, Builder, and Reviewer solve the same fixture as the Single baseline."""

    task_pack_id = "repo-maintainer"

    def __init__(self, data_dir: Path) -> None:
        self.repo = RepoMaintainerTaskPack(data_dir)

    def prepare(self, run_id: str) -> PreparedRepoWorkspace:
        return self.repo.prepare(run_id)

    def prepare_run(self, run_id: str) -> dict[str, object]:
        prepared = self.prepare(run_id)
        return {
            "workspace_path": str(prepared.workspace),
            "baseline_path": str(prepared.baseline),
            **self.repo.case_metadata(prepared),
        }

    def team_contract(self) -> TeamContract:
        stages = (
            TeamStageSpec(
                stage_id="inspect",
                result_kind="evidence",
                goal="Inspect the broken repository and persist a bounded diagnosis.",
                required_capabilities=frozenset({"repo.inspect"}),
                exit_criteria=(
                    "implementation and tests were inspected",
                    "the defect is explained from exact source evidence",
                ),
                completion_event_type="evidence.recorded",
            ),
            TeamStageSpec(
                stage_id="repair",
                result_kind="artifact",
                goal="Repair the allowlisted implementation and prove the change.",
                required_capabilities=frozenset(
                    {"repo.edit", "test.verify", "peer.request"}
                ),
                exit_criteria=(
                    "one bounded peer check completed",
                    "only the allowlisted implementation changed",
                    "tests passed and a non-empty diff was recorded",
                ),
                depends_on=("inspect",),
                completion_event_type="artifact.recorded",
            ),
            TeamStageSpec(
                stage_id="review",
                result_kind="review",
                goal="Independently verify the repaired workspace and issue a decision.",
                required_capabilities=frozenset({"repo.review", "test.verify"}),
                exit_criteria=(
                    "the real tests passed again",
                    "the diff changes only the implementation",
                    "the review decision is explicit",
                ),
                depends_on=("repair",),
                completion_event_type="review.recorded",
            ),
        )
        durable = tuple(
            stage.model_copy(
                update={"assignment_contract": self._assignment_contract(stage)}
            )
            for stage in stages
        )
        return TeamContract(
            contract_id="repo-maintainer-team-v1",
            version=1,
            max_parallel_assignments=1,
            lease_seconds=60,
            stages=durable,
            peer_contract=self.peer_contract(),
        )

    def assignment_contract(self, stage_id: str) -> AssignmentContract:
        stage = self.stage(stage_id)
        if stage.assignment_contract is None:
            raise RuntimeError(f"Repo Team stage has no durable contract: {stage_id}")
        return stage.assignment_contract

    def _assignment_contract(self, stage: TeamStageSpec) -> AssignmentContract:
        schemas = {
            "inspect": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
            "repair": {
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
        evidence = {
            "inspect": ("repo.read",),
            "repair": ("repo.write", "test.run", "repo.diff"),
            "review": ("test.run", "repo.diff"),
        }
        return AssignmentContract(
            goal=stage.goal,
            exit_criteria=stage.exit_criteria,
            output_schema=schemas[stage.stage_id],
            evidence_requirements=evidence[stage.stage_id],
            constraints=(
                "do not modify tests",
                "remain inside the disposable workspace",
                "treat public Team events as references, not private context",
            ),
            permissions=tuple(evidence[stage.stage_id]),
            budgets=AssignmentBudget(
                turns=8,
                tool_calls=5,
                retries=1,
                wall_time_seconds=180,
            ),
        )

    def peer_contract(self) -> AssignmentContract:
        return AssignmentContract(
            goal="Cross-check the implementation from a bounded read-only peer request.",
            exit_criteria=(
                "the requested implementation was inspected",
                "the response contains a concise evidence-based brief",
            ),
            output_schema={
                "type": "object",
                "properties": {"brief": {"type": "string"}},
                "required": ["brief"],
                "additionalProperties": False,
            },
            evidence_requirements=("repo.read",),
            constraints=("one hop only", "read-only", "do not share private context"),
            permissions=("repo.read",),
            budgets=AssignmentBudget(
                turns=4,
                tool_calls=1,
                retries=1,
                wall_time_seconds=60,
            ),
        )

    def scripted_assignment_responses(
        self, stage_id: str, *, peer_receiver: str = "scout"
    ) -> list[str]:
        actions = {
            "inspect": [
                self._call("repo.read", "inspect implementation", {"path": "calculator.py"}),
                self._call(
                    "repo.read",
                    "inspect immutable tests",
                    {"path": "tests/test_calculator.py"},
                ),
                {
                    "type": "submit_output",
                    "reason": "the exact source and tests establish the diagnosis",
                    "artifact": {
                        "summary": "clamp applies min/max in the wrong order and violates both bounds"
                    },
                },
            ],
            "repair": [
                {
                    "type": "send_message",
                    "reason": "cross-check the diagnosis before editing",
                    "receiver": peer_receiver,
                    "message": {
                        "brief": "Confirm the clamp bounds defect from the current source.",
                        "scope": ["evidence"],
                        "permissions": ["read"],
                        "depth": 1,
                        "peer_budget": 1,
                    },
                },
                self._call(
                    "repo.write",
                    "apply the bounded repair",
                    {"path": "calculator.py", "content": self.repo.fixed_source()},
                ),
                self._call("test.run", "prove the repaired behavior", {}),
                self._call("repo.diff", "record the implementation diff", {}),
                {
                    "type": "submit_output",
                    "reason": "peer check, tests, and diff prove the repair",
                    "artifact": {
                        "title": "Clamp bounds repair",
                        "summary": "Corrected the nested min/max order in calculator.py.",
                        "content": {
                            "steps": ["inspect", "repair", "test", "diff"],
                            "rollback": "restore calculator.py from the immutable baseline",
                        },
                    },
                },
            ],
            "review": [
                self._call("repo.read", "inspect the final implementation", {"path": "calculator.py"}),
                self._call("test.run", "independently rerun the tests", {}),
                self._call("repo.diff", "independently inspect the diff", {}),
                {
                    "type": "submit_output",
                    "reason": "the final source, tests, and diff satisfy the review contract",
                    "artifact": {
                        "decision": "approved",
                        "summary": "Tests pass and only calculator.py differs from baseline.",
                    },
                },
            ],
        }
        try:
            return [json.dumps(action, ensure_ascii=False) for action in actions[stage_id]]
        except KeyError as exc:
            raise KeyError(f"unknown Repo Team stage: {stage_id}") from exc

    def scripted_peer_responses(self) -> list[str]:
        return [
            json.dumps(
                self._call(
                    "repo.read",
                    "inspect the implementation for the bounded peer request",
                    {"path": "calculator.py"},
                ),
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "type": "submit_output",
                    "reason": "the implementation confirms the reported ordering defect",
                    "artifact": {
                        "brief": "Confirmed: the nested min/max order reverses clamp behavior."
                    },
                },
                ensure_ascii=False,
            ),
        ]

    def scripted_comparison_manifest_hash(self) -> str:
        manifest = {
            "single": self.repo.scripted_responses(),
            "team_assignments": {
                stage_id: self.scripted_assignment_responses(stage_id)
                for stage_id in ("inspect", "repair", "review")
            },
            "team_peer": self.scripted_peer_responses(),
        }
        encoded = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

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
        model_mode: str,
        model: ModelProvider,
        event_log,
        artifact_store: ArtifactStore,
        ledger_path: Path,
        assignment_contract: AssignmentContract,
        message_handler: MessageHandler,
        fault_injector: Callable[[str], None] | None = None,
    ):
        tools = self._stage_tools(run_id, stage_id)
        return self._build_loop(
            run_id=run_id,
            root_task_id=root_task_id,
            task_id=task_id,
            assignment_id=assignment_id,
            agent_id=agent_id,
            brief=brief,
            model_mode=model_mode,
            model=model,
            event_log=event_log,
            artifact_store=artifact_store,
            ledger_path=ledger_path,
            contract=assignment_contract,
            tools=tools,
            plan_steps=(
                "Read the persisted AssignmentContract and public evidence references.",
                "Use only this role's bounded repository tools.",
                "Submit only after the mechanical evidence requirements are complete.",
            ),
            message_handler=message_handler,
            fault_injector=fault_injector,
            approved_tools=(
                frozenset({"repo.write"})
                if stage_id == "repair"
                else frozenset()
            ),
            destructive_modes=("scripted", "deepseek"),
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
        model_mode: str,
        model: ModelProvider,
        event_log,
        artifact_store: ArtifactStore,
        ledger_path: Path,
        assignment_contract: AssignmentContract,
        fault_injector: Callable[[str], None] | None = None,
    ):
        return self._build_loop(
            run_id=run_id,
            root_task_id=root_task_id,
            task_id=task_id,
            assignment_id=f"peer:{correlation_id}",
            agent_id=agent_id,
            brief=brief,
            model_mode=model_mode,
            model=model,
            event_log=event_log,
            artifact_store=artifact_store,
            ledger_path=ledger_path,
            contract=assignment_contract,
            tools=self._repo_tools(run_id, frozenset({"repo.read"})),
            plan_steps=(
                "Inspect the bounded peer request.",
                "Read only the requested implementation evidence.",
                "Return one concise evidence capsule.",
            ),
            message_handler=None,
            fault_injector=fault_injector,
        )

    def _stage_tools(self, run_id: str, stage_id: str) -> ToolRegistry:
        allowed = {
            "inspect": frozenset({"repo.read"}),
            "repair": frozenset({"repo.read", "repo.write", "test.run", "repo.diff"}),
            "review": frozenset({"repo.read", "test.run", "repo.diff"}),
        }
        try:
            return self._repo_tools(run_id, allowed[stage_id])
        except KeyError as exc:
            raise KeyError(f"unknown Repo Team stage: {stage_id}") from exc

    def _repo_tools(self, run_id: str, allowed: frozenset[str]) -> ToolRegistry:
        prepared = self.prepare(run_id)
        tools = build_repo_tools(
            prepared.workspace,
            prepared.baseline,
            writable_paths=self.repo.writable_paths,
        )
        for spec in tuple(tools.specs()):
            if spec.name not in allowed:
                tools.unregister(spec.name)
        return tools

    @staticmethod
    def _call(name: str, reason: str, args: dict[str, object]) -> dict[str, object]:
        return {
            "type": "call_tool",
            "reason": reason,
            "tool_name": name,
            "tool_args": args,
        }
