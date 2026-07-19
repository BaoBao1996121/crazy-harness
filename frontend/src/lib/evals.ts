import type { PairedEvalReport } from "../api/client";

export interface FairnessCheck {
  id: "input" | "model" | "budget" | "workspace";
  label: string;
  passed: boolean;
}

export interface RecommendationPresentation {
  outcome: "pending" | "insufficient_live_evidence" | "recommend_team" | "keep_single";
  title: string;
  detail: string;
  tone: "neutral" | "caution" | "positive";
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, entry]) => `${JSON.stringify(key)}:${stableJson(entry)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function draftModelComparisonLabel(
  modelMode: "scripted" | "deepseek",
): string {
  return modelMode === "scripted"
    ? "不同确定性脚本 / Different deterministic scripts"
    : "同模型 / Same model";
}

export function fairnessChecks(report: PairedEvalReport): FairnessCheck[] {
  const { single, team } = report.contract;
  const modelCheck = report.contract.evidence_tier === "deterministic"
    ? {
        id: "model" as const,
        label: "不同确定性脚本 / Different deterministic scripts",
        passed: true,
      }
    : {
        id: "model" as const,
        label: "同模型且有调用证明 / Same model + attestation",
        passed: stableJson(single.model_profile) === stableJson(team.model_profile)
          && !(report.invalid_reasons ?? []).some((reason) =>
            reason.includes("model call attestation")
          ),
      };
  return [
    { id: "input", label: "同输入 / Same input", passed: single.input_hash === team.input_hash },
    modelCheck,
    { id: "budget", label: "同总预算 / Same total budget", passed: stableJson(single.model_budget) === stableJson(team.model_budget) },
    { id: "workspace", label: "隔离工作区 / Isolated workspace", passed: single.workspace !== team.workspace },
  ];
}

export function recommendationPresentation(report: PairedEvalReport): RecommendationPresentation {
  if (report.evidence_valid === false) {
    return {
      outcome: "insufficient_live_evidence",
      title: "评测证据无效 / Invalid eval evidence",
      detail: `原因 / Reason: ${(report.invalid_reasons ?? []).join("；")}`,
      tone: "caution",
    };
  }
  if (report.contract.evidence_tier === "deterministic") {
    return {
      outcome: "insufficient_live_evidence",
      title: "真实证据不足 / Insufficient live evidence",
      detail: "脚本模型只验证公平机制与可重放性，不能证明 Team 在真实任务上更优。",
      tone: "caution",
    };
  }
  const outcome = report.recommendation?.outcome;
  if (!outcome || outcome === "insufficient_live_evidence") {
    return {
      outcome: outcome ?? "pending",
      title: outcome ? "真实证据不足 / Insufficient live evidence" : "正在收集配对证据 / Collecting paired evidence",
      detail: outcome
        ? "真实配对试验尚未达到推荐门槛，暂不改变编排策略。"
        : "两臂完成后才会依据机器评分、可靠性、成本与时延给出建议。",
      tone: outcome ? "caution" : "neutral",
    };
  }
  if (outcome === "recommend_team") {
    return {
      outcome,
      title: "建议 Team / Recommend Team",
      detail: "Team 已通过当前的质量、可靠性、成本与时延推荐门槛。",
      tone: "positive",
    };
  }
  return {
    outcome,
    title: "保留 Single / Keep Single",
    detail: "Team 未通过全部推荐门槛，当前继续使用 Single。",
    tone: "neutral",
  };
}
