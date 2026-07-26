import {
  ApiError,
  type EvalCampaignDraft,
  type EvalCampaignRequest,
} from "../api/client";

const PENDING_CAMPAIGN_KEY = "crazy.pendingCampaignRequest.v1";
const PENDING_CAMPAIGN_CANCEL_KEY = "crazy.pendingCampaignCancel.v1";
const PENDING_RECOVERIES = new Map<string, Promise<unknown>>();
const PENDING_CANCELLATIONS = new Map<string, Promise<unknown>>();

interface RequestStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => unknown;
  removeItem: (key: string) => unknown;
}

interface PendingCampaignRequest {
  version: 1;
  request: EvalCampaignRequest;
}

interface PendingCampaignCancel {
  version: 1;
  campaign_id: string;
}

interface CampaignCancelRetryOptions {
  maxAttempts?: number;
  baseDelayMs?: number;
  sleep?: (delayMs: number) => Promise<void>;
  onRetry?: (
    completedAttempts: number,
    maxAttempts: number,
    delayMs: number,
  ) => void;
}

type CampaignRecoveryRetryOptions = CampaignCancelRetryOptions;

export interface CampaignRequestIds {
  prepare: (draft: EvalCampaignDraft) => EvalCampaignRequest;
  pending: () => EvalCampaignRequest | undefined;
  rotate: () => void;
}

export interface CampaignCancelIntents {
  remember: (campaignId: string) => void;
  pending: () => string | undefined;
  clear: (campaignId: string) => void;
}

export function createCampaignCancelIntents(
  storage?: RequestStorage,
): CampaignCancelIntents {
  let pending = readPendingCancel(storage);
  return {
    remember: (campaignId) => {
      pending = { version: 1, campaign_id: campaignId };
      storage?.setItem(PENDING_CAMPAIGN_CANCEL_KEY, JSON.stringify(pending));
    },
    pending: () => pending?.campaign_id,
    clear: (campaignId) => {
      if (pending?.campaign_id !== campaignId) return;
      pending = undefined;
      storage?.removeItem(PENDING_CAMPAIGN_CANCEL_KEY);
    },
  };
}

export function createCampaignRequestIds(
  generate: () => string = () => `campaign-${globalThis.crypto.randomUUID()}`,
  storage?: RequestStorage,
): CampaignRequestIds {
  let pending = readPending(storage);
  let current = pending?.request.request_id ?? generate();

  return {
    prepare: (draft) => {
      if (pending) return pending.request;
      const request = { ...draft, request_id: current };
      pending = { version: 1, request };
      storage?.setItem(PENDING_CAMPAIGN_KEY, JSON.stringify(pending));
      return request;
    },
    pending: () => pending?.request,
    rotate: () => {
      pending = undefined;
      storage?.removeItem(PENDING_CAMPAIGN_KEY);
      current = generate();
    },
  };
}

export async function recoverPendingEvalCampaign<T>(
  ids: CampaignRequestIds,
  create: (request: EvalCampaignRequest) => Promise<T>,
  confirm?: (result: T) => Promise<void> | void,
  options: CampaignRecoveryRetryOptions = {},
): Promise<T | undefined> {
  const pending = ids.pending();
  if (!pending) return undefined;
  const requestId = pending.request_id;
  let recovery = PENDING_RECOVERIES.get(requestId) as Promise<T> | undefined;
  const ownsRecovery = recovery === undefined;
  if (!recovery) {
    recovery = createPendingCampaignWithRetry(
      pending,
      create,
      confirm,
      options,
    );
    PENDING_RECOVERIES.set(requestId, recovery);
  }
  try {
    const result = await recovery;
    ids.rotate();
    return result;
  } catch (error) {
    rotateAfterDefinitiveRejection(error, ids);
    throw error;
  } finally {
    if (ownsRecovery && PENDING_RECOVERIES.get(requestId) === recovery) {
      PENDING_RECOVERIES.delete(requestId);
    }
  }
}

async function createPendingCampaignWithRetry<T>(
  request: EvalCampaignRequest,
  create: (request: EvalCampaignRequest) => Promise<T>,
  confirm: ((result: T) => Promise<void> | void) | undefined,
  options: CampaignRecoveryRetryOptions,
): Promise<T> {
  const maxAttempts = Math.max(1, Math.min(options.maxAttempts ?? 4, 8));
  const baseDelayMs = Math.max(0, options.baseDelayMs ?? 200);
  const sleep = options.sleep ?? ((delayMs: number) => (
    new Promise<void>((resolve) => globalThis.setTimeout(resolve, delayMs))
  ));

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const result = await create(request);
      await confirm?.(result);
      return result;
    } catch (error) {
      if (!isRetryableAmbiguousRequest(error) || attempt === maxAttempts) {
        throw error;
      }
      const delayMs = Math.min(baseDelayMs * (2 ** (attempt - 1)), 2_000);
      options.onRetry?.(attempt, maxAttempts, delayMs);
      await sleep(delayMs);
    }
  }
  throw new Error("Campaign recovery retry loop exhausted");
}

function readPending(storage?: RequestStorage): PendingCampaignRequest | undefined {
  const raw = storage?.getItem(PENDING_CAMPAIGN_KEY);
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as Partial<PendingCampaignRequest>;
    if (
      parsed.version === 1
      && parsed.request
      && typeof parsed.request.request_id === "string"
      && parsed.request.request_id.trim()
    ) {
      return parsed as PendingCampaignRequest;
    }
  } catch {
    // Invalid browser state cannot safely identify a paid Campaign request.
  }
  storage?.removeItem(PENDING_CAMPAIGN_KEY);
  return undefined;
}

function readPendingCancel(storage?: RequestStorage): PendingCampaignCancel | undefined {
  const raw = storage?.getItem(PENDING_CAMPAIGN_CANCEL_KEY);
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as Partial<PendingCampaignCancel>;
    if (
      parsed.version === 1
      && typeof parsed.campaign_id === "string"
      && parsed.campaign_id.trim()
    ) {
      return parsed as PendingCampaignCancel;
    }
  } catch {
    // A malformed local intent cannot identify a Campaign safely.
  }
  storage?.removeItem(PENDING_CAMPAIGN_CANCEL_KEY);
  return undefined;
}

export function hasPendingEvalCampaign(storage?: RequestStorage): boolean {
  return Boolean(readPending(storage));
}

export async function submitEvalCampaign<T>(
  draft: EvalCampaignDraft,
  ids: CampaignRequestIds,
  create: (request: EvalCampaignRequest) => Promise<T>,
  confirm?: (result: T) => Promise<void> | void,
): Promise<T> {
  let result: T;
  try {
    result = await create(ids.prepare(draft));
  } catch (error) {
    rotateAfterDefinitiveRejection(error, ids);
    throw error;
  }
  await confirm?.(result);
  ids.rotate();
  return result;
}

export async function cancelEvalCampaignWithRetry<T>(
  campaignId: string,
  cancel: (campaignId: string) => Promise<T>,
  options: CampaignCancelRetryOptions = {},
): Promise<T> {
  const active = PENDING_CANCELLATIONS.get(campaignId) as Promise<T> | undefined;
  if (active) return active;
  const cancellation = cancelWithRetry(campaignId, cancel, options);
  PENDING_CANCELLATIONS.set(campaignId, cancellation);
  try {
    return await cancellation;
  } finally {
    if (PENDING_CANCELLATIONS.get(campaignId) === cancellation) {
      PENDING_CANCELLATIONS.delete(campaignId);
    }
  }
}

async function cancelWithRetry<T>(
  campaignId: string,
  cancel: (campaignId: string) => Promise<T>,
  options: CampaignCancelRetryOptions,
): Promise<T> {
  const maxAttempts = Math.max(1, Math.min(options.maxAttempts ?? 4, 8));
  const baseDelayMs = Math.max(0, options.baseDelayMs ?? 200);
  const sleep = options.sleep ?? ((delayMs: number) => (
    new Promise<void>((resolve) => globalThis.setTimeout(resolve, delayMs))
  ));

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      return await cancel(campaignId);
    } catch (error) {
      const retryable = isRetryableAmbiguousRequest(error);
      if (!retryable || attempt === maxAttempts) throw error;
      const delayMs = Math.min(baseDelayMs * (2 ** (attempt - 1)), 2_000);
      options.onRetry?.(attempt, maxAttempts, delayMs);
      await sleep(delayMs);
    }
  }
  throw new Error("Campaign cancellation retry loop exhausted");
}

function isRetryableAmbiguousRequest(error: unknown): boolean {
  return error instanceof TypeError || (
    error instanceof ApiError
    && (
      error.retryable === true
      || error.status === 408
      || error.status === 425
      || error.status === 429
      || error.status >= 500
    )
  );
}

function rotateAfterDefinitiveRejection(
  error: unknown,
  ids: CampaignRequestIds,
): void {
  if (
    error instanceof ApiError
    && (
      error.status === 400
      || error.status === 422
      || (error.status === 409 && error.retryable === false)
    )
  ) {
    ids.rotate();
  }
}
