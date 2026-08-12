import { describe, expect, it } from "vitest";

import { replaceIdentitySelection, resolveIdentityParam } from "./urlState";

describe("shareable URL identity", () => {
  it("does not mix missing identities from browser memory into an explicit share link", () => {
    expect(resolveIdentityParam("?campaign=campaign_shared", "campaign", "campaign_old"))
      .toBe("campaign_shared");
    expect(resolveIdentityParam("?campaign=campaign_shared", "eval", "eval_old"))
      .toBeUndefined();
    expect(resolveIdentityParam("?eval=eval_shared", "run", "run_old"))
      .toBeUndefined();
    expect(resolveIdentityParam("?run=run_shared", "campaign", "campaign_old"))
      .toBeUndefined();
    expect(resolveIdentityParam("?loop=loop_shared", "run", "run_old"))
      .toBeUndefined();
    expect(resolveIdentityParam("?loop=loop_shared", "loop", "loop_old"))
      .toBe("loop_shared");
    expect(resolveIdentityParam("", "run", "run_old")).toBe("run_old");
  });

  it("atomically replaces stale identities when a pending Campaign is recovered", () => {
    expect(replaceIdentitySelection(
      "?eval=eval_old&run=run_old&trial=2&theme=calm",
      { campaign: "campaign_recovered" },
    )).toBe("?theme=calm&campaign=campaign_recovered");
  });
});
