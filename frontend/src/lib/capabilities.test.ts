import { describe, expect, it } from "vitest";

import { capabilityIdentityEntries, capabilityRecallEntries } from "./capabilities";

describe("capabilityRecallEntries", () => {
  it("keeps only valid name-to-event references in stable order", () => {
    expect(
      capabilityRecallEntries({
        "test.run": "event-z",
        "deploy.audit": "event-a",
        ignored: 42,
      }),
    ).toEqual([
      { name: "deploy.audit", sourceEventId: "event-a" },
      { name: "test.run", sourceEventId: "event-z" },
    ]);
  });

  it("returns an empty list for absent or malformed recall sources", () => {
    expect(capabilityRecallEntries(undefined)).toEqual([]);
    expect(capabilityRecallEntries(["event-a"])).toEqual([]);
  });
});
describe("capabilityIdentityEntries", () => {
  it("joins disclosed names with kind and provider while preserving old manifests", () => {
    expect(
      capabilityIdentityEntries(
        ["repo.read", "mcp.docs.lookup"],
        { "mcp.docs.lookup": "mcp" },
        { "mcp.docs.lookup": "mcp:docs" },
      ),
    ).toEqual([
      { name: "repo.read", kind: "function", provider: "local" },
      { name: "mcp.docs.lookup", kind: "mcp", provider: "mcp:docs" },
    ]);
  });
});
