import { describe, expect, it } from "vitest";

import { agentLabel, boundaryLabel, contentLabel, statusLabel } from "./i18n";

describe("Chinese-first control-room labels", () => {
  it("keeps the English harness term after the Chinese explanation", () => {
    expect(agentLabel("coordinator")).toBe("总控 / Coordinator");
    expect(statusLabel("succeeded")).toBe("已完成 / Succeeded");
    expect(boundaryLabel("proposal")).toBe("提议 / Proposal");
    expect(contentLabel("Collect verifiable evidence for the incoming task.")).toContain("收集可验证证据");
  });
});
