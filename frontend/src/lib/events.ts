import type { components } from "../api/schema";

export type EventRecord = components["schemas"]["EventRecord"];

export type EventTone = "neutral" | "info" | "success" | "warning" | "danger";
export type TrustBoundary = "proposal" | "control" | "fact" | "runtime";

export interface EventMeta {
  label: string;
  family: "loop" | "a2a" | "context" | "runtime" | "memory" | "evolution" | "gate";
  tone: EventTone;
  boundary: TrustBoundary;
  important: boolean;
}

const EXACT: Record<string, Partial<EventMeta> & Pick<EventMeta, "label">> = {
  "run.created": { label: "运行创建 / Run created", family: "runtime", boundary: "fact" },
  "event.external.received": { label: "外部事件进入 / Event received", family: "runtime", boundary: "fact" },
  "orchestration.plan.patched": { label: "计划修订 / Plan patched", family: "a2a", boundary: "fact" },
  "assignment.created": { label: "任务委派 / Assignment delegated", family: "a2a", boundary: "fact" },
  "assignment.running": { label: "任务执行中 / Assignment running", family: "a2a", boundary: "fact" },
  "assignment.waiting": { label: "任务等待中 / Assignment waiting", family: "a2a", boundary: "fact", tone: "warning" },
  "assignment.succeeded": { label: "任务已完成 / Assignment succeeded", family: "a2a", boundary: "fact", tone: "success" },
  "assignment.completed": { label: "任务已完成 / Assignment completed", family: "a2a", boundary: "fact", tone: "success" },
  "assignment.failed": { label: "任务失败 / Assignment failed", family: "a2a", boundary: "fact", tone: "danger" },
  "agent.result.submitted": { label: "Agent 结果提交 / Agent result submitted", family: "a2a", boundary: "fact" },
  "model.requested": { label: "模型调用 / Model requested", family: "loop", boundary: "runtime" },
  "model.completed": { label: "模型建议 / Model response", family: "loop", boundary: "proposal" },
  "model.response.reused": { label: "响应复用 / Response reused", family: "loop", boundary: "runtime", tone: "warning" },
  "model.validation_failed": { label: "模型建议不合法 / Model validation failed", family: "gate", boundary: "control", tone: "danger" },
  "agent.command.validated": { label: "命令校验通过 / Command validated", family: "gate", boundary: "control", tone: "success" },
  "agent.command.reused": { label: "复用已校验命令 / Command reused", family: "loop", boundary: "runtime", tone: "warning" },
  "loop.phase.changed": { label: "循环阶段迁移 / Loop phase changed", family: "loop", boundary: "runtime" },
  "candidate.submitted": { label: "候选提交 / Candidate submitted", family: "loop", boundary: "proposal" },
  "candidate.recovered": { label: "候选恢复 / Candidate recovered", family: "loop", boundary: "runtime", tone: "warning" },
  "candidate.accepted": { label: "命令获准 / Candidate accepted", family: "gate", boundary: "control", tone: "success" },
  "candidate.rejected": { label: "命令拒绝 / Candidate rejected", family: "gate", boundary: "control", tone: "danger" },
  "operation.started": { label: "副作用开始 / Operation started", family: "loop", boundary: "fact", tone: "warning" },
  "operation.completed": { label: "副作用完成 / Operation completed", family: "loop", boundary: "fact", tone: "success" },
  "operation.failed": { label: "副作用失败 / Operation failed", family: "loop", boundary: "fact", tone: "danger" },
  "tool.requested": { label: "工具调用请求 / Tool requested", family: "loop", boundary: "fact" },
  "tool.completed": { label: "工具事实 / Tool observation", family: "loop", boundary: "fact", tone: "success" },
  "tool.failed": { label: "工具执行失败 / Tool failed", family: "loop", boundary: "fact", tone: "danger" },
  "evidence.recorded": { label: "证据入账 / Evidence recorded", family: "gate", boundary: "fact", tone: "success" },
  "artifact.recorded": { label: "制品入账 / Artifact recorded", family: "gate", boundary: "fact", tone: "success" },
  "review.recorded": { label: "审查入账 / Review recorded", family: "gate", boundary: "fact", tone: "success" },
  "a2a.policy.allowed": { label: "A2A 策略放行 / Peer allowed", family: "a2a", boundary: "control", tone: "success" },
  "a2a.policy.denied": { label: "A2A 策略拒绝 / Peer denied", family: "a2a", boundary: "control", tone: "danger" },
  "a2a.peer.requested": { label: "发起一跳对账 / Peer requested", family: "a2a", boundary: "fact", tone: "info" },
  "a2a.peer.responded": { label: "对账响应 / Peer responded", family: "a2a", boundary: "fact", tone: "success" },
  "completion.gate.passed": { label: "准出通过 / Completion passed", family: "gate", boundary: "control", tone: "success" },
  "completion.gate.failed": { label: "准出阻断 / Completion blocked", family: "gate", boundary: "control", tone: "danger" },
  "completion.requested": { label: "申请完成 / Completion requested", family: "gate", boundary: "fact" },
  "agent.nudged": { label: "定向纠偏 / Nudge", family: "gate", boundary: "control", tone: "warning" },
  "agent.continued": { label: "继续下一轮 / Continue", family: "loop", boundary: "fact" },
  "agent.submitted": { label: "Agent 正式交付 / Agent submitted", family: "gate", boundary: "fact", tone: "success" },
  "agent.stopped": { label: "Agent 停止 / Agent stopped", family: "gate", boundary: "fact", tone: "warning" },
  "agent.failed": { label: "Agent 失败 / Agent failed", family: "gate", boundary: "fact", tone: "danger" },
  "run.succeeded": { label: "运行完成 / Run succeeded", family: "runtime", boundary: "fact", tone: "success" },
  "run.failed": { label: "运行失败 / Run failed", family: "runtime", boundary: "fact", tone: "danger" },
  "run.paused": { label: "运行暂停 / Run paused", family: "runtime", boundary: "fact", tone: "warning" },
  "runtime.agent.crashed": { label: "执行中断 / Worker crashed", family: "runtime", boundary: "runtime", tone: "danger" },
  "runtime.agent.busy": { label: "Agent 执行中 / Agent busy", family: "runtime", boundary: "runtime" },
  "runtime.agent.idle": { label: "Agent 空闲 / Agent idle", family: "runtime", boundary: "runtime" },
  "runtime.agent.step.completed": { label: "Agent 单步完成 / Agent step completed", family: "runtime", boundary: "runtime", tone: "success" },
  "runtime.turn.ready": { label: "下一轮已排队 / Next turn ready", family: "runtime", boundary: "runtime" },
  "mailbox.delivery.sent": { label: "邮箱消息投递 / Mailbox delivery sent", family: "runtime", boundary: "runtime" },
  "mailbox.delivery.acked": { label: "邮箱消息确认 / Mailbox delivery acknowledged", family: "runtime", boundary: "runtime", tone: "success" },
  "context.compiled": { label: "上下文编译 / Context compiled", family: "context", boundary: "runtime", tone: "info" },
  "context.manifest.compiled": { label: "上下文清单生成 / Context manifest compiled", family: "context", boundary: "runtime", tone: "info" },
  "capability.manifest.compiled": { label: "能力披露清单 / Capability manifest", family: "context", boundary: "control", tone: "info" },
  "skill.catalog.compiled": { label: "技能目录编译 / Skill catalog", family: "context", boundary: "control", tone: "info" },
  "context.item.offloaded": { label: "大结果卸载 / Result offloaded", family: "context", boundary: "control", tone: "warning" },
  "learning.signal.detected": { label: "学习信号 / Learning signal", family: "memory", boundary: "control", tone: "info" },
  "dream.job.queued": { label: "Dream 排队 / Dream queued", family: "memory", boundary: "runtime" },
  "dream.job.started": { label: "Dream 蒸馏 / Dream started", family: "memory", boundary: "runtime", tone: "info" },
  "dream.evidence.frozen": { label: "证据冻结 / Evidence frozen", family: "memory", boundary: "fact" },
  "memory.candidate.proposed": { label: "记忆候选 / Memory candidate", family: "memory", boundary: "proposal" },
  "memory.admission.decided": { label: "记忆准入 / Memory admission", family: "memory", boundary: "control" },
  "memory.activated": { label: "记忆激活 / Memory active", family: "memory", boundary: "fact", tone: "success" },
  "dream.job.completed": { label: "Dream 完成 / Dream completed", family: "memory", boundary: "fact", tone: "success" },
  "evolution.candidate.proposed": { label: "进化候选 / Evolution candidate", family: "evolution", boundary: "proposal" },
  "evolution.signal.ready": { label: "进化信号就绪 / Evolution signal ready", family: "evolution", boundary: "fact" },
  "evolution.offline.passed": { label: "离线评测通过 / Replay passed", family: "evolution", boundary: "control", tone: "success" },
};

const NOISE = new Set([
  "runtime.agent.busy",
  "runtime.agent.idle",
  "runtime.agent.step.completed",
  "mailbox.delivery.sent",
  "mailbox.delivery.acked",
  "assignment.running",
  "assignment.waiting",
  "assignment.succeeded",
  "operation.completed",
  "tool.requested",
]);

export function eventMeta(type: string): EventMeta {
  const known = EXACT[type];
  const family: EventMeta["family"] = type.startsWith("a2a.") || type.startsWith("assignment.")
    ? "a2a"
    : type.startsWith("context.")
      ? "context"
      : type.startsWith("memory.") || type.startsWith("dream.") || type.startsWith("learning.")
        ? "memory"
        : type.startsWith("evolution.")
          ? "evolution"
          : type.startsWith("completion.") || type.startsWith("candidate.")
            ? "gate"
            : type.startsWith("runtime.") || type.startsWith("run.") || type.startsWith("mailbox.")
              ? "runtime"
              : "loop";
  const boundary: TrustBoundary = type.startsWith("model.") || type.endsWith("candidate.proposed")
    ? "proposal"
    : type.includes("policy") || type.includes("gate") || type.startsWith("candidate.")
      ? "control"
      : type.startsWith("runtime.") || type.startsWith("mailbox.") || type.startsWith("context.")
        ? "runtime"
        : "fact";
  return {
    label: known?.label ?? type,
    family: known?.family ?? family,
    tone: known?.tone ?? "neutral",
    boundary: known?.boundary ?? boundary,
    important: known?.important ?? !NOISE.has(type),
  };
}

export function selectVisibleEvents(records: EventRecord[], showAll: boolean): EventRecord[] {
  return showAll ? records : records.filter((record) => eventMeta(record.event.type).important);
}

export function shortId(value: string | null | undefined, length = 8): string {
  if (!value) return "—";
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}
