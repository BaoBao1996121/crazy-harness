import { describe, expect, it } from "vitest";

import { eventMeta, selectVisibleEvents, type EventRecord } from "./events";

const record = (cursor: number, type: string): EventRecord => ({
  cursor,
  event: {
    id: `event-${cursor}`,
    run_id: "run-1",
    task_id: "task-1",
    type,
    source: "test",
    payload: {},
    refs: [],
    causation_id: null,
    created_at: "2026-07-16T00:00:00Z",
  },
});

describe("timeline event vocabulary", () => {
  it("keeps causal milestones and hides scheduler noise in focus mode", () => {
    const input = [
      record(1, "runtime.agent.busy"),
      record(2, "model.completed"),
      record(3, "candidate.accepted"),
      record(4, "completion.gate.passed"),
    ];

    expect(selectVisibleEvents(input, false).map((item) => item.cursor)).toEqual([2, 3, 4]);
    expect(selectVisibleEvents(input, true)).toEqual(input);
  });

  it("labels the proposal and fact sides of the trust boundary differently", () => {
    expect(eventMeta("model.completed").boundary).toBe("proposal");
    expect(eventMeta("evidence.recorded").boundary).toBe("fact");
    expect(eventMeta("a2a.policy.denied").tone).toBe("danger");
  });

  it("labels temporary orchestration capacity waiting and recovery in Chinese first", () => {
    expect(eventMeta("orchestration.capacity.waiting").label).toBe(
      "等待 Agent 容量 / Waiting for agent capacity",
    );
    expect(eventMeta("orchestration.capacity.waiting").tone).toBe("warning");
    expect(eventMeta("orchestration.capacity.resumed").label).toBe(
      "Agent 容量恢复 / Agent capacity restored",
    );
    expect(eventMeta("orchestration.capacity.resumed").tone).toBe("success");
  });

  it("renders the durable Eval and Campaign lifecycle in Chinese-first bilingual labels", () => {
    const expected = {
      "eval.arm.planned": "评测臂已规划 / Eval arm planned",
      "eval.arm.created": "评测臂已创建 / Eval arm created",
      "eval.arm.linked": "评测臂已关联 / Eval arm linked",
      "eval.arm.released": "评测臂已释放执行 / Eval arm released",
      "eval.pair.committed": "配对契约已提交 / Eval pair committed",
      "eval.pair.completed": "配对评测已完成 / Eval pair completed",
      "eval.campaign.requested": "多轮评测已请求 / Campaign requested",
      "eval.campaign.created": "多轮评测已创建 / Campaign created",
      "eval.campaign.trial.started": "Trial 已启动 / Campaign trial started",
      "eval.campaign.trial.observed": "Trial 证据已观察 / Campaign trial observed",
      "eval.campaign.completed": "多轮评测已完成 / Campaign completed",
      "eval.campaign.cancelled": "多轮评测已取消 / Campaign cancelled",
    };

    for (const [type, label] of Object.entries(expected)) {
      expect(eventMeta(type).label, type).toBe(label);
      expect(eventMeta(type).label, type).not.toBe(type);
    }
  });

  it("gives low-level runtime events a Chinese-first label", () => {
    expect(eventMeta("agent.result.submitted").label).toBe("Agent 结果提交 / Agent result submitted");
    expect(eventMeta("completion.requested").label).toBe("申请完成 / Completion requested");
    expect(eventMeta("evolution.signal.ready").label).toBe("进化信号就绪 / Evolution signal ready");
  });

  it("shows capability disclosure as a context control decision", () => {
    const meta = eventMeta("capability.manifest.compiled");

    expect(meta.label).toBe("能力披露清单 / Capability manifest");
    expect(meta.family).toBe("context");
    expect(meta.boundary).toBe("control");
  });

  it("explains lease lifecycle and stale fencing in Chinese-first labels", () => {
    expect(eventMeta("assignment.lease.acquired").label).toBe("租约获取 / Lease acquired");
    expect(eventMeta("assignment.lease.expired").label).toBe("租约超时 / Lease expired");
    expect(eventMeta("assignment.delivery.stale").label).toBe("过期投递已隔离 / Stale delivery fenced");
    expect(eventMeta("assignment.lease.expired").tone).toBe("danger");
    expect(eventMeta("assignment.delivery.stale").important).toBe(true);
  });

  it("keeps heartbeat and renewal noise out of focus mode", () => {
    const input = [record(1, "runtime.agent.heartbeat"), record(2, "assignment.lease.renewed")];

    expect(selectVisibleEvents(input, false)).toEqual([]);
    expect(selectVisibleEvents(input, true)).toEqual(input);
  });

  it("labels the child AgentRun lifecycle and result promotion explicitly", () => {
    expect(eventMeta("agent.run.created").label).toBe("子 AgentRun 创建 / Child AgentRun created");
    expect(eventMeta("agent.waiting").label).toBe("Agent 等待外部事件 / Agent waiting");
    expect(eventMeta("a2a.message.sent").label).toBe("A2A 消息已发出 / A2A message sent");
    expect(eventMeta("agent.result.promoted").label).toBe("结果晋升为团队事实 / Result promoted");
    expect(eventMeta("agent.result.rejected").label).toBe("结果晋升被拒 / Result promotion rejected");
    expect(eventMeta("agent.result.rejected").tone).toBe("danger");
  });

  it("keeps AgentRun milestones and hides per-turn mechanics in focus mode", () => {
    const input = [
      record(1, "loop.phase.changed"),
      record(2, "runtime.turn.ready"),
      record(3, "agent.run.created"),
      record(4, "agent.waiting"),
      record(5, "a2a.message.sent"),
      record(6, "agent.result.promoted"),
    ];

    expect(selectVisibleEvents(input, false).map((item) => item.cursor)).toEqual([3, 4, 5, 6]);
    expect(selectVisibleEvents(input, true)).toEqual(input);
  });

  it("labels scheduler recovery and dead-letter facts in Chinese first", () => {
    expect(eventMeta("runtime.scheduler.cycle.failed").label).toBe(
      "调度周期故障，正在恢复 / Scheduler cycle recovering",
    );
    expect(eventMeta("mailbox.delivery.dead_lettered").label).toBe(
      "邮箱消息进入死信 / Delivery dead-lettered",
    );
    expect(eventMeta("a2a.peer.failure.unroutable").label).toBe(
      "同伴失败无法路由 / Peer failure unroutable",
    );
    expect(eventMeta("mailbox.delivery.dead_lettered").tone).toBe("danger");
    expect(eventMeta("mailbox.delivery.dead_lettered").important).toBe(true);
  });

  it("hides routine claim renewal but highlights concurrency and cancellation signals", () => {
    const routineRenewal = record(1, "runtime.delivery.claim.renewed");
    const backpressure = record(2, "runtime.scheduler.backpressure");
    const claimLost = record(3, "runtime.delivery.claim.lost");
    const cancelled = record(4, "run.cancelled");

    expect(selectVisibleEvents([routineRenewal, backpressure, claimLost, cancelled], false))
      .toEqual([backpressure, claimLost, cancelled]);
    expect(eventMeta("runtime.scheduler.backpressure").label).toBe(
      "调度器背压 / Scheduler backpressure",
    );
    expect(eventMeta("runtime.delivery.claim.lost").tone).toBe("danger");
    expect(eventMeta("runtime.delivery.cancelled").tone).toBe("warning");
    expect(eventMeta("run.cancel.requested").label).toBe("运行取消请求 / Run cancellation requested");
  });
});
