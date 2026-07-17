export type SingleTaskPackId = "repo-maintainer" | "evidence-research";
export type ResidentTaskPackId = "resident-demo" | SingleTaskPackId;

export interface TaskPackPresentation {
  id: SingleTaskPackId;
  label: string;
  detail: string;
  title: string;
  brief: string;
}

const SINGLE_TASK_PACKS: readonly TaskPackPresentation[] = [
  {
    id: "repo-maintainer",
    label: "仓库维护 / Repository maintenance",
    detail: "读代码、受控修改、真实测试 / Inspect, edit, verify",
    title: "修复一次可回收仓库 / Repair a disposable repository",
    brief:
      "定位实现缺陷，只修改允许的实现文件，运行真实测试并用差异证据证明结果。 / Find the implementation defect, edit only allowlisted files, run real tests, and prove the result with a diff.",
  },
  {
    id: "evidence-research",
    label: "证据研究 / Evidence research",
    detail: "浏览器取证、规范引用、报告门禁 / Browse, cite, validate",
    title: "证据研究：选择发布策略 / Evidence research: choose a deployment strategy",
    brief:
      "从至少两个独立来源收集浏览器证据，生成带规范引用的建议报告，并通过确定性引用校验。 / Collect browser evidence from at least two independent sources, write a canonically cited recommendation, and pass deterministic citation validation.",
  },
] as const;

export function singleTaskPackIds(): SingleTaskPackId[] {
  return SINGLE_TASK_PACKS.map((item) => item.id);
}

export function singleTaskPackOptions(): readonly TaskPackPresentation[] {
  return SINGLE_TASK_PACKS;
}

export function singleTaskPackDefaults(id: SingleTaskPackId): TaskPackPresentation {
  const selected = SINGLE_TASK_PACKS.find((item) => item.id === id);
  if (!selected) throw new Error(`unknown single-agent TaskPack: ${id}`);
  return selected;
}

export function taskPackForExecution(
  mode: "team" | "single",
  selected: SingleTaskPackId,
): ResidentTaskPackId {
  return mode === "team" ? "resident-demo" : selected;
}
