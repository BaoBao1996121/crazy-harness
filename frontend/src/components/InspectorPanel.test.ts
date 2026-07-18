import { describe, expect, it } from "vitest";

import { claimKindLabel, stageLabel } from "./InspectorPanel";

describe("collaboration observability labels", () => {
  it("labels risk stages and AgentRun claims bilingually", () => {
    expect(stageLabel("risk")).toBe("风险研判 / Risk");
    expect(claimKindLabel("agent-run:run_123")).toBe("AgentRun 声明 / AgentRun claim");
    expect(claimKindLabel("worker-slot:builder:0")).toBe("容量槽声明 / Worker slot claim");
  });
});
