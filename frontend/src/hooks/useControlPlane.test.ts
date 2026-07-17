import { describe, expect, it } from "vitest";

import { resolveInitialRunId } from "./useControlPlane";

describe("control plane run selection", () => {
  it("prefers an explicit URL run over stale local storage", () => {
    expect(resolveInitialRunId("?run=run-from-url", "run-from-storage")).toBe("run-from-url");
  });

  it("falls back to local storage only when the URL has no usable run", () => {
    expect(resolveInitialRunId("", "run-from-storage")).toBe("run-from-storage");
    expect(resolveInitialRunId("?run=%20%20", "run-from-storage")).toBe("run-from-storage");
    expect(resolveInitialRunId("", null)).toBeUndefined();
  });
});
