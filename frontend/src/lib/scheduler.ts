export interface SchedulerWorker {
  workerId: string;
  active: number;
  capacity: number;
  queued: number;
}

export interface SchedulerObservation {
  instanceId: string;
  state: string;
  policy: string;
  fairnessScope: string;
  active: number;
  capacity: number;
  queued: number;
  workers: Map<string, SchedulerWorker>;
}

export type PressureKind = "idle" | "steady" | "saturated" | "backpressure" | "paused";

export interface SchedulerPressure {
  kind: PressureKind;
  label: string;
  percent: number;
}

export function normalizeScheduler(value: unknown): SchedulerObservation {
  const source = asRecord(value);
  const workers = new Map<string, SchedulerWorker>();
  if (Array.isArray(source.workers)) {
    source.workers.forEach((item) => {
      const worker = asRecord(item);
      const workerId = stringValue(worker.worker_id);
      if (!workerId) return;
      workers.set(workerId, {
        workerId,
        active: countValue(worker.active),
        capacity: countValue(worker.capacity),
        queued: countValue(worker.queued),
      });
    });
  }
  return {
    instanceId: stringValue(source.instance_id),
    state: stringValue(source.state) || "unknown",
    policy: stringValue(source.policy) || "unknown",
    fairnessScope: stringValue(source.fairness_scope) || "unknown",
    active: countValue(source.active),
    capacity: countValue(source.capacity),
    queued: countValue(source.queued),
    workers,
  };
}

export function schedulerPressure(scheduler: SchedulerObservation): SchedulerPressure {
  const demand = scheduler.active + scheduler.queued;
  const percent = scheduler.capacity > 0
    ? Math.round((demand / scheduler.capacity) * 100)
    : demand > 0 ? 100 : 0;
  if (scheduler.state === "paused") {
    return { kind: "paused", label: "暂停 / Paused", percent };
  }
  if (scheduler.queued > 0) {
    return { kind: "backpressure", label: "背压 / Backpressure", percent };
  }
  if (scheduler.capacity > 0 && scheduler.active >= scheduler.capacity) {
    return { kind: "saturated", label: "满载 / Saturated", percent };
  }
  if (scheduler.active > 0) {
    return { kind: "steady", label: "平稳 / Steady", percent };
  }
  return { kind: "idle", label: "空闲 / Idle", percent };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function countValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : 0;
}
