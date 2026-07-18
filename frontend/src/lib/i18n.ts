const AGENT_LABELS: Record<string, string> = {
  coordinator: "总控 / Coordinator",
  scout: "侦察 / Scout",
  "scout-backup": "侦察备用 / Scout Backup",
  builder: "构建 / Builder",
  reviewer: "审查 / Reviewer",
  generalist: "通用执行 / Generalist",
  "dream.worker": "记忆蒸馏 / Dream Worker",
  "context.evolver": "上下文进化 / Context Evolver",
};

const STATUS_LABELS: Record<string, string> = {
  idle: "空闲 / Idle",
  busy: "执行中 / Busy",
  waiting: "等待中 / Waiting",
  degraded: "降级 / Degraded",
  offline: "离线 / Offline",
  manual: "手动模式 / Manual",
  queued: "已排队 / Queued",
  running: "运行中 / Running",
  succeeded: "已完成 / Succeeded",
  failed: "失败 / Failed",
  completed: "已完成 / Completed",
  active: "已生效 / Active",
  released: "已释放 / Released",
  expired: "已超时 / Expired",
  stale: "已隔离 / Stale",
  candidate: "候选 / Candidate",
  pending: "待处理 / Pending",
  approved: "已批准 / Approved",
  rejected: "已拒绝 / Rejected",
  passed: "已通过 / Passed",
  green: "绿色区 / Green",
  yellow: "黄色区 / Yellow",
  red: "红色区 / Red",
  review: "待审核 / Review",
};

const BOUNDARY_LABELS: Record<string, string> = {
  proposal: "提议 / Proposal",
  control: "控制 / Control",
  fact: "事实 / Fact",
  runtime: "运行时 / Runtime",
};

const STREAM_LABELS: Record<string, string> = {
  connecting: "连接中 / Connecting",
  live: "实时 / Live",
  reconnecting: "重连中 / Reconnecting",
  offline: "离线 / Offline",
};

const CONTENT_LABELS: Record<string, string> = {
  "Collect verifiable evidence for the incoming task.": "为当前任务收集可验证证据。 / Collect verifiable evidence for the incoming task.",
  "Compose a bounded execution artifact from the collected evidence.": "根据已收集证据生成受控的执行制品。 / Compose a bounded execution artifact from the collected evidence.",
  "Independently review the artifact and its evidence.": "独立审查执行制品及其证据。 / Independently review the artifact and its evidence.",
  "Before composing an artifact, use one bounded peer check when evidence freshness matters.": "生成制品前，如证据时效性重要，则进行一次受控的同伴对账。 / Before composing an artifact, use one bounded peer check when evidence freshness matters.",
  "Retain slightly more structured facts after successful offloading.": "成功卸载大内容后，适当保留更多结构化事实。 / Retain slightly more structured facts after successful offloading.",
};

const CAPABILITY_LABELS: Record<string, string> = {
  "orchestration.plan": "编排计划 / orchestration.plan",
  "completion.gate": "完成门禁 / completion.gate",
  "evidence.collect": "证据收集 / evidence.collect",
  "peer.respond": "同伴响应 / peer.respond",
  "artifact.compose": "制品生成 / artifact.compose",
  "peer.request": "同伴请求 / peer.request",
  "artifact.review": "制品审查 / artifact.review",
  "evidence.verify": "证据验证 / evidence.verify",
  "repo.inspect": "仓库检查 / repo.inspect",
  "repo.edit": "受控修改 / repo.edit",
  "test.verify": "测试验证 / test.verify",
  "research.browse": "浏览器研究 / research.browse",
  "research.cite": "证据引用 / research.cite",
};

const TOOL_LABELS: Record<string, string> = {
  "skill.activate": "激活技能 / skill.activate",
  "capability.search": "检索能力 / capability.search",
  "repo.read": "读取仓库文件 / repo.read",
  "repo.write": "写入仓库文件 / repo.write",
  "repo.replace": "替换仓库内容 / repo.replace",
  "test.run": "运行真实测试 / test.run",
  "repo.diff": "检查仓库差异 / repo.diff",
  "research.sources.list": "列出证据源 / research.sources.list",
  "research.source.open": "浏览器打开证据源 / research.source.open",
  "research.report.write": "写入研究报告 / research.report.write",
  "research.report.validate": "校验研究报告 / research.report.validate",
};

export function agentLabel(value: string): string {
  return AGENT_LABELS[value] ?? value;
}

export function statusLabel(value: string): string {
  return STATUS_LABELS[value] ?? value;
}

export function boundaryLabel(value: string): string {
  return BOUNDARY_LABELS[value] ?? value;
}

export function streamLabel(value: string): string {
  return STREAM_LABELS[value] ?? value;
}

export function contentLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return CONTENT_LABELS[value] ?? value;
}

export function capabilityLabel(value: string | null | undefined): string {
  if (!value) return "就绪 / Ready";
  return CAPABILITY_LABELS[value] ?? value;
}

export function toolLabel(value: string | null | undefined): string {
  if (!value) return "未知工具 / Unknown tool";
  return TOOL_LABELS[value] ?? value;
}
