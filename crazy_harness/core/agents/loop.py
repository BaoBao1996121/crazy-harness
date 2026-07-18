from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection
from dataclasses import dataclass
from uuid import uuid4

from pydantic import ValidationError

from crazy_harness.core.agents.actions import AgentAction
from crazy_harness.core.agents.completion import (
    CompletionFindingCode,
    CompletionGate,
    NudgeBudget,
    NudgeKind,
)
from crazy_harness.core.agents.contracts import AssignmentContract
from crazy_harness.core.agents.planning import LocalPlan
from crazy_harness.core.agents.state import LoopPhase, transition_allowed
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.capabilities import (
    CAPABILITY_SEARCH_TOOL_NAME,
    CapabilityCompileRequest,
    CapabilityCompiler,
    CapabilityManifest,
    CapabilitySearchResult,
)
from crazy_harness.core.context.builder import ContextBuilder
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.models import ModelMessage, ModelProvider
from crazy_harness.core.prompts import PromptPack
from crazy_harness.core.skills import active_skill_activations
from crazy_harness.core.tools import ToolCall, ToolRegistry
from crazy_harness.core.tools.concurrency import ToolInvocation
from crazy_harness.core.tools.pipeline import OperationRecord, OperationState, ToolPipeline, ToolRequest, ToolValidationError
from crazy_harness.core.tools.policy import PolicyContext, PolicyDenied

FaultInjector = Callable[[str], None]
MessageHandler = Callable[[AgentAction, str], dict[str, object] | None]


class InjectedCrash(RuntimeError):
    """Deliberate process-crash stand-in used by the learning labs."""


# 这些事件表示“本轮已经有了确定结果”，恢复时不能再次执行同一轮命令。
# 注意：本轮结束不等于整个任务结束，例如 tool.completed 后还会进入下一轮。
_TURN_COMPLETED_EVENTS = {
    "model.validation_failed",
    "agent.action.denied",
    "tool.completed",
    "tool.failed",
    "agent.stopped",
    "agent.submitted",
    "agent.waiting",
    "agent.nudged",
    "agent.continued",
    "a2a.message.sent",
    "a2a.message.denied",
    "artifact.created",
    "operation.unknown",
}

# 这些事件才表示整个 Assignment 已经结束，后续不应再调用模型。
_RUN_TERMINAL_EVENTS = {"agent.stopped", "agent.submitted", "agent.failed"}


@dataclass
class AgentLoop:
    """可恢复的单 Agent 控制循环；模型只提议，Harness 掌握执行与事实记录。"""

    agent_id: str
    model: ModelProvider
    event_log: EventLog
    artifact_store: ArtifactStore
    tool_registry: ToolRegistry
    task_id: str | None = None
    fault_injector: FaultInjector | None = None
    context_builder: ContextBuilder | None = None
    prompt_pack: PromptPack | None = None
    assignment_contract: AssignmentContract | None = None
    local_plan: LocalPlan | None = None
    active_nudge: str | None = None
    completion_gate: CompletionGate | None = None
    nudge_budget: NudgeBudget | None = None
    tool_pipeline: ToolPipeline | None = None
    policy_context: PolicyContext | None = None
    message_handler: MessageHandler | None = None
    capability_compiler: CapabilityCompiler | None = None
    capability_always_include: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.tool_pipeline is not None and self.policy_context is None:
            raise ValueError("policy_context is required when tool_pipeline is configured")

    def run_once(self) -> None:
        """推进一个回合；若发现等待、终态或待对账副作用，则不启动新回合。"""

        # EventLog 是事实源。每次进入都重新读取，不能依赖上次进程留下的内存状态。
        events = self._events()
        if not events:
            raise RuntimeError("AgentLoop requires a seed event")

        # 整个任务已结束、存在 UNKNOWN，或仍在等待外部事件时，都不能继续调用模型。
        if self._is_terminal(events) or self._has_unresolved_unknown(events) or self._has_active_wait(events):
            return

        # operation.started 已落盘但没有 terminal 事件，说明工具效果可能发生而结果尚未记下。
        # 优先查 OperationLedger 恢复；无法确认时标记 UNKNOWN，禁止盲目重试副作用。
        unresolved = self._unresolved_operation(events)
        if unresolved is not None:
            if self._recover_pipeline_operation(unresolved):
                return
            turn_id = str(unresolved.payload["turn_id"])
            self._phase(LoopPhase.RESULT_RECORDING, turn_id)
            self._append(
                "operation.unknown",
                {
                    "turn_id": turn_id,
                    "operation_id": unresolved.payload["operation_id"],
                    "tool_name": unresolved.payload["tool_name"],
                    "reason": "effect may have happened before result persistence",
                },
                causation_id=unresolved.id,
            )
            return

        # 查找“模型响应已落盘、但本轮尚未完成”的回合。崩溃恢复时复用响应，避免再次采样。
        turn_id, turn_events = self._recoverable_turn(events)
        response_event = self._last_of_type(turn_events, "model.completed")
        command_event = self._last_of_type(turn_events, "agent.command.validated")
        capability_event = self._last_of_type(turn_events, "capability.manifest.compiled")
        response_recovered = response_event is not None

        # 没有可复用响应，才创建新 turn、编译上下文并真正调用模型。
        if response_event is None:
            turn_id = self._next_turn_id(events)
            self._phase(LoopPhase.CONTEXT_BUILDING, turn_id)
            capability_event = self._compile_capabilities(events, turn_id=turn_id)
            messages, prompt_hash = self._build_messages(events, capability_event=capability_event)

            # Manifest 记录本轮模型看见/没看见什么，便于审计 Context 编译结果。
            context_event = None
            if self.context_builder is not None and self.context_builder.last_manifest is not None:
                manifest = self.context_builder.last_manifest
                manifest_payload = manifest.model_dump(mode="json")
                context_event = self._append(
                    "context.manifest.compiled",
                    {
                        "turn_id": turn_id,
                        "agent_id": self.agent_id,
                        "trigger_event_id": events[-1].id,
                        "context_epoch": self._turn_number(turn_id),
                        "manifest": manifest_payload,
                        "microcompact": {
                            "retained_count": len(manifest.included_refs),
                            "discarded_count": len(manifest.excluded_refs),
                            "offloaded_count": sum(
                                item.representation.value == "ref" for item in manifest.transform
                            ),
                        },
                        "message_preview": [],
                        **manifest_payload,
                    },
                    causation_id=(
                        capability_event.id if capability_event is not None else events[-1].id
                    ),
                )
            self._phase(LoopPhase.MODEL_CALLING, turn_id)
            request_trigger = context_event or capability_event or events[-1]
            requested = self._append(
                "model.requested",
                {
                    "turn_id": turn_id,
                    "message_count": len(messages),
                    "prompt_hash": prompt_hash,
                    "contract_version": self.assignment_contract.version if self.assignment_contract else None,
                    "local_plan_version": self.local_plan.version if self.local_plan else 0,
                },
                causation_id=request_trigger.id,
            )

            # model.completed 是模型响应的持久边界：它落盘后即使进程崩溃也应复用。
            response = self.model.complete(
                messages,
                tools=self._tool_schemas(self._disclosed_names(capability_event)),
                response_schema=AgentAction.model_json_schema(),
            )
            response_event = self._append(
                "model.completed",
                {
                    "turn_id": turn_id,
                    "content": response.content,
                    "raw": response.raw,
                    "usage": response.usage,
                },
                causation_id=requested.id,
            )
            self._fault("after_model_persisted")

        if response_recovered and self._last_of_type(turn_events, "model.response.reused") is None:
            self._append(
                "model.response.reused",
                {
                    "turn_id": turn_id,
                    "response_event_id": response_event.id,
                    "reason": "persisted response precedes the interrupted harness step",
                },
                causation_id=response_event.id,
            )

        # 模型文本只是候选建议；必须先转成严格 AgentAction，校验失败不得产生副作用。
        if command_event is None:
            self._phase(LoopPhase.DECISION_VALIDATING, turn_id)
            try:
                action = AgentAction.model_validate_json(response_event.payload["content"])
            except (ValidationError, ValueError, TypeError) as exc:
                self._phase(LoopPhase.FAILED, turn_id)
                self._append(
                    "model.validation_failed",
                    {"turn_id": turn_id, "error": str(exc), "raw_content": response_event.payload.get("content", "")},
                    causation_id=response_event.id,
                )
                return
            command_event = self._append(
                "agent.command.validated",
                {"turn_id": turn_id, "command": action.model_dump(mode="json")},
                causation_id=response_event.id,
            )
            self._fault("after_command_persisted")
        else:
            # command 已持久化时直接重建，不重新解析或重新调用模型。
            if self._last_of_type(turn_events, "agent.command.reused") is None:
                self._append(
                    "agent.command.reused",
                    {
                        "turn_id": turn_id,
                        "command_event_id": command_event.id,
                        "reason": "validated command precedes the interrupted execution step",
                    },
                    causation_id=command_event.id,
                )
            action = AgentAction.model_validate(command_event.payload["command"])

        # 结构合法不等于有权限执行。这里先做工具存在性检查，完整 Policy 在 Pipeline 中执行。
        self._phase(LoopPhase.ACTION_AUTHORIZING, turn_id)
        if action.type == "call_tool" and not self.tool_registry.has(action.tool_name or ""):
            self._phase(LoopPhase.FAILED, turn_id)
            self._append(
                "agent.action.denied",
                {"turn_id": turn_id, "reason": f"unknown tool: {action.tool_name}"},
                causation_id=command_event.id,
            )
            return

        # Native tool schema 是本轮能力契约。即使模型手写了注册表中的隐藏工具名，
        # 也不能绕过 progressive disclosure；执行期 ToolPolicy 仍会再做一次权限校验。
        disclosed_names = self._disclosed_names(capability_event)
        if (
            action.type == "call_tool"
            and self.capability_compiler is not None
            and disclosed_names is not None
            and action.tool_name not in disclosed_names
        ):
            self._phase(LoopPhase.FAILED, turn_id)
            self._append(
                "agent.action.denied",
                {
                    "turn_id": turn_id,
                    "reason": "tool_not_disclosed",
                    "tool_name": action.tool_name,
                    "capability_manifest_event_id": capability_event.id if capability_event else None,
                },
                causation_id=command_event.id,
            )
            return

        # Budget 是 Harness 的硬授权边界，不是给模型看的建议。按 operation.started
        # 计数可覆盖“外部效果发生后、完成事件落盘前崩溃”的调用，避免重启后漏算。
        if action.type == "call_tool" and self.assignment_contract is not None:
            limit = self.assignment_contract.budgets.tool_calls
            used = sum(event.type == "operation.started" for event in events)
            if limit is not None and used >= limit:
                self._phase(LoopPhase.FAILED, turn_id)
                self._append(
                    "agent.action.denied",
                    {
                        "turn_id": turn_id,
                        "reason": "tool_call_budget_exhausted",
                        "tool_name": action.tool_name,
                        "used": used,
                        "limit": limit,
                    },
                    causation_id=command_event.id,
                )
                return

        # 只有通过校验和授权的 Action 才能进入执行/记录阶段。
        self._execute(action, turn_id=turn_id, command_event=command_event)

    def run_until_stop(self, *, max_steps: int = 20) -> None:
        """反复推进单回合，直到终态、等待、UNKNOWN、无进展或达到安全轮数上限。"""

        for _ in range(max_steps):
            before = len(self._events())
            self.run_once()
            events = self._events()

            # WAITING 会释放执行权，UNKNOWN 要先对账；两者都不是继续空转模型的理由。
            if self._is_terminal(events) or self._has_unresolved_unknown(events) or self._has_active_wait(events):
                return

            # 没有新增事件意味着本轮没有取得进展，立即退出以避免忙循环。
            if len(events) == before:
                return

    def _execute(self, action: AgentAction, *, turn_id: str, command_event: Event) -> None:
        """分发一个已校验 Action，并把真实执行结果写成事件。"""

        if action.type == "call_tool":
            # 正式路径交给 ToolPipeline，统一完成 Hook、重校验、Policy、Ledger 和调度。
            if self.tool_pipeline is not None:
                self._execute_via_pipeline(action, turn_id=turn_id, command_event=command_event)
                return

            # 兼容最小配置的直连路径；仍先记录 operation.started，再执行工具和记录结果。
            self._phase(LoopPhase.ACTION_EXECUTING, turn_id)
            operation_id = f"op_{uuid4().hex}"
            operation = self._append(
                "operation.started",
                {
                    "turn_id": turn_id,
                    "operation_id": operation_id,
                    "tool_name": action.tool_name,
                    "tool_args": action.tool_args,
                },
                causation_id=command_event.id,
            )
            self._append(
                "tool.requested",
                {
                    "turn_id": turn_id,
                    "operation_id": operation_id,
                    "tool_name": action.tool_name,
                    "tool_args": action.tool_args,
                },
                causation_id=operation.id,
            )
            result = self.tool_registry.call(ToolCall(name=action.tool_name or "", args=action.tool_args))
            self._fault("after_tool_effect")
            self._phase(LoopPhase.RESULT_RECORDING, turn_id)
            completed = self._append(
                "tool.completed",
                {"turn_id": turn_id, "operation_id": operation_id, "result": result.model_dump(mode="json")},
                causation_id=operation.id,
            )
            self._append(
                "operation.completed",
                {"turn_id": turn_id, "operation_id": operation_id, "result_event_id": completed.id},
                causation_id=completed.id,
            )
            return

        # 非工具 Action 不产生 ToolResult，但仍要记录它造成的 Harness 状态变化。
        self._phase(LoopPhase.RESULT_RECORDING, turn_id)

        # stop/submit 只是模型的完成申请，必须先通过机械准出条件。
        if action.type in {"stop", "submit_output"} and not self._completion_allows(
            action,
            turn_id=turn_id,
            command_event=command_event,
        ):
            return
        if action.type in {"stop", "report_blocked"}:
            self._append(
                "agent.stopped",
                {"turn_id": turn_id, "reason": action.reason, "status": action.type},
                causation_id=command_event.id,
            )
        elif action.type == "submit_output":
            self._phase(LoopPhase.SUBMITTED, turn_id)
            self._append(
                "agent.submitted",
                {"turn_id": turn_id, "artifact": action.artifact, "reason": action.reason},
                causation_id=command_event.id,
            )
        elif action.type in {"wait_for_event", "request_human"}:
            # 等待条件持久化后退出当前运行槽；后续由相关事件重新唤醒。
            self._phase(LoopPhase.WAITING, turn_id)
            self._append(
                "agent.waiting",
                {"turn_id": turn_id, "reason": action.reason, "condition": action.message},
                causation_id=command_event.id,
            )
        elif action.type == "emit_artifact":
            # 大型或结构化产物进入 ArtifactStore，EventLog 只保存可追踪引用。
            ref = self.artifact_store.write_json("agent_output", action.artifact, summary=action.reason)
            self._append(
                "artifact.created",
                {"turn_id": turn_id, "artifact_ref": ref.model_dump(mode="json")},
                causation_id=command_event.id,
            )
        elif action.type == "send_message":
            # A2A 消息先交给受控 handler；拒绝与成功都会成为可审计事件。
            handler_result: dict[str, object] = {}
            if self.message_handler is not None:
                try:
                    handler_result = self.message_handler(action, turn_id) or {}
                except (PermissionError, ValueError) as exc:
                    self._append(
                        "a2a.message.denied",
                        {
                            "turn_id": turn_id,
                            "receiver": action.receiver,
                            "message": action.message,
                            "reason": str(exc),
                        },
                        causation_id=command_event.id,
                    )
                    return
            self._append(
                "a2a.message.sent",
                {
                    "turn_id": turn_id,
                    "receiver": action.receiver,
                    "message": action.message,
                    **handler_result,
                },
                causation_id=command_event.id,
            )
            if self.message_handler is not None:
                self._phase(LoopPhase.WAITING, turn_id)
                self._append(
                    "agent.waiting",
                    {
                        "turn_id": turn_id,
                        "reason": "waiting for peer response",
                        "condition": handler_result,
                        "correlation_id": handler_result.get("correlation_id"),
                    },
                    causation_id=command_event.id,
                )
        elif action.type == "continue":
            self._append(
                "agent.continued",
                {"turn_id": turn_id, "reason": action.reason},
                causation_id=command_event.id,
            )

    def _phase(self, phase: LoopPhase, turn_id: str) -> None:
        """校验并记录显式阶段迁移，阻止不合法的控制流跳转。"""

        previous_event = self._last_phase_event(turn_id)
        if previous_event is not None:
            previous = LoopPhase(previous_event.payload["phase"])
            if not transition_allowed(previous, phase):
                raise RuntimeError(f"illegal loop transition: {previous.value} -> {phase.value}")
        self._append("loop.phase.changed", {"turn_id": turn_id, "phase": phase.value})

    def _build_messages(
        self,
        events: list[Event],
        *,
        capability_event: Event | None = None,
    ) -> tuple[list[ModelMessage], str]:
        """从持久事实编译本轮临时 Context，并返回可审计的 Prompt 哈希。"""

        if self.context_builder is not None:
            # 正式路径由 ContextBuilder 决定 inline、offload、discard 等表示方式。
            selected = self.context_builder.build_messages(events)
            context_view = [item["content"] for item in selected]
        else:
            # 最小降级路径只暴露最近事件，保证没有 ContextBuilder 时仍可运行。
            visible = [
                {"type": event.type, "source": event.source, "payload": event.payload}
                for event in events[-20:]
                if event.type != "loop.phase.changed"
            ]
            context_view = [json.dumps(visible, ensure_ascii=False, default=str)]

        if self.prompt_pack is not None:
            # Contract、Plan、Nudge 等受保护信息通过 latest-only slot 注入。
            prompt_update: dict[str, object] = {
                "context_view": context_view,
                "task_brief_section": self._protected_task_brief(),
            }
            disclosed_names = self._disclosed_names(capability_event)
            if disclosed_names is not None:
                visible_specs = [
                    self.tool_registry.spec(name)
                    for name in disclosed_names
                    if self.tool_registry.has(name)
                ]
                prompt_update["runtime_manifest"] = self.prompt_pack.runtime_manifest.model_copy(
                    update={"available_tools": visible_specs}
                )
            return self.prompt_pack.model_copy(update=prompt_update).compile()

        messages = [
            ModelMessage(
                role="system",
                content=(
                    "Propose exactly one JSON action. The harness validates and executes it. "
                    "Use call_tool for evidence and stop only when no further action is needed."
                ),
            ),
            ModelMessage(role="user", content="\n\n".join(context_view)),
        ]
        canonical = json.dumps([message.model_dump(mode="json") for message in messages], sort_keys=True)
        return messages, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _protected_task_brief(self) -> str:
        """组装每轮只保留最新版的 Contract、LocalPlan 与 Nudge 保护槽。"""

        if self.prompt_pack is None:
            return ""
        protected: dict[str, object] = {}
        if self.assignment_contract is not None:
            protected["assignment_contract"] = self.assignment_contract.model_dump(mode="json")
        if self.local_plan is not None:
            protected["local_plan"] = self.local_plan.model_dump(mode="json")
        active_nudge = self.active_nudge or self._latest_active_nudge()
        if active_nudge:
            protected["active_nudge"] = active_nudge
        active_skills = active_skill_activations(self._events())
        if active_skills:
            protected["active_skills"] = [
                {
                    **activation.model_dump(mode="json"),
                    "allowed_tools_hint_is_authority": False,
                }
                for activation in active_skills
            ]
        if not protected:
            return self.prompt_pack.task_brief_section
        return (
            f"{self.prompt_pack.task_brief_section}\n\n"
            "## Protected latest-only slots\n"
            f"{json.dumps(protected, ensure_ascii=False, sort_keys=True)}"
        )

    def _completion_allows(self, action: AgentAction, *, turn_id: str, command_event: Event) -> bool:
        """机械检查完成申请；证据不足时 nudge，预算耗尽后以 blocked 结束。"""

        if self.completion_gate is None or self.assignment_contract is None:
            return True
        events = self._events()

        # 只有真实 tool.completed 才算证据，模型在 reason 中声称“已完成”不算。
        evidence: dict[str, list[str]] = {}
        for event in events:
            if event.type != "tool.completed":
                continue
            result = event.payload.get("result", {})
            name = result.get("name") if isinstance(result, dict) else None
            if name:
                evidence.setdefault(str(name), []).append(event.id)

        # started - terminal 得到尚未确定完成/失败/UNKNOWN 的外部操作。
        started = {
            str(event.payload["operation_id"])
            for event in events
            if event.type == "operation.started"
        }
        terminal = {
            str(event.payload["operation_id"])
            for event in events
            if event.type in {"operation.completed", "operation.failed", "operation.unknown"}
        }
        output = action.artifact if action.type == "submit_output" and action.artifact is not None else {}
        result = self.completion_gate.evaluate(
            self.assignment_contract,
            output=output,
            evidence=evidence,
            pending_operations=started - terminal,
        )
        self._append(
            "completion.gate.passed" if result.passed else "completion.gate.failed",
            {
                "turn_id": turn_id,
                "findings": [finding.model_dump(mode="json") for finding in result.findings],
            },
            causation_id=command_event.id,
        )
        if result.passed:
            return True

        # Gate 失败时根据首个问题选择定向 Nudge；每类 Nudge 都有独立预算。
        kind_by_code = {
            CompletionFindingCode.SCHEMA: NudgeKind.SCHEMA,
            CompletionFindingCode.EVIDENCE: NudgeKind.EVIDENCE,
            CompletionFindingCode.PENDING_OPERATION: NudgeKind.PENDING_OPERATION,
        }
        kind = kind_by_code[result.findings[0].code]
        limit = self.nudge_budget.remaining(kind) if self.nudge_budget is not None else 0
        used = sum(
            event.type == "agent.nudged" and event.payload.get("kind") == kind.value
            for event in events
        )
        findings = [finding.model_dump(mode="json") for finding in result.findings]
        if used < limit:
            message = "; ".join(finding.message for finding in result.findings)
            self._append(
                "agent.nudged",
                {
                    "turn_id": turn_id,
                    "kind": kind.value,
                    "message": message,
                    "findings": findings,
                    "remaining": limit - used - 1,
                },
                causation_id=command_event.id,
            )
            return False

        # 反复提醒仍无法满足准出条件时停止空转，并明确报告 blocked。
        self._append(
            "agent.stopped",
            {
                "turn_id": turn_id,
                "reason": "completion gate failed and nudge budget is exhausted",
                "status": "report_blocked",
                "findings": findings,
            },
            causation_id=command_event.id,
        )
        return False

    def _latest_active_nudge(self) -> str | None:
        """返回仍有效的最近 Nudge；一旦有新工具证据，它就不再继续注入。"""

        events = self._events()
        last_nudge_index = next(
            (index for index in range(len(events) - 1, -1, -1) if events[index].type == "agent.nudged"),
            None,
        )
        if last_nudge_index is None:
            return None
        if any(event.type == "tool.completed" for event in events[last_nudge_index + 1 :]):
            return None
        return str(events[last_nudge_index].payload.get("message", "")) or None

    def _execute_via_pipeline(self, action: AgentAction, *, turn_id: str, command_event: Event) -> None:
        """经完整 ToolPipeline 执行单个工具请求，并同步 Ledger 与 EventLog。"""

        assert self.tool_pipeline is not None
        assert self.policy_context is not None
        self._phase(LoopPhase.ACTION_EXECUTING, turn_id)
        call_id = f"{self.task_id or 'task'}:{turn_id}:{action.tool_name}"
        request = ToolRequest(
            call=ToolCall(name=action.tool_name or "", args=action.tool_args),
            call_id=call_id,
            idempotency_key=call_id,
        )
        operation_event: Event | None = None

        def record_started(record: OperationRecord, invocation: ToolInvocation) -> None:
            """在外部效果发生前，先把最终参数和幂等键写入 EventLog。"""

            nonlocal operation_event
            # Hook 可能修改参数，因此同时保留模型提议值和真正执行值。
            effective_args = invocation.call.args
            operation_event = self._append(
                "operation.started",
                {
                    "turn_id": turn_id,
                    "operation_id": record.operation_id,
                    "tool_name": action.tool_name,
                    "proposed_tool_args": action.tool_args,
                    "tool_args": effective_args,
                    "hook_patched": effective_args != action.tool_args,
                    "idempotency_key": record.idempotency_key,
                },
                causation_id=command_event.id,
            )
            self._append(
                "tool.requested",
                {
                    "turn_id": turn_id,
                    "operation_id": record.operation_id,
                    "tool_name": action.tool_name,
                    "tool_args": effective_args,
                },
                causation_id=operation_event.id,
            )

        try:
            # Pipeline 内部顺序：schema -> Hook -> revalidate -> Policy -> Ledger -> execute。
            execution = self.tool_pipeline.execute(
                [request],
                self.policy_context,
                on_started=record_started,
            )
        except (PolicyDenied, ToolValidationError) as exc:
            self._phase(LoopPhase.FAILED, turn_id)
            self._append(
                "agent.action.denied",
                {"turn_id": turn_id, "reason": str(exc), "tool_name": action.tool_name},
                causation_id=command_event.id,
            )
            return

        # all-settled 结果在这里投影为 AgentLoop 事件；模型下一轮只看已记录的结果。
        settled = execution.results[0]
        self._fault("after_tool_effect")
        self._phase(LoopPhase.RESULT_RECORDING, turn_id)
        if settled.status in {"fulfilled", "cached"} and settled.result is not None:
            if operation_event is None:
                operation_event = next(
                    (
                        event
                        for event in reversed(self._events())
                        if event.type == "operation.started"
                        and event.payload.get("operation_id") == settled.operation_id
                    ),
                    None,
                )
            if operation_event is None:
                raise RuntimeError(
                    f"settled operation has no durable start: {settled.operation_id}"
                )
            completed = self._append(
                "tool.completed",
                {
                    "turn_id": turn_id,
                    "operation_id": settled.operation_id,
                    "result": settled.result.model_dump(mode="json"),
                    "cached": settled.status == "cached",
                },
                causation_id=operation_event.id,
            )
            self._append(
                "operation.completed",
                {"turn_id": turn_id, "operation_id": settled.operation_id, "result_event_id": completed.id},
                causation_id=completed.id,
            )
        elif settled.status == "unknown":
            self._append(
                "operation.unknown",
                {"turn_id": turn_id, "operation_id": settled.operation_id, "reason": settled.error},
                causation_id=command_event.id,
            )
        else:
            failed = self._append(
                "tool.failed",
                {
                    "turn_id": turn_id,
                    "operation_id": settled.operation_id,
                    "error": settled.error,
                    "result": settled.result.model_dump(mode="json") if settled.result else None,
                },
                causation_id=command_event.id,
            )
            self._append(
                "operation.failed",
                {"turn_id": turn_id, "operation_id": settled.operation_id, "error": settled.error},
                causation_id=failed.id,
            )

    def _recover_pipeline_operation(self, unresolved: Event) -> bool:
        """用持久 Ledger 对账未收尾操作；能确认则补事件，否则标记 UNKNOWN。"""

        if self.tool_pipeline is None:
            return False
        operation_id = str(unresolved.payload["operation_id"])
        try:
            record = self.tool_pipeline.ledger.get(operation_id)
        except KeyError:
            return False

        turn_id = str(unresolved.payload["turn_id"])
        self._phase(LoopPhase.RESULT_RECORDING, turn_id)
        if record.state is OperationState.SUCCEEDED and record.result is not None:
            # 工具已成功，只是 Agent EventLog 来不及记结果；从 Ledger 补写，不重做工具。
            completed = self._append(
                "tool.completed",
                {
                    "turn_id": turn_id,
                    "operation_id": operation_id,
                    "result": record.result.model_dump(mode="json"),
                    "recovered_from_ledger": True,
                },
                causation_id=unresolved.id,
            )
            self._append(
                "operation.completed",
                {"turn_id": turn_id, "operation_id": operation_id, "result_event_id": completed.id},
                causation_id=completed.id,
            )
        elif record.state is OperationState.FAILED:
            # Ledger 已确认失败，补齐 operation.failed 即可。
            self._append(
                "operation.failed",
                {"turn_id": turn_id, "operation_id": operation_id, "error": record.error},
                causation_id=unresolved.id,
            )
        else:
            # 无法证明成功或失败时保持 UNKNOWN，等待人工或外部系统 reconciliation。
            self._append(
                "operation.unknown",
                {
                    "turn_id": turn_id,
                    "operation_id": operation_id,
                    "tool_name": unresolved.payload.get("tool_name"),
                    "reason": record.error or f"ledger state is {record.state.value}",
                },
                causation_id=unresolved.id,
            )
        return True

    def _compile_capabilities(self, events: list[Event], *, turn_id: str) -> Event | None:
        """Compile and persist the exact capability subset visible to this model turn."""

        if self.capability_compiler is None:
            return None
        registered_names = frozenset(spec.name for spec in self.tool_registry.specs())
        allowed_names = (
            self.policy_context.allowed_tools
            if self.policy_context is not None
            else registered_names
        )
        query_parts: list[str] = []
        if self.assignment_contract is not None:
            query_parts.extend((self.assignment_contract.goal, *self.assignment_contract.exit_criteria))
        if self.local_plan is not None:
            query_parts.extend(step.description for step in self.local_plan.steps)
        if self.active_nudge:
            query_parts.append(self.active_nudge)
        recall_sources = self._capability_recall_sources(events)
        compiled = self.capability_compiler.compile(
            CapabilityCompileRequest(
                agent_id=self.agent_id,
                assignment_id=self.task_id or self.agent_id,
                mode=self.policy_context.mode if self.policy_context is not None else "default",
                query="\n".join(query_parts),
                allowed_names=allowed_names,
                always_include=self.capability_always_include,
                explicit_names=tuple(recall_sources),
                explicit_sources=recall_sources,
            )
        )
        manifest_payload = compiled.manifest.model_dump(mode="json")
        return self._append(
            "capability.manifest.compiled",
            {
                "turn_id": turn_id,
                "agent_id": self.agent_id,
                "assignment_id": self.task_id or self.agent_id,
                "manifest": manifest_payload,
                "strategy": manifest_payload["strategy"],
                "catalog_size": manifest_payload["catalog_size"],
                "disclosed_count": len(compiled.manifest.disclosed_names),
                "withheld_count": len(compiled.manifest.withheld_names),
                "excluded_count": len(compiled.manifest.excluded_names),
            },
            causation_id=events[-1].id,
        )

    @staticmethod
    def _capability_recall_sources(events: list[Event]) -> dict[str, str]:
        """Return the latest successful search hits and their durable evidence event."""

        for event in reversed(events):
            if event.type != "tool.completed":
                continue
            raw_result = event.payload.get("result")
            if not isinstance(raw_result, dict):
                continue
            if raw_result.get("name") != CAPABILITY_SEARCH_TOOL_NAME:
                continue
            if str(raw_result.get("status", "")).casefold() not in {
                "ok",
                "success",
                "succeeded",
            }:
                continue
            output = raw_result.get("output")
            if not isinstance(output, str):
                return {}
            try:
                result = CapabilitySearchResult.model_validate_json(output)
            except (ValidationError, ValueError, TypeError):
                return {}
            return {match.name: event.id for match in result.matches}
        return {}

    @staticmethod
    def _disclosed_names(capability_event: Event | None) -> tuple[str, ...] | None:
        if capability_event is None:
            # Compatibility for turns created before CapabilityCompiler existed: those models saw all tools.
            return None
        manifest = CapabilityManifest.model_validate(capability_event.payload["manifest"])
        return manifest.disclosed_names

    def _tool_schemas(self, names: Collection[str] | None = None) -> list[dict]:
        """把内部 ToolSpec 转成模型原生 tool-calling 可识别的函数 Schema。"""

        specs_by_name = {spec.name: spec for spec in self.tool_registry.specs()}
        specs = list(specs_by_name.values()) if names is None else [specs_by_name[name] for name in names]
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_schema or {"type": "object", "additionalProperties": True},
                },
            }
            for spec in specs
        ]

    def _recoverable_turn(self, events: list[Event]) -> tuple[str, list[Event]]:
        """寻找最近的半成品回合：已有模型响应，但还没有本轮结果事件。"""

        # 倒序检查 turn，优先恢复最新回合；完整回合不会被重复执行。
        turn_ids = sorted(
            {str(event.payload["turn_id"]) for event in events if "turn_id" in event.payload},
            key=self._turn_number,
            reverse=True,
        )
        for turn_id in turn_ids:
            turn_events = [event for event in events if str(event.payload.get("turn_id")) == turn_id]
            has_response = self._last_of_type(turn_events, "model.completed") is not None
            completed = any(event.type in _TURN_COMPLETED_EVENTS for event in turn_events)

            # 典型场景：model.completed 已 fsync，进程在 command 校验或执行前崩溃。
            if has_response and not completed:
                return turn_id, turn_events
        return "", []

    def _unresolved_operation(self, events: list[Event]) -> Event | None:
        """寻找 started 之后没有 terminal 事件的操作，它的外部效果处于不确定窗口。"""

        # terminal_ids 表示已经明确成功、失败、UNKNOWN 或完成对账的 operation。
        terminal_ids = {
            event.payload.get("operation_id")
            for event in events
            if event.type in {"operation.completed", "operation.failed", "operation.unknown", "operation.reconciled"}
        }

        # 从后往前找最近悬空的 started，恢复时一次只处理一个不确定操作。
        for event in reversed(events):
            if event.type == "operation.started" and event.payload.get("operation_id") not in terminal_ids:
                return event
        return None

    def _has_unresolved_unknown(self, events: list[Event]) -> bool:
        """判断是否仍有 UNKNOWN 未完成 reconciliation；有则禁止自动继续。"""

        unknown_ids = {event.payload.get("operation_id") for event in events if event.type == "operation.unknown"}
        resolved_ids = {event.payload.get("operation_id") for event in events if event.type == "operation.reconciled"}
        return bool(unknown_ids - resolved_ids)

    @staticmethod
    def _has_active_wait(events: list[Event]) -> bool:
        """判断最近等待条件是否尚未被同 correlation 的唤醒事件满足。"""

        waiting_index = next(
            (index for index in range(len(events) - 1, -1, -1) if events[index].type == "agent.waiting"),
            None,
        )
        if waiting_index is None:
            return False
        waiting = events[waiting_index]
        correlation_id = waiting.payload.get("correlation_id")
        later = events[waiting_index + 1 :]

        # 没有 correlation 的普通等待，只要后面还没有任何新事件就仍然有效。
        if correlation_id is None:
            return not later

        # A2A 回复、审批结果或超时事件都可以解除对应 correlation 的等待。
        return not any(
            event.payload.get("correlation_id") == correlation_id
            and event.type in {"a2a.peer.responded", "approval.decided", "runtime.wait.timed_out"}
            for event in later
        )

    @staticmethod
    def _is_terminal(events: list[Event]) -> bool:
        """判断整个 Assignment 是否已有最终事件，而不是判断某一轮是否结束。"""

        return any(event.type in _RUN_TERMINAL_EVENTS for event in events)

    def _next_turn_id(self, events: list[Event]) -> str:
        """根据历史最大编号生成下一回合 ID。"""

        numbers = [self._turn_number(str(event.payload["turn_id"])) for event in events if "turn_id" in event.payload]
        return f"turn_{max(numbers, default=0) + 1}"

    @staticmethod
    def _turn_number(turn_id: str) -> int:
        """从 turn_5 提取数字 5；异常格式降级为 0。"""

        try:
            return int(turn_id.rsplit("_", 1)[-1])
        except ValueError:
            return 0

    def _last_phase_event(self, turn_id: str) -> Event | None:
        """读取指定回合最近一次显式 phase，用于校验下一次状态迁移。"""

        for event in reversed(self._events()):
            if event.type == "loop.phase.changed" and str(event.payload.get("turn_id")) == turn_id:
                return event
        return None

    @staticmethod
    def _last_of_type(events: list[Event], event_type: str) -> Event | None:
        """从一组事件中倒序取得指定类型的最近事件。"""

        for event in reversed(events):
            if event.type == event_type:
                return event
        return None

    def _append(self, event_type: str, payload: dict, *, causation_id: str | None = None) -> Event:
        """沿用当前 run/task 身份追加持久事件，并保留因果来源。"""

        identity = self.event_log.last(task_id=self.task_id)
        if identity is None:
            raise RuntimeError("cannot append loop event without a seed event")
        return self.event_log.append(
            Event(
                run_id=identity.run_id,
                task_id=identity.task_id,
                type=event_type,
                source=self.agent_id,
                payload=payload,
                causation_id=causation_id,
            )
        )

    def _events(self) -> list[Event]:
        """读取当前 Assignment 的全部持久事实。"""

        return self.event_log.read_all(task_id=self.task_id)

    def _fault(self, marker: str) -> None:
        """教学用崩溃注入点；生产路径未配置 injector 时什么也不做。"""

        if self.fault_injector is not None:
            self.fault_injector(marker)
