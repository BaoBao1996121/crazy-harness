import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { Checkpoint } from "../api/client";
import { CheckpointBand } from "./CheckpointBand";

describe("CheckpointBand", () => {
  it("explains fork restore and exposes blockers in Chinese-first text", () => {
    const checkpoint = {
      checkpoint_id: "checkpoint_demo",
      label: "修改后",
      created_at: "2026-07-26T12:00:00Z",
      source: { phase: "observing", turn_id: "turn-4" },
      workspace: { file_count: 4, total_bytes: 1024, object_id: "a".repeat(64) },
      state_refs: { artifacts: [] },
      effects: { effects: [], restore_blockers: ["operation:op-1:unknown"] },
    } as unknown as Checkpoint;

    const html = renderToStaticMarkup(
      <CheckpointBand
        runId="run_demo"
        checkpoints={[checkpoint]}
        selected={checkpoint}
        label=""
        loading={false}
        busy={false}
        onLabelChange={vi.fn()}
        onCreate={vi.fn()}
        onSelect={vi.fn()}
        onRestore={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(html).toContain("检查点工作台");
    expect(html).toContain("派生新 Run");
    expect(html).toContain("恢复受阻");
    expect(html).toContain("operation:op-1:unknown");
    expect(html).toContain("disabled");
  });
});
