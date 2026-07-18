import { describe, expect, it } from "vitest";

import { normalizeScheduler, schedulerPressure } from "./scheduler";

describe("scheduler observability", () => {
  it("normalizes the runtime contract and exposes demand pressure", () => {
    const scheduler = normalizeScheduler({
      instance_id: "scheduler_alpha",
      state: "accepting",
      policy: "round_robin",
      fairness_scope: "process_workers",
      active: 2,
      capacity: 2,
      queued: 3,
      workers: [
        { worker_id: "builder", active: 1, capacity: 1, queued: 2 },
        { worker_id: "reviewer", active: 1, capacity: 1, queued: 1 },
      ],
    });

    expect(scheduler).toMatchObject({
      instanceId: "scheduler_alpha",
      state: "accepting",
      policy: "round_robin",
      fairnessScope: "process_workers",
      active: 2,
      capacity: 2,
      queued: 3,
    });
    expect(scheduler.workers.get("builder")).toEqual({
      workerId: "builder",
      active: 1,
      capacity: 1,
      queued: 2,
    });
    expect(schedulerPressure(scheduler)).toEqual({
      kind: "backpressure",
      label: "背压 / Backpressure",
      percent: 250,
    });
  });

  it("fails soft when an older snapshot has no scheduler payload", () => {
    const scheduler = normalizeScheduler(undefined);

    expect(scheduler.active).toBe(0);
    expect(scheduler.capacity).toBe(0);
    expect(scheduler.workers.size).toBe(0);
    expect(schedulerPressure(scheduler).label).toBe("空闲 / Idle");
  });
});
