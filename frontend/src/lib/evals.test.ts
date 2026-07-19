import { describe, expect, it } from "vitest";

import type { PairedEvalReport } from "../api/client";
import {
  draftModelComparisonLabel,
  fairnessChecks,
  recommendationPresentation,
} from "./evals";

function reportFixture(): PairedEvalReport {
  const shared = {
    input_hash: "sha256:shared",
    model_profile: { provider: "scripted", model: "scripted" },
    model_budget: { max_total_tokens: 250000, max_cost_usd: "0.10" },
  };
  return {
    eval_id: "eval_demo",
    status: "running",
    evidence_valid: true,
    invalid_reasons: [],
    contract: {
      eval_id: "eval_demo",
      case_id: "case_repo_maintainer",
      task_pack: "repo-maintainer",
      fixture_hash: "sha256:fixture",
      scorer_version: "repo-maintainer-v1",
      evidence_tier: "deterministic",
      single: {
        execution_mode: "single",
        run_id: "run_single",
        workspace: "runs/evals/eval_demo/single",
        ...shared,
      },
      team: {
        execution_mode: "team",
        run_id: "run_team",
        workspace: "runs/evals/eval_demo/team",
        ...shared,
      },
    },
    single: { execution_mode: "single", run_id: "run_single", status: "running" },
    team: { execution_mode: "team", run_id: "run_team", status: "queued" },
  };
}

describe("paired eval presentation", () => {
  it("does not promise the same model in the scripted creation preview", () => {
    expect(draftModelComparisonLabel("scripted")).toBe(
      "不同确定性脚本 / Different deterministic scripts",
    );
    expect(draftModelComparisonLabel("deepseek")).toBe(
      "同模型 / Same model",
    );
  });

  it("derives the four fairness promises from the immutable contract", () => {
    expect(fairnessChecks(reportFixture())).toEqual([
      expect.objectContaining({ id: "input", passed: true }),
      expect.objectContaining({
        id: "model",
        label: "不同确定性脚本 / Different deterministic scripts",
        passed: true,
      }),
      expect.objectContaining({ id: "budget", passed: true }),
      expect.objectContaining({ id: "workspace", passed: true }),
    ]);
  });

  it("claims the same model only for a live pair with matching model profiles", () => {
    const report = reportFixture();
    report.contract.evidence_tier = "live_paired";
    report.contract.single.model_profile = {
      provider: "deepseek",
      model: "deepseek-v4-flash",
    };
    report.contract.team.model_profile = {
      provider: "deepseek",
      model: "deepseek-v4-flash",
    };

    expect(fairnessChecks(report).find((check) => check.id === "model")).toEqual({
      id: "model",
      label: "同模型且有调用证明 / Same model + attestation",
      passed: true,
    });
  });

  it("surfaces a completed live pair whose evidence attestation is invalid", () => {
    const report = reportFixture();
    report.status = "completed";
    report.contract.evidence_tier = "live_paired";
    report.evidence_valid = false;
    report.invalid_reasons = [
      "single arm has no persisted model call attestation",
    ];
    report.recommendation = {
      outcome: "insufficient_live_evidence",
      reason: "paired evidence is invalid and cannot change routing",
      failed_thresholds: ["invalid_evidence"],
    };

    expect(fairnessChecks(report).find((check) => check.id === "model")).toEqual({
      id: "model",
      label: "同模型且有调用证明 / Same model + attestation",
      passed: false,
    });
    expect(recommendationPresentation(report)).toMatchObject({
      outcome: "insufficient_live_evidence",
      title: "评测证据无效 / Invalid eval evidence",
      tone: "caution",
    });
    expect(recommendationPresentation(report).detail).toContain(
      "single arm has no persisted model call attestation",
    );
  });

  it("never presents deterministic evidence as proof that Team is better", () => {
    const report = reportFixture();
    report.status = "completed";
    report.recommendation = {
      outcome: "recommend_team",
      reason: "unexpected upstream recommendation",
      failed_thresholds: [],
    };

    expect(recommendationPresentation(report)).toMatchObject({
      outcome: "insufficient_live_evidence",
      title: "真实证据不足 / Insufficient live evidence",
      tone: "caution",
    });
  });
});
