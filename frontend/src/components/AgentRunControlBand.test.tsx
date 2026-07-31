import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { AgentRunControlBand } from "./AgentRunControlBand";

describe("AgentRunControlBand", () => {
  it("shows durable session state, correction controls, and branch lineage in Chinese", () => {
    const html = renderToStaticMarkup(
      <AgentRunControlBand
        runId="run_child"
        session={{
          identity: {
            run_id: "run_child",
            task_id: "task_demo",
            agent_id: "generalist",
            kind: "single",
          },
          status: "paused",
          completed_turns: 2,
          latest_phase: "result_recording",
          latest_event_id: "event_9",
          latest_event_type: "run.paused",
          capability_manifest_hash: "sha256:1234567890abcdef",
          fork_supported: true,
          fork_ready: true,
          fork_blocker: null,
        }}
        branch={{
          run_id: "run_child",
          parent_run_id: "run_parent",
          checkpoint_id: "checkpoint_1",
          source_event_id: "event_7",
          source_turn_id: "turn_2",
          source_phase: "observe",
          children_run_ids: ["run_grandchild"],
        }}
        nudge="补齐测试证据"
        forkLabel="另一种方案"
        loading={false}
        busy={false}
        onNudgeChange={vi.fn()}
        onForkLabelChange={vi.fn()}
        onPause={vi.fn()}
        onResume={vi.fn()}
        onSendNudge={vi.fn()}
        onFork={vi.fn()}
        onSelectRun={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(html).toContain("运行控制");
    expect(html).toContain("已暂停");
    expect(html).toContain("继续运行");
    expect(html).toContain("下一轮纠偏");
    expect(html).toContain("派生分支");
    expect(html).toContain("父分支");
    expect(html).toContain("2 轮");
    expect(html).toContain("run_parent");
    expect(html).toContain("run_grandchild");
  });

  it("keeps system waiting distinct from an operator pause", () => {
    const html = renderToStaticMarkup(
      <AgentRunControlBand
        runId="run_waiting"
        session={{
          identity: {
            run_id: "run_waiting",
            task_id: "task_waiting",
            agent_id: "generalist",
            kind: "single",
          },
          status: "waiting",
          completed_turns: 1,
          latest_phase: "waiting",
          latest_event_id: "event_waiting",
          latest_event_type: "run.paused",
          capability_manifest_hash: null,
          fork_supported: false,
          fork_ready: false,
          fork_blocker: "当前任务类型不支持工作区分支 / This task pack cannot fork a workspace",
        }}
        branch={null}
        nudge=""
        forkLabel=""
        loading={false}
        busy={false}
        onNudgeChange={vi.fn()}
        onForkLabelChange={vi.fn()}
        onPause={vi.fn()}
        onResume={vi.fn()}
        onSendNudge={vi.fn()}
        onFork={vi.fn()}
        onSelectRun={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(html).toContain("等待事件 / Waiting");
    expect(html).toContain("安全暂停");
    expect(html).not.toContain("继续运行");
    expect(html).toContain("当前任务类型不支持工作区分支");
    expect(html).toMatch(/class="icon-only fork-send"[^>]*disabled/);
  });

  it("renders cancellation as a terminal control state", () => {
    const html = renderToStaticMarkup(
      <AgentRunControlBand
        runId="run_cancelled"
        session={{
          identity: {
            run_id: "run_cancelled",
            task_id: "task_cancelled",
            agent_id: "generalist",
            kind: "single",
          },
          status: "cancelled",
          completed_turns: 1,
          latest_phase: "failed",
          latest_event_id: "event_cancelled",
          latest_event_type: "run.cancelled",
          capability_manifest_hash: null,
          fork_supported: true,
          fork_ready: true,
          fork_blocker: null,
        }}
        branch={null}
        nudge=""
        forkLabel=""
        loading={false}
        busy={false}
        onNudgeChange={vi.fn()}
        onForkLabelChange={vi.fn()}
        onPause={vi.fn()}
        onResume={vi.fn()}
        onSendNudge={vi.fn()}
        onFork={vi.fn()}
        onSelectRun={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(html).toContain("已取消 / Cancelled");
    expect(html).not.toContain("继续运行");
    expect(html).toMatch(/class="icon-command pause-command"[^>]*disabled/);
  });

  it("explains a temporarily unsafe fork boundary without hiding pack support", () => {
    const html = renderToStaticMarkup(
      <AgentRunControlBand
        runId="run_inflight"
        session={{
          identity: {
            run_id: "run_inflight",
            task_id: "task_inflight",
            agent_id: "generalist",
            kind: "single",
          },
          status: "running",
          completed_turns: 1,
          latest_phase: "model_calling",
          latest_event_id: "event_inflight",
          latest_event_type: "model.requested",
          capability_manifest_hash: null,
          fork_supported: true,
          fork_ready: false,
          fork_blocker: "当前不是可派生的安全边界 / unresolved model call prevents checkpoint",
        }}
        branch={null}
        nudge=""
        forkLabel=""
        loading={false}
        busy={false}
        onNudgeChange={vi.fn()}
        onForkLabelChange={vi.fn()}
        onPause={vi.fn()}
        onResume={vi.fn()}
        onSendNudge={vi.fn()}
        onFork={vi.fn()}
        onSelectRun={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(html).toContain("当前不是可派生的安全边界");
    expect(html).toMatch(/class="icon-only fork-send"[^>]*disabled/);
  });
});
