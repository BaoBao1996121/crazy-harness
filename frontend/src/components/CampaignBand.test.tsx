import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { EvalCampaignReport } from "../api/client";
import { CampaignBand } from "./CampaignBand";

describe("CampaignBand", () => {
  it("exposes durable cancellation while a Campaign is running", () => {
    const report = {
      campaign_id: "campaign_test",
      status: "running",
      contract: {
        title: "三轮公平评测",
        evidence_tier: "scripted_mechanism",
      },
      planned_trial_count: 3,
      completed_trial_count: 1,
      trials: [],
    } as unknown as EvalCampaignReport;

    const html = renderToStaticMarkup(
      <CampaignBand
        campaignId="campaign_test"
        report={report}
        loading={false}
        busy={false}
        onOpenTrial={vi.fn()}
        onCancel={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(html).toContain("取消实验 / Cancel");
    expect(html).toContain("三轮公平评测");
  });

  it("shows the backend recommendation reason as Chinese-first bilingual text", () => {
    const report = {
      campaign_id: "campaign_test",
      status: "completed",
      contract: {
        title: "三轮公平评测",
        evidence_tier: "scripted_mechanism",
      },
      planned_trial_count: 3,
      completed_trial_count: 3,
      trials: [],
      recommendation: {
        outcome: "keep_single",
        reason: "Team regressed under deterministic campaign evidence",
        failed_thresholds: ["scripted_duration_regression"],
      },
    } as unknown as EvalCampaignReport;

    const html = renderToStaticMarkup(
      <CampaignBand
        campaignId="campaign_test"
        report={report}
        loading={false}
        busy={false}
        onOpenTrial={vi.fn()}
        onCancel={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(html).toContain(
      "确定性 Campaign 证据显示 Team 发生退化 / Team regressed under deterministic campaign evidence",
    );
  });
});
