import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, type PairedEvalRequest } from "./client";

describe("paired eval API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("creates a paired eval without draining it in the create request", async () => {
    const created = {
      eval_id: "eval_demo",
      single_run_id: "run_single",
      team_run_id: "run_team",
      status: "queued" as const,
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(created), { status: 201, headers: { "Content-Type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body: PairedEvalRequest = {
      request_id: "eval-request-0001",
      title: "修复问候语",
      brief: "对同一个仓库任务进行公平对照。",
      model_mode: "scripted",
      task_pack: "repo-maintainer",
    };

    await expect(api.createEvalPair(body)).resolves.toEqual(created);
    expect(fetchMock).toHaveBeenCalledWith("/api/evals/pairs", expect.objectContaining({
      method: "POST",
      body: JSON.stringify(body),
    }));
  });

  it("keeps the HTTP status so an unknown eval can be recovered safely", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "paired eval not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    ));

    const error = await api.evalPair("eval_missing").catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, message: "paired eval not found" });
  });

  it("uses the generated list, get, and drain routes", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } }),
    ));
    vi.stubGlobal("fetch", fetchMock);

    await api.listEvalPairs();
    await api.evalPair("eval/with space");
    await api.drainEvalPair("eval/with space");

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/evals/pairs",
      "/api/evals/pairs/eval%2Fwith%20space",
      "/api/evals/pairs/eval%2Fwith%20space/drain",
    ]);
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: "POST" });
  });
});
