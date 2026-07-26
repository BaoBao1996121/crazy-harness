interface MetricBounds {
  point_ppm?: number | null;
  lower_bound_ppm?: number | null;
  upper_bound_ppm?: number | null;
}

const RECOMMENDATION_REASONS: Record<string, string> = {
  "campaign evidence is incomplete or invalid":
    "Campaign 证据不完整或无效 / Campaign evidence is incomplete or invalid",
  "Team introduced a hard reliability regression":
    "Team 引入了严重可靠性退化 / Team introduced a hard reliability regression",
  "Team regressed under deterministic campaign evidence":
    "确定性 Campaign 证据显示 Team 发生退化 / Team regressed under deterministic campaign evidence",
  "deterministic campaigns can veto regressions but cannot promote Team":
    "确定性 Campaign 可否决退化，但不能据此晋升 Team / Deterministic campaigns can veto regressions but cannot promote Team",
  "Team failed at least one campaign point-estimate gate":
    "Team 至少未通过一项点估计门禁 / Team failed at least one campaign point-estimate gate",
  "valid live trial count is below the initial promotion minimum":
    "有效真实 Trial 数低于初始晋升门槛 / Valid live trial count is below the initial promotion minimum",
  "point estimates pass, but family-wise promotion bounds do not":
    "点估计通过，但族级置信边界未通过 / Point estimates pass, but family-wise promotion bounds do not",
  "Team passed every point and family-wise confidence gate":
    "Team 通过全部点估计与族级置信门禁 / Team passed every point and family-wise confidence gate",
};

export function campaignRecommendationReason(reason: string | undefined): string | undefined {
  if (!reason) return undefined;
  return RECOMMENDATION_REASONS[reason]
    ?? "收到尚未登记的推荐原因，请查看审计详情 / Unmapped recommendation reason; inspect audit details";
}

export function campaignProgress(completed: number, planned: number) {
  const safePlanned = Math.max(1, planned);
  const safeCompleted = Math.max(0, Math.min(completed, safePlanned));
  return {
    completed: safeCompleted,
    planned: safePlanned,
    percent: Math.round((safeCompleted / safePlanned) * 100),
  };
}

function signedPercent(value: number): string {
  const percent = value / 10_000;
  return `${percent > 0 ? "+" : ""}${percent.toFixed(1)}%`;
}

function ratio(value: number): string {
  return `${(value / 1_000_000).toFixed(2)}×`;
}

export function formatCampaignMetric(
  metric: MetricBounds | null | undefined,
  kind: "delta" | "ratio",
): string {
  if (!metric || metric.point_ppm == null) return "暂无可比较数据 / Unavailable";
  const format = kind === "delta" ? signedPercent : ratio;
  const point = format(metric.point_ppm);
  if (metric.lower_bound_ppm == null || metric.upper_bound_ppm == null) {
    return `${point} · 样本不足，暂无区间`;
  }
  return `${point} · 区间 ${format(metric.lower_bound_ppm)} ～ ${format(metric.upper_bound_ppm)}`;
}

const TRIAL_STATUS: Record<string, string> = {
  planned: "待开始 / Planned",
  started: "已占位 / Started",
  linked: "已关联 / Linked",
  released: "执行中 / Running",
  observed: "已观察 / Observed",
  creation_failed: "创建失败 / Creation failed",
};

export function trialStatusLabel(status: string): string {
  return TRIAL_STATUS[status] ?? status;
}
