import { describe, expect, it } from "vitest";

import { agentLabel, boundaryLabel, contentLabel, statusLabel, toolLabel } from "./i18n";

describe("Chinese-first control-room labels", () => {
  it("keeps the English harness term after the Chinese explanation", () => {
    expect(agentLabel("coordinator")).toBe("总控 / Coordinator");
    expect(statusLabel("succeeded")).toBe("已完成 / Succeeded");
    expect(boundaryLabel("proposal")).toBe("提议 / Proposal");
    expect(contentLabel("Collect verifiable evidence for the incoming task.")).toContain("收集可验证证据");
  });

  it("explains evidence-research tools without hiding their canonical names", () => {
    expect(toolLabel("research.source.open")).toBe("浏览器打开证据源 / research.source.open");
    expect(toolLabel("research.report.validate")).toBe("校验研究报告 / research.report.validate");
  });

  it("labels the backup worker and lease terminal states", () => {
    expect(agentLabel("scout-backup")).toBe("侦察备用 / Scout Backup");
    expect(statusLabel("released")).toBe("已释放 / Released");
    expect(statusLabel("expired")).toBe("已超时 / Expired");
  });

  it("labels scheduler and cancellation states in both languages", () => {
    expect(statusLabel("accepting")).toBe("接收中 / Accepting");
    expect(statusLabel("paused")).toBe("已暂停 / Paused");
    expect(statusLabel("cancelling")).toBe("取消中 / Cancelling");
    expect(statusLabel("cancelled")).toBe("已取消 / Cancelled");
  });
});
