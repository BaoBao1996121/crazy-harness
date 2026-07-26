import type { components } from "./schema";

export type Snapshot = components["schemas"]["SnapshotView"];
export type EventPage = components["schemas"]["EventPage"];
export type EventRecord = components["schemas"]["EventRecord"];
export type RunCreated = components["schemas"]["RunCreated"];
export type CancelResult = components["schemas"]["CancelResult"];
export type TaskRequest = components["schemas"]["TaskRequest"];
export type KernelDecision = components["schemas"]["KernelDecision"];
export type PairedEvalRequest = components["schemas"]["PairedEvalCreateRequest"];
export type PairedEvalDraft = Omit<PairedEvalRequest, "request_id">;
export type PairedEvalCreated = components["schemas"]["PairedEvalCreated"];
export type PairedEvalReport = components["schemas"]["PairedEvalReport"];
export type PairedEvalArmReport = components["schemas"]["PairedEvalArmReport"];
export type EvalCampaignRequest = components["schemas"]["EvalCampaignRequest"];
export type EvalCampaignDraft = Omit<EvalCampaignRequest, "request_id">;
export type EvalCampaignCreated = components["schemas"]["EvalCampaignCreated"];
export type EvalCampaignReport = components["schemas"]["EvalCampaignReport"];
export type CampaignTrialSummary = components["schemas"]["CampaignTrialSummary"];
export type Checkpoint = components["schemas"]["CheckpointContract"];
export type CheckpointCreateRequest = components["schemas"]["CheckpointCreateRequest"];
export type CheckpointRestoreRequest = components["schemas"]["CheckpointRestoreRequest"];
export type CheckpointRestored = components["schemas"]["CheckpointRestored"];
export type FaultPoint =
  | "after_candidate_persisted"
  | "after_model_persisted"
  | "after_command_persisted"
  | "after_tool_effect"
  | "before_mailbox_ack";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly retryable?: boolean,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = body.detail;
    const structured = typeof detail === "object" && detail !== null
      ? detail as { code?: unknown; message?: unknown; retryable?: unknown }
      : undefined;
    const message = typeof detail === "string"
      ? detail
      : typeof structured?.message === "string"
        ? structured.message
        : JSON.stringify(detail);
    throw new ApiError(
      message,
      response.status,
      typeof structured?.code === "string" ? structured.code : undefined,
      typeof structured?.retryable === "boolean" ? structured.retryable : undefined,
    );
  }
  return response.json() as Promise<T>;
}

export const api = {
  createEvalPair: (body: PairedEvalRequest) =>
    request<PairedEvalCreated>("/api/evals/pairs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listEvalPairs: () => request<PairedEvalReport[]>("/api/evals/pairs"),
  evalPair: (evalId: string) =>
    request<PairedEvalReport>(`/api/evals/pairs/${encodeURIComponent(evalId)}`),
  drainEvalPair: (evalId: string) =>
    request<PairedEvalReport>(`/api/evals/pairs/${encodeURIComponent(evalId)}/drain`, {
      method: "POST",
    }),
  createEvalCampaign: (body: EvalCampaignRequest) =>
    request<EvalCampaignCreated>("/api/evals/campaigns", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listEvalCampaigns: () =>
    request<EvalCampaignReport[]>("/api/evals/campaigns"),
  evalCampaign: (campaignId: string) =>
    request<EvalCampaignReport>(
      `/api/evals/campaigns/${encodeURIComponent(campaignId)}`,
    ),
  drainEvalCampaign: (campaignId: string) =>
    request<EvalCampaignReport>(
      `/api/evals/campaigns/${encodeURIComponent(campaignId)}/drain`,
      { method: "POST" },
    ),
  cancelEvalCampaign: (campaignId: string) =>
    request<EvalCampaignReport>(
      `/api/evals/campaigns/${encodeURIComponent(campaignId)}/cancel`,
      { method: "POST" },
    ),
  createRun: (body: TaskRequest) =>
    request<RunCreated>("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  drainRun: (runId: string) =>
    request<{ run_id: string; steps: number }>(`/api/runs/${runId}/drain`, { method: "POST" }),
  cancelRun: (runId: string) =>
    request<CancelResult>(`/api/runs/${runId}/cancel`, { method: "POST" }),
  createCheckpoint: (runId: string, body: CheckpointCreateRequest) =>
    request<Checkpoint>(`/api/runs/${encodeURIComponent(runId)}/checkpoints`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listCheckpoints: (runId: string) =>
    request<Checkpoint[]>(`/api/runs/${encodeURIComponent(runId)}/checkpoints`),
  checkpoint: (checkpointId: string) =>
    request<Checkpoint>(`/api/checkpoints/${encodeURIComponent(checkpointId)}`),
  restoreCheckpoint: (checkpointId: string, body: CheckpointRestoreRequest) =>
    request<CheckpointRestored>(
      `/api/checkpoints/${encodeURIComponent(checkpointId)}/restore`,
      { method: "POST", body: JSON.stringify(body) },
    ),
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
