import type { components } from "./schema";

export type Snapshot = components["schemas"]["SnapshotView"];
export type EventPage = components["schemas"]["EventPage"];
export type EventRecord = components["schemas"]["EventRecord"];
export type RunCreated = components["schemas"]["RunCreated"];
export type TaskRequest = components["schemas"]["TaskRequest"];
export type KernelDecision = components["schemas"]["KernelDecision"];
export type FaultPoint =
  | "after_candidate_persisted"
  | "after_model_persisted"
  | "after_command_persisted"
  | "after_tool_effect"
  | "before_mailbox_ack";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
  }
  return response.json() as Promise<T>;
}

export const api = {
  createRun: (body: TaskRequest) =>
    request<RunCreated>("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  drainRun: (runId: string) =>
    request<{ run_id: string; steps: number }>(`/api/runs/${runId}/drain`, { method: "POST" }),
  snapshot: (runId?: string) =>
    request<Snapshot>(`/api/snapshot${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`),
  events: (runId: string, after = 0) =>
    request<EventPage>(`/api/events?run_id=${encodeURIComponent(runId)}&after=${after}`),
  armFault: (point: FaultPoint) =>
    request<{ armed: string; mode: string }>("/api/chaos/faults", {
      method: "POST",
      body: JSON.stringify({ point }),
    }),
  peerProbe: (body: { run_id: string; sender: string; receiver: string; depth: number }) =>
    request<KernelDecision>("/api/chaos/peer-probe", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  rebuildProjections: () =>
    request<{ status: string }>("/api/projections/rebuild", { method: "POST" }),
};
