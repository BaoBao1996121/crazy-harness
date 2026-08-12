import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { EngineeringLoopReport } from "../api/client";
import { EngineeringLoopBand } from "./EngineeringLoopBand";

function report(status: EngineeringLoopReport["status"] = "completed") {
  return {
    loop_id: "loop_quality",
    status,
    contract: {
      title: "仓库质量爬坡",
      objective: "先修复行为，再清理质量标记",
      exit_criteria: ["quality_score reaches 1"],
      loop_pack: "repo-quality",
      initial_state_ref: "loop-pack://initial",
      promotion_mode: "auto_disposable",
      metric: { name: "quality_score", direction: "maximize", target: "1", evaluator_version: "repo-quality-v1" },
      budget: { max_iterations: 3, max_no_progress_iterations: 2 },
    },
    active_state_ref: "snapshot://final",
    active_score: "1.0",
    no_progress_count: 0,
    iterations: [
      {
        identity: { iteration: 1, iteration_id: "iteration_1", candidate_id: "candidate_1", child_run_id: "run_child_1", child_task_id: "task_child_1" },
        status: "decided",
        base_state_ref: "loop-pack://initial",
        candidate: { candidate_id: "candidate_1", iteration: 1, base_state_ref: "loop-pack://initial", change_set: { intent: "repair" }, rationale: "repair failing behavior", expected_effect: "tests pass", proposer_attestation: { version: "v1" } },
        outcome: { run_id: "run_child_1", task_id: "task_child_1", status: "succeeded", terminal_event_id: "event_child_1", candidate_state_ref: "snapshot://one", artifact_refs: [], evidence_refs: ["artifact://test-1"] },
        evaluation: { iteration: 1, candidate_id: "candidate_1", candidate_state_ref: "snapshot://one", evaluator_version: "repo-quality-v1", metrics: { quality_score: "0.5" }, hard_gates: { tests_passed: true }, evidence_refs: ["artifact://eval-1"], valid: true, invalid_reasons: [] },
        decision: { kind: "accept_continue", iteration: 1, candidate_id: "candidate_1", accepted: true, score: "0.5", active_state_ref: "snapshot://one", next_no_progress_count: 0, reason: "improved but target not reached" },
      },
    ],
  } as unknown as EngineeringLoopReport;
}

describe("EngineeringLoopBand", () => {
  it("renders the candidate, canonical child, independent evaluation, and decision as one story", () => {
    const html = renderToStaticMarkup(
      <EngineeringLoopBand
        loopId="loop_quality"
        report={report()}
        loading={false}
        busy={false}
        onAdvance={vi.fn()}
        onDrain={vi.fn()}
        onPause={vi.fn()}
        onResume={vi.fn()}
        onCancel={vi.fn()}
        onSelectRun={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(html).toContain("仓库质量爬坡");
    expect(html).toContain("先修复行为，再清理质量标记");
    expect(html).toContain("候选 / Candidate");
    expect(html).toContain("独立评测 / Independent Eval");
    expect(html).toContain("接受并继续 / Accept + Continue");
    expect(html).toContain("run_child_1");
    expect(html).toContain("0.5");
  });

  it("states that pausing the parent does not pretend an active child is frozen", () => {
    const html = renderToStaticMarkup(
      <EngineeringLoopBand
        loopId="loop_quality"
        report={report("paused")}
        loading={false}
        busy={false}
        onAdvance={vi.fn()}
        onDrain={vi.fn()}
        onPause={vi.fn()}
        onResume={vi.fn()}
        onCancel={vi.fn()}
        onSelectRun={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(html).toContain("继续循环 / Resume");
    expect(html).toContain("父循环已暂停；已经发布的子运行可能仍在收尾");
  });
});
