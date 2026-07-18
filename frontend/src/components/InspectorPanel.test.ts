import { describe, expect, it } from "vitest";

import {
  claimKindLabel,
  formatEstimatedModelCost,
  modelBudgetPercent,
  stageLabel,
} from "./InspectorPanel";

describe("collaboration observability labels", () => {
  it("labels risk stages and AgentRun claims bilingually", () => {
    expect(stageLabel("risk")).toBe("风险研判 / Risk");
    expect(claimKindLabel("agent-run:run_123")).toBe("AgentRun 声明 / AgentRun claim");
    expect(claimKindLabel("worker-slot:builder:0")).toBe("容量槽声明 / Worker slot claim");
  });

  it("formats model budget progress and estimated cost deterministically", () => {
    expect(modelBudgetPercent(25, 100)).toBe(25);
    expect(modelBudgetPercent(120, 100)).toBe(100);
    expect(modelBudgetPercent(0, 0)).toBe(0);
    expect(formatEstimatedModelCost(2526)).toBe("$0.002526");
  });
});
