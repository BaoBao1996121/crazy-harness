import { ApiError, type PairedEvalDraft, type PairedEvalRequest } from "../api/client";

const PENDING_EVAL_REQUEST_KEY = "crazy.pendingEvalRequest.v1";

interface EvalRequestStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => unknown;
  removeItem: (key: string) => unknown;
}

interface PendingEvalRequest {
  version: 1;
  request: PairedEvalRequest;
}

interface EvalRequestIds {
  current: () => string;
  rotate: () => void;
  prepare: (draft: PairedEvalDraft) => PairedEvalRequest;
}

export function generateEvalRequestId(): string {
  return `eval-${globalThis.crypto.randomUUID()}`;
}

export function createEvalRequestIds(
  generate: () => string = generateEvalRequestId,
  storage?: EvalRequestStorage,
): EvalRequestIds {
  let pending = readPendingRequest(storage);
  let current = pending?.request.request_id ?? generate();

  const clearPending = () => {
    pending = undefined;
    storage?.removeItem(PENDING_EVAL_REQUEST_KEY);
  };

  return {
    current: () => current,
    rotate: () => {
      clearPending();
      current = generate();
    },
    prepare: (draft) => {
      if (pending) return pending.request;
      const request = { ...draft, request_id: current };
      pending = { version: 1, request };
      storage?.setItem(PENDING_EVAL_REQUEST_KEY, JSON.stringify(pending));
      return request;
    },
  };
}

function readPendingRequest(storage?: EvalRequestStorage): PendingEvalRequest | undefined {
  const raw = storage?.getItem(PENDING_EVAL_REQUEST_KEY);
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as Partial<PendingEvalRequest>;
    if (
      parsed.version === 1
      && parsed.request
      && typeof parsed.request.request_id === "string"
      && parsed.request.request_id.trim()
    ) {
      return parsed as PendingEvalRequest;
    }
  } catch {
    // A malformed browser entry cannot safely identify a request.
  }
  storage?.removeItem(PENDING_EVAL_REQUEST_KEY);
  return undefined;
}

export async function submitPairedEval<T>(
  draft: PairedEvalDraft,
  requestIds: EvalRequestIds,
  create: (request: PairedEvalRequest) => Promise<T>,
): Promise<T> {
  try {
    const created = await create(requestIds.prepare(draft));
    requestIds.rotate();
    return created;
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) requestIds.rotate();
    throw error;
  }
}
