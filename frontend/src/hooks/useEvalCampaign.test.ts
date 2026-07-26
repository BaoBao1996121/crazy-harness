import { describe, expect, it } from "vitest";

import type { EvalCampaignReport } from "../api/client";
import {
  nextCampaignPollDelay,
  resolveInitialCampaignId,
  resolveStartupCampaignId,
  shouldRecoverPendingCampaign,
} from "./useEvalCampaign";

describe("eval campaign state", () => {
  it("prefers the URL identity over browser memory", () => {
    expect(resolveInitialCampaignId("?campaign=campaign_url", "campaign_stored"))
      .toBe("campaign_url");
  });

  it("gives a durable pending request priority on an ordinary refresh", () => {
    expect(resolveStartupCampaignId("", "campaign_old", true)).toBeUndefined();
    expect(resolveStartupCampaignId("", "campaign_old", false)).toBe("campaign_old");
    expect(resolveStartupCampaignId("?campaign=campaign_shared", "campaign_old", true))
      .toBe("campaign_shared");
    expect(resolveStartupCampaignId("?eval=eval_shared", "campaign_old", true))
      .toBeUndefined();
  });

  it("never lets pending recovery take over an explicit shared identity", () => {
    expect(shouldRecoverPendingCampaign("", undefined, true)).toBe(true);
    expect(shouldRecoverPendingCampaign("?campaign=campaign_shared", "campaign_shared", true))
      .toBe(false);
    expect(shouldRecoverPendingCampaign("?eval=eval_shared", undefined, true))
      .toBe(false);
  });

  it("stops polling after the durable terminal report", () => {
    expect(nextCampaignPollDelay({ status: "running" } as EvalCampaignReport)).toBe(900);
    expect(nextCampaignPollDelay({ status: "completed" } as EvalCampaignReport))
      .toBeUndefined();
    expect(nextCampaignPollDelay({ status: "cancelled" } as EvalCampaignReport))
      .toBeUndefined();
  });
});
