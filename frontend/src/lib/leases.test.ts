import { describe, expect, it } from "vitest";

import { leaseSummary, type LeaseLike } from "./leases";

const lease = (status: string, expiresAt = "2026-07-17T00:00:30Z"): LeaseLike => ({
  status,
  expires_at: expiresAt,
});

describe("lease presentation", () => {
  it("shows a deterministic remaining duration for an active lease", () => {
    expect(
      leaseSummary(
        lease("active"),
        new Date("2026-07-17T00:00:20Z"),
      ),
    ).toBe("租约有效 10s / Lease active");
  });

  it("never renders a negative duration when a refresh arrives late", () => {
    expect(
      leaseSummary(
        lease("active"),
        new Date("2026-07-17T00:00:31Z"),
      ),
    ).toBe("租约有效 0s / Lease active");
  });

  it("uses explicit bilingual terminal labels", () => {
    expect(leaseSummary(lease("released"))).toBe("租约已释放 / Lease released");
    expect(leaseSummary(lease("expired"))).toBe("租约已超时 / Lease expired");
  });

  it("returns no copy when an assignment has no lease", () => {
    expect(leaseSummary(undefined)).toBeNull();
  });
});
