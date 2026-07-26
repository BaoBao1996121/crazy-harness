import { describe, expect, it } from "vitest";

import {
  campaignRecommendationReason,
  campaignProgress,
  formatCampaignMetric,
  trialStatusLabel,
} from "./campaigns";

describe("campaign presentation", () => {
  it("keeps progress bounded and explicit", () => {
    expect(campaignProgress(2, 5)).toEqual({ completed: 2, planned: 5, percent: 40 });
    expect(campaignProgress(7, 5).percent).toBe(100);
  });

  it("renders paired deltas and ratios with their interval", () => {
    expect(formatCampaignMetric({
      point_ppm: 10_000,
      lower_bound_ppm: -5_000,
      upper_bound_ppm: 20_000,
    }, "delta")).toBe("+1.0% · 区间 -0.5% ～ +2.0%")
    expect(formatCampaignMetric({
      point_ppm: 1_250_000,
      lower_bound_ppm: null,
      upper_bound_ppm: null,
    }, "ratio")).toBe("1.25× · 样本不足，暂无区间")
  });

  it("uses Chinese-first durable trial states", () => {
    expect(trialStatusLabel("started")).toBe("已占位 / Started");
    expect(trialStatusLabel("creation_failed")).toBe("创建失败 / Creation failed");
  });

  it("translates every current Campaign recommendation reason instead of leaking raw English", () => {
    expect(campaignRecommendationReason(
      "Team regressed under deterministic campaign evidence",
    )).toBe(
      "确定性 Campaign 证据显示 Team 发生退化 / Team regressed under deterministic campaign evidence",
    );
    expect(campaignRecommendationReason(
      "Team passed every point and family-wise confidence gate",
    )).toBe(
      "Team 通过全部点估计与族级置信门禁 / Team passed every point and family-wise confidence gate",
    );
    expect(campaignRecommendationReason("future backend reason")).toBe(
      "收到尚未登记的推荐原因，请查看审计详情 / Unmapped recommendation reason; inspect audit details",
    );
  });
});
