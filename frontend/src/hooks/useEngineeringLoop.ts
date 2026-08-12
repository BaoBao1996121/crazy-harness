import { useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  ApiError,
  type EngineeringLoopDraft,
  type EngineeringLoopReport,
  type EngineeringLoopRequest,
  type RuntimeView,
} from "../api/client";
import { mergeSearchParam, resolveIdentityParam } from "../lib/urlState";

const ACTIVE_KEY = "crazy.activeEngineeringLoop";
const PENDING_KEY = "crazy.pendingEngineeringLoop";
type StreamState = "connecting" | "live" | "reconnecting" | "offline";

function requestId(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${prefix}-${suffix}`;
}

export function resolveInitialEngineeringLoopId(
  search: string,
  storedLoop: string | null,
): string | undefined {
  return resolveIdentityParam(search, "loop", storedLoop);
}

function rememberedLoop(): string | undefined {
  return resolveInitialEngineeringLoopId(
    window.location.search,
    window.localStorage.getItem(ACTIVE_KEY),
  );
}

function replaceLoopParam(loopId: string | undefined): void {
  const search = mergeSearchParam(window.location.search, "loop", loopId);
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${search}${window.location.hash}`,
  );
}

function readPendingRequest(): EngineeringLoopRequest | null {
  const raw = window.localStorage.getItem(PENDING_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as EngineeringLoopRequest;
  } catch {
    window.localStorage.removeItem(PENDING_KEY);
    return null;
  }
}

function loopError(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) {
    return "找不到这次工程循环 / Engineering Loop not found";
  }
  const detail = error instanceof Error ? error.message : "unknown error";
  return `工程循环暂不可用 / Engineering Loop unavailable: ${detail}`;
}

export function useEngineeringLoop() {
  const [loopId, setLoopId] = useState<string | undefined>(rememberedLoop);
  const [report, setReport] = useState<EngineeringLoopReport | null>(null);
  const [loading, setLoading] = useState(Boolean(loopId));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<StreamState>(
    loopId ? "connecting" : "offline",
  );
  const [runtime, setRuntime] = useState<RuntimeView | null>(null);
  const refreshSequence = useRef(0);

  const activate = useCallback((nextLoopId: string) => {
    setLoopId(nextLoopId);
    setReport(null);
    setLoading(true);
    setStreamState("connecting");
    window.localStorage.setItem(ACTIVE_KEY, nextLoopId);
    replaceLoopParam(nextLoopId);
  }, []);

  const clearLoop = useCallback((message?: string) => {
    refreshSequence.current += 1;
    setLoopId(undefined);
    setReport(null);
    setLoading(false);
    setStreamState("offline");
    setRuntime(null);
    window.localStorage.removeItem(ACTIVE_KEY);
    replaceLoopParam(undefined);
    if (message) setNotice(message);
  }, []);

  const refresh = useCallback(async () => {
    if (!loopId) return;
    const sequence = ++refreshSequence.current;
    try {
      const next = await api.engineeringLoop(loopId);
      if (sequence !== refreshSequence.current) return;
      setReport(next);
      setLoading(false);
    } catch (error) {
      if (sequence !== refreshSequence.current) return;
      if (error instanceof ApiError && error.status === 404) {
        clearLoop(loopError(error));
        return;
      }
      setLoading(false);
      setNotice(loopError(error));
    }
  }, [clearLoop, loopId]);

  useEffect(() => {
    const pending = readPendingRequest();
    if (!pending) return;
    let disposed = false;
    setBusy(true);
    setNotice("正在确认上次创建请求 / Recovering pending Loop request");
    void api.createEngineeringLoop(pending).then((created) => {
      window.localStorage.removeItem(PENDING_KEY);
      if (!disposed && !loopId) activate(created.loop_id);
    }).catch((error) => {
      if (!disposed) setNotice(loopError(error));
    }).finally(() => {
      if (!disposed) setBusy(false);
    });
    return () => { disposed = true; };
  }, [activate, loopId]);

  useEffect(() => {
    if (!loopId) {
      setStreamState("offline");
      return;
    }
    let disposed = false;
    let timer: ReturnType<typeof window.setTimeout> | undefined;
    setStreamState("connecting");
    const source = new EventSource(
      `/api/events/stream?run_id=${encodeURIComponent(loopId)}&after=0`,
    );
    const refreshRuntime = async () => {
      try {
        const health = await api.health();
        if (!disposed) setRuntime(health.runtime);
      } catch {
        if (!disposed) setRuntime(null);
      }
    };
    const scheduleRefresh = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        if (!disposed) {
          void refresh();
          void refreshRuntime();
        }
      }, 150);
    };
    source.addEventListener("runtime", scheduleRefresh);
    source.onopen = () => {
      if (!disposed) setStreamState("live");
    };
    source.onerror = () => {
      if (!disposed) {
        setStreamState("reconnecting");
        setNotice("父循环事件流正在重连 / Loop event stream reconnecting");
      }
    };
    void refresh();
    void refreshRuntime();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
      source.close();
    };
  }, [loopId, refresh]);

  const createLoop = useCallback(async (draft: EngineeringLoopDraft): Promise<boolean> => {
    const request: EngineeringLoopRequest = {
      ...draft,
      request_id: requestId("loop-ui"),
    };
    window.localStorage.setItem(PENDING_KEY, JSON.stringify(request));
    setBusy(true);
    setNotice(null);
    try {
      const created = await api.createEngineeringLoop(request);
      window.localStorage.removeItem(PENDING_KEY);
      activate(created.loop_id);
      return true;
    } catch (error) {
      setNotice(loopError(error));
      return false;
    } finally {
      setBusy(false);
    }
  }, [activate]);

  const runAction = useCallback(async (
    action: () => Promise<EngineeringLoopReport>,
    successMessage: string,
  ) => {
    setBusy(true);
    setNotice(null);
    try {
      const next = await action();
      setReport(next);
      setNotice(successMessage);
    } catch (error) {
      setNotice(loopError(error));
    } finally {
      setBusy(false);
    }
  }, []);

  const advance = useCallback(async () => {
    if (!loopId) return;
    await runAction(
      async () => (await api.advanceEngineeringLoop(loopId)).report,
      "已推进一个父级阶段 / Advanced one parent phase",
    );
  }, [loopId, runAction]);

  const drain = useCallback(async () => {
    if (!loopId) return;
    await runAction(
      async () => (await api.drainEngineeringLoop(loopId)).report,
      "当前工程循环已运行到稳定边界 / Loop drained to a stable boundary",
    );
  }, [loopId, runAction]);

  const pause = useCallback(async () => {
    if (!loopId) return;
    await runAction(
      () => api.pauseEngineeringLoop(loopId, {
        request_id: requestId("loop-pause-ui"),
        reason: "用户从控制台暂停父循环 / Human paused the parent loop",
      }),
      "父循环已在安全边界暂停 / Parent loop paused at a safe boundary",
    );
  }, [loopId, runAction]);

  const resume = useCallback(async () => {
    if (!loopId) return;
    await runAction(
      () => api.resumeEngineeringLoop(loopId, {
        request_id: requestId("loop-resume-ui"),
        reason: "用户从控制台恢复父循环 / Human resumed the parent loop",
      }),
      "父循环已恢复 / Parent loop resumed",
    );
  }, [loopId, runAction]);

  const cancel = useCallback(async () => {
    if (!loopId) return;
    await runAction(
      () => api.cancelEngineeringLoop(loopId, {
        request_id: requestId("loop-cancel-ui"),
        reason: "用户从控制台取消父循环 / Human cancelled the parent loop",
      }),
      "父循环已取消 / Parent loop cancelled",
    );
  }, [loopId, runAction]);

  return {
    loopId,
    report,
    runtime,
    streamState,
    loading,
    busy,
    notice,
    setNotice,
    createLoop,
    clearLoop,
    advance,
    drain,
    pause,
    resume,
    cancel,
    refresh,
  };
}
