import { describe, expect, it } from "vitest";

import { resolveInitialRunId, shouldDiscoverLatestRun } from "./useControlPlane";

describe("control plane run selection", () => {
  it("prefers an explicit URL run over stale local storage", () => {
    expect(resolveInitialRunId("?run=run-from-url", "run-from-storage")).toBe("run-from-url");
  });

  it("falls back to local storage only when the URL has no usable run", () => {
    expect(resolveInitialRunId("", "run-from-storage")).toBe("run-from-storage");
    expect(resolveInitialRunId("?run=%20%20", "run-from-storage")).toBe("run-from-storage");
    expect(resolveInitialRunId("", null)).toBeUndefined();
  });

  it("does not discover an unrelated latest run behind an explicit Campaign or Eval link", () => {
    expect(shouldDiscoverLatestRun("", undefined)).toBe(true);
    expect(shouldDiscoverLatestRun("?campaign=campaign_shared", undefined)).toBe(false);
    expect(shouldDiscoverLatestRun("?eval=eval_shared", undefined)).toBe(false);
    expect(shouldDiscoverLatestRun("?run=run_shared", "run_shared")).toBe(false);
  });

  it("reserves startup for an unconfirmed paid Campaign before restoring or discovering a Run", () => {
    expect(resolveInitialRunId("?run=run_shared", "run_stored", true))
      .toBeUndefined();
    expect(resolveInitialRunId("", "run_stored", true)).toBeUndefined();
    expect(shouldDiscoverLatestRun("", undefined, true)).toBe(false);
  });
});
