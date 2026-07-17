import { describe, expect, it } from "vitest";

import {
  singleTaskPackDefaults,
  singleTaskPackIds,
  taskPackForExecution,
} from "./taskpacks";

describe("TaskPack selection", () => {
  it("offers both real single-agent business shells", () => {
    expect(singleTaskPackIds()).toEqual(["repo-maintainer", "evidence-research"]);
  });

  it("provides bilingual defaults for evidence research", () => {
    const defaults = singleTaskPackDefaults("evidence-research");

    expect(defaults.title).toContain("证据研究");
    expect(defaults.title).toContain("Evidence research");
    expect(defaults.brief).toContain("独立来源");
  });

  it("keeps resident demo reserved for team mode", () => {
    expect(taskPackForExecution("team", "evidence-research")).toBe("resident-demo");
    expect(taskPackForExecution("single", "evidence-research")).toBe("evidence-research");
  });
});
