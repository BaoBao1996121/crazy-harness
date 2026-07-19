import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { createEvalRequestIds, submitPairedEval } from "./evalRequests";

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  };
}

describe("paired eval request identity", () => {
  it("reuses a request id after failure and rotates it only after success", async () => {
    const generateRequestId = vi
      .fn()
      .mockReturnValueOnce("eval-request-0001")
      .mockReturnValueOnce("eval-request-0002")
      .mockReturnValueOnce("eval-request-0003");
    const requestIds = createEvalRequestIds(generateRequestId);
    const create = vi
      .fn()
      .mockRejectedValueOnce(new Error("connection reset"))
      .mockResolvedValueOnce({ eval_id: "eval_1" })
      .mockResolvedValueOnce({ eval_id: "eval_2" });
    const draft = {
      title: "Repair greeting",
      brief: "Apply the same repair to both arms.",
      model_mode: "scripted" as const,
      task_pack: "repo-maintainer" as const,
    };

    await expect(submitPairedEval(draft, requestIds, create)).rejects.toThrow(
      "connection reset",
    );
    await expect(submitPairedEval(draft, requestIds, create)).resolves.toEqual({
      eval_id: "eval_1",
    });
    await expect(submitPairedEval(draft, requestIds, create)).resolves.toEqual({
      eval_id: "eval_2",
    });

    expect(create.mock.calls.map(([request]) => request.request_id)).toEqual([
      "eval-request-0001",
      "eval-request-0001",
      "eval-request-0002",
    ]);
    expect(generateRequestId).toHaveBeenCalledTimes(3);
  });

  it("rotates a request id after the server marks that creation terminal", async () => {
    const generateRequestId = vi
      .fn()
      .mockReturnValueOnce("eval-request-0001")
      .mockReturnValueOnce("eval-request-0002")
      .mockReturnValueOnce("eval-request-0003");
    const requestIds = createEvalRequestIds(generateRequestId);
    const create = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("creation is terminal", 409))
      .mockResolvedValueOnce({ eval_id: "eval_2" });
    const draft = {
      title: "Repair greeting",
      brief: "Apply the same repair to both arms.",
      model_mode: "scripted" as const,
      task_pack: "repo-maintainer" as const,
    };

    await expect(submitPairedEval(draft, requestIds, create)).rejects.toThrow(
      "creation is terminal",
    );
    await expect(submitPairedEval(draft, requestIds, create)).resolves.toEqual({
      eval_id: "eval_2",
    });

    expect(create.mock.calls.map(([request]) => request.request_id)).toEqual([
      "eval-request-0001",
      "eval-request-0002",
    ]);
  });

  it("reuses the persisted request and original draft after response loss and page refresh", async () => {
    const storage = memoryStorage();
    const generateRequestId = vi
      .fn()
      .mockReturnValueOnce("eval-request-0001")
      .mockReturnValueOnce("eval-request-0002")
      .mockReturnValueOnce("eval-request-0003");
    const originalDraft = {
      title: "Repair greeting",
      brief: "Apply the same repair to both arms.",
      model_mode: "scripted" as const,
      task_pack: "repo-maintainer" as const,
    };
    const rebuiltFormDraft = {
      ...originalDraft,
      title: "Default title restored by a fresh page",
    };
    const committed = new Map<string, { eval_id: string }>();
    const firstPost = vi.fn(async (request) => {
      committed.set(request.request_id, { eval_id: "eval_committed" });
      throw new Error("response lost after server commit");
    });

    const beforeRefresh = createEvalRequestIds(generateRequestId, storage);
    await expect(
      submitPairedEval(originalDraft, beforeRefresh, firstPost),
    ).rejects.toThrow("response lost after server commit");

    const afterRefresh = createEvalRequestIds(generateRequestId, storage);
    const replayPost = vi.fn(async (request) => committed.get(request.request_id));
    await expect(
      submitPairedEval(rebuiltFormDraft, afterRefresh, replayPost),
    ).resolves.toEqual({ eval_id: "eval_committed" });

    expect(firstPost.mock.calls[0][0]).toEqual({
      ...originalDraft,
      request_id: "eval-request-0001",
    });
    expect(replayPost.mock.calls[0][0]).toEqual(firstPost.mock.calls[0][0]);
    expect(afterRefresh.current()).toBe("eval-request-0002");

    const afterSuccess = createEvalRequestIds(generateRequestId, storage);
    expect(afterSuccess.current()).toBe("eval-request-0003");
    expect(generateRequestId).toHaveBeenCalledTimes(3);
  });
});
