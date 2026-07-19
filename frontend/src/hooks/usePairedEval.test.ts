import { describe, expect, it, vi } from "vitest";

import { ApiError, type PairedEvalReport } from "../api/client";
import { mergeSearchParam, restoreSearchParam } from "../lib/urlState";
import {
  armForRun,
  evalErrorMessage,
  nextPollDelay,
  resolveInitialEvalId,
} from "./usePairedEval";

describe("paired eval state", () => {
  it("prefers the explicit URL eval and otherwise restores local storage", () => {
    expect(resolveInitialEvalId("?eval=eval_url", "eval_stored")).toBe("eval_url");
    expect(resolveInitialEvalId("?eval=%20", "eval_stored")).toBe("eval_stored");
    expect(resolveInitialEvalId("", null)).toBeUndefined();
  });

  it("overwrites stale storage when the initial URL names an eval", () => {
    const storage = {
      getItem: vi.fn().mockReturnValue("eval_stale"),
      setItem: vi.fn(),
    };

    expect(
      restoreSearchParam(
        "?eval=eval_url",
        "eval",
        "crazy.activeEval",
        storage,
      ),
    ).toBe("eval_url");
    expect(storage.setItem).toHaveBeenCalledWith("crazy.activeEval", "eval_url");
  });

  it("updates one identity without erasing the other one", () => {
    expect(mergeSearchParam("?run=run_single", "eval", "eval_pair")).toBe(
      "?run=run_single&eval=eval_pair",
    );
    expect(mergeSearchParam("?run=run_single&eval=eval_pair", "run", "run_team")).toBe(
      "?run=run_team&eval=eval_pair",
    );
    expect(mergeSearchParam("?run=run_team&eval=eval_pair", "eval", undefined)).toBe(
      "?run=run_team",
    );
  });

  it("polls running pairs and stops after the persisted report completes", () => {
    expect(nextPollDelay({ status: "running" } as PairedEvalReport)).toBe(900);
    expect(nextPollDelay({ status: "completed" } as PairedEvalReport)).toBeUndefined();
  });

  it("defaults to Single unless the URL already names the Team arm", () => {
    const report = {
      single: { run_id: "run_single" },
      team: { run_id: "run_team" },
    } as PairedEvalReport;
    expect(armForRun(report, undefined)).toBe("single");
    expect(armForRun(report, "run_unrelated")).toBe("single");
    expect(armForRun(report, "run_team")).toBe("team");
  });

  it("turns an unknown eval into a Chinese-first recovery message", () => {
    expect(evalErrorMessage(new ApiError("paired eval not found", 404))).toBe(
      "找不到这次公平评测，已清除失效链接 / Eval not found; stale link removed",
    );
    expect(evalErrorMessage(new Error("connection reset"))).toContain("公平评测暂不可用");
  });
});
