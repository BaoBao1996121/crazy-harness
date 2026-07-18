import { afterEach, describe, expect, it, vi } from "vitest";

import { createAsyncThrottle } from "./throttle";

afterEach(() => vi.useRealTimers());

describe("async throttle", () => {
  it("flushes during a continuous burst instead of waiting for silence", async () => {
    vi.useFakeTimers();
    const refresh = vi.fn(async (_runId: string) => undefined);
    const throttle = createAsyncThrottle(refresh, 180);

    throttle.schedule("run-1");
    await vi.advanceTimersByTimeAsync(100);
    throttle.schedule("run-2");
    await vi.advanceTimersByTimeAsync(80);

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenLastCalledWith("run-2");
    throttle.cancel();
  });
});
