import { describe, expect, it, vi } from "vitest";

import { ApiError, type EvalCampaignDraft } from "../api/client";
import {
  cancelEvalCampaignWithRetry,
  createCampaignCancelIntents,
  createCampaignRequestIds,
  hasPendingEvalCampaign,
  recoverPendingEvalCampaign,
  submitEvalCampaign,
} from "./campaignRequests";

const draft: EvalCampaignDraft = {
  title: "Campaign",
  brief: "Repeat the same task.",
  model_mode: "scripted",
  task_pack: "repo-maintainer",
  trial_count: 3,
  max_parallel_pairs: 1,
  model_budget: {
    max_total_tokens: 100,
    max_cost_usd: "0.01",
    max_concurrent_calls: 1,
    max_output_tokens_per_call: 64,
    max_retries_per_call: 0,
  },
  campaign_max_total_tokens: 600,
  campaign_max_cost_usd: "0.06",
};

describe("campaign request identity", () => {
  it("exposes whether startup is reserved by an unconfirmed Campaign request", () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    };
    expect(hasPendingEvalCampaign(storage)).toBe(false);
    createCampaignRequestIds(() => "campaign-pending", storage).prepare(draft);
    expect(hasPendingEvalCampaign(storage)).toBe(true);
  });

  it("replays a persisted ambiguous POST with the same request id after refresh", async () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    };
    const firstPage = createCampaignRequestIds(() => "campaign-paid", storage);
    await expect(submitEvalCampaign(
      draft,
      firstPage,
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    )).rejects.toThrow("Failed to fetch");

    const refreshedPage = createCampaignRequestIds(() => "campaign-wrong", storage);
    const replay = vi.fn().mockResolvedValue({ campaign_id: "campaign_recovered" });

    await expect(recoverPendingEvalCampaign(refreshedPage, replay)).resolves.toEqual({
      campaign_id: "campaign_recovered",
    });
    expect(replay).toHaveBeenCalledWith(expect.objectContaining({
      request_id: "campaign-paid",
      title: "Campaign",
    }));
    expect(createCampaignRequestIds(() => "campaign-next", storage).pending())
      .toBeUndefined();
  });

  it("keeps the pending request until the recovered campaign identity is durable", async () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    };
    const ids = createCampaignRequestIds(() => "campaign-durable", storage);
    ids.prepare(draft);

    await expect(recoverPendingEvalCampaign(
      ids,
      vi.fn().mockResolvedValue({ campaign_id: "campaign_existing" }),
      vi.fn().mockRejectedValue(new Error("local identity write failed")),
    )).rejects.toThrow("local identity write failed");

    expect(createCampaignRequestIds(() => "campaign-wrong", storage).pending())
      .toEqual(expect.objectContaining({ request_id: "campaign-durable" }));
  });

  it("coalesces concurrent refresh recoveries by request id", async () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    };
    const firstPage = createCampaignRequestIds(() => "campaign-concurrent", storage);
    firstPage.prepare(draft);
    const strictModeRemount = createCampaignRequestIds(() => "campaign-wrong", storage);
    const create = vi.fn().mockImplementation(async () => {
      await Promise.resolve();
      return { campaign_id: "campaign_existing" };
    });

    await expect(Promise.all([
      recoverPendingEvalCampaign(firstPage, create),
      recoverPendingEvalCampaign(strictModeRemount, create),
    ])).resolves.toEqual([
      { campaign_id: "campaign_existing" },
      { campaign_id: "campaign_existing" },
    ]);

    expect(create).toHaveBeenCalledTimes(1);
    expect(firstPage.pending()).toBeUndefined();
    expect(strictModeRemount.pending()).toBeUndefined();
  });

  it("recovers a retryable creation conflict in the current page session", async () => {
    const ids = createCampaignRequestIds(() => "campaign-auto-recover");
    ids.prepare(draft);
    const create = vi.fn()
      .mockRejectedValueOnce(new ApiError("claim busy", 409, "in_progress", true))
      .mockResolvedValueOnce({ campaign_id: "campaign_existing" });
    const sleep = vi.fn().mockResolvedValue(undefined);

    await expect(recoverPendingEvalCampaign(
      ids,
      create,
      undefined,
      { maxAttempts: 3, baseDelayMs: 25, sleep },
    )).resolves.toEqual({ campaign_id: "campaign_existing" });

    expect(create).toHaveBeenCalledTimes(2);
    expect(create.mock.calls[0][0].request_id).toBe("campaign-auto-recover");
    expect(create.mock.calls[1][0].request_id).toBe("campaign-auto-recover");
    expect(sleep).toHaveBeenCalledWith(25);
  });

  it("keeps the same request id for retryable claim conflicts", async () => {
    const ids = createCampaignRequestIds(() => "campaign-fixed");
    const create = vi.fn()
      .mockRejectedValueOnce(new ApiError("busy", 409, "in_progress", true))
      .mockResolvedValueOnce({ campaign_id: "campaign_1" });

    await expect(submitEvalCampaign(draft, ids, create)).rejects.toThrow("busy");
    await expect(submitEvalCampaign(draft, ids, create)).resolves.toEqual({
      campaign_id: "campaign_1",
    });

    expect(create.mock.calls[0][0].request_id).toBe("campaign-fixed");
    expect(create.mock.calls[1][0].request_id).toBe("campaign-fixed");
  });

  it("rotates after definitive validation rejection so edited input is not replayed", async () => {
    const generate = vi.fn()
      .mockReturnValueOnce("campaign-invalid")
      .mockReturnValueOnce("campaign-corrected")
      .mockReturnValueOnce("campaign-next");
    const ids = createCampaignRequestIds(generate);
    const create = vi.fn()
      .mockRejectedValueOnce(new ApiError("invalid budget", 400))
      .mockResolvedValueOnce({ campaign_id: "campaign_2" });

    await expect(submitEvalCampaign(draft, ids, create)).rejects.toThrow(
      "invalid budget",
    );
    await expect(submitEvalCampaign(
      { ...draft, title: "Corrected campaign" },
      ids,
      create,
    )).resolves.toEqual({ campaign_id: "campaign_2" });

    expect(create.mock.calls.map(([request]) => ({
      id: request.request_id,
      title: request.title,
    }))).toEqual([
      { id: "campaign-invalid", title: "Campaign" },
      { id: "campaign-corrected", title: "Corrected campaign" },
    ]);
  });

  it("rotates only for 400, 422, or an explicitly non-retryable 409", async () => {
    const makeIds = () => createCampaignRequestIds(() => "campaign-fixed");
    const cases = [
      { error: new ApiError("bad request", 400), retained: false },
      { error: new ApiError("invalid schema", 422), retained: false },
      { error: new ApiError("final conflict", 409, "conflict", false), retained: false },
      { error: new ApiError("unknown conflict", 409, "conflict"), retained: true },
      { error: new ApiError("temporary server failure", 503), retained: true },
    ];

    for (const { error, retained } of cases) {
      const ids = makeIds();
      await expect(submitEvalCampaign(
        draft,
        ids,
        vi.fn().mockRejectedValue(error),
      )).rejects.toThrow(error.message);
      expect(Boolean(ids.pending()), `${error.status}/${String(error.retryable)}`)
        .toBe(retained);
    }
  });
});

describe("campaign cancellation intent", () => {
  it("persists an unconfirmed cancellation across page refreshes", () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    };

    const firstPage = createCampaignCancelIntents(storage);
    firstPage.remember("campaign_paid");
    expect(createCampaignCancelIntents(storage).pending()).toBe("campaign_paid");

    createCampaignCancelIntents(storage).clear("campaign_paid");
    expect(createCampaignCancelIntents(storage).pending()).toBeUndefined();
  });

  it("retries ambiguous network and temporary server failures", async () => {
    const cancel = vi.fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockRejectedValueOnce(new ApiError("gateway unavailable", 503))
      .mockResolvedValueOnce({ campaign_id: "campaign_1", status: "cancelled" });

    await expect(cancelEvalCampaignWithRetry("campaign_1", cancel, {
      maxAttempts: 4,
      baseDelayMs: 0,
      sleep: vi.fn().mockResolvedValue(undefined),
    })).resolves.toEqual({ campaign_id: "campaign_1", status: "cancelled" });
    expect(cancel).toHaveBeenCalledTimes(3);
  });

  it("keeps retrying the same cancellation after retryable claim conflicts", async () => {
    const cancel = vi.fn()
      .mockRejectedValueOnce(new ApiError("advance busy", 409, "in_progress", true))
      .mockRejectedValueOnce(new ApiError("advance busy", 409, "in_progress", true))
      .mockResolvedValueOnce({ campaign_id: "campaign_1", status: "cancelled" });
    const sleep = vi.fn().mockResolvedValue(undefined);

    await expect(cancelEvalCampaignWithRetry("campaign_1", cancel, {
      maxAttempts: 4,
      baseDelayMs: 25,
      sleep,
    })).resolves.toEqual({ campaign_id: "campaign_1", status: "cancelled" });

    expect(cancel).toHaveBeenCalledTimes(3);
    expect(cancel).toHaveBeenNthCalledWith(1, "campaign_1");
    expect(cancel).toHaveBeenNthCalledWith(3, "campaign_1");
    expect(sleep.mock.calls.map(([delay]) => delay)).toEqual([25, 50]);
  });

  it("stops at the retry bound and never retries a definitive conflict", async () => {
    const retryableCancel = vi.fn().mockRejectedValue(
      new ApiError("still busy", 409, "in_progress", true),
    );
    await expect(cancelEvalCampaignWithRetry("campaign_1", retryableCancel, {
      maxAttempts: 3,
      baseDelayMs: 0,
      sleep: vi.fn().mockResolvedValue(undefined),
    })).rejects.toThrow("still busy");
    expect(retryableCancel).toHaveBeenCalledTimes(3);

    const definitiveCancel = vi.fn().mockRejectedValue(
      new ApiError("cannot cancel", 409, "terminal", false),
    );
    await expect(cancelEvalCampaignWithRetry("campaign_1", definitiveCancel, {
      maxAttempts: 8,
      sleep: vi.fn().mockResolvedValue(undefined),
    })).rejects.toThrow("cannot cancel");
    expect(definitiveCancel).toHaveBeenCalledTimes(1);
  });
});
