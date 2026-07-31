import { useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  type AgentRunBranch,
  type AgentRunSession,
} from "../api/client";

interface UseAgentRunControlsOptions {
  runId: string | undefined;
  enabled: boolean;
  refreshToken: string | number | undefined;
  onSelectRun: (runId: string) => void;
}

function requestId(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${prefix}-${suffix}`;
}

export function useAgentRunControls({
  runId,
  enabled,
  refreshToken,
  onSelectRun,
}: UseAgentRunControlsOptions) {
  const [session, setSession] = useState<AgentRunSession | null>(null);
  const [branch, setBranch] = useState<AgentRunBranch | null>(null);
  const [nudge, setNudge] = useState("");
  const [forkLabel, setForkLabel] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const loadedRunRef = useRef<string | null>(null);
  const observedRefreshTokenRef = useRef(refreshToken);
  const sessionSequenceRef = useRef(0);
  const branchSequenceRef = useRef(0);

  const refreshSession = useCallback(async () => {
    if (!runId) return;
    const sequence = ++sessionSequenceRef.current;
    const initialLoad = loadedRunRef.current !== runId;
    if (initialLoad) setLoading(true);
    try {
      const nextSession = await api.agentRunSession(runId);
      if (sequence !== sessionSequenceRef.current) return;
      setSession(nextSession);
      loadedRunRef.current = runId;
    } catch (error) {
      if (sequence !== sessionSequenceRef.current) return;
      setNotice(error instanceof Error ? error.message : "运行控制暂时不可用 / Controls unavailable");
    } finally {
      if (sequence === sessionSequenceRef.current && initialLoad) setLoading(false);
    }
  }, [runId]);

  const refreshBranch = useCallback(async () => {
    if (!runId) return;
    const sequence = ++branchSequenceRef.current;
    try {
      const nextBranch = await api.agentRunBranch(runId);
      if (sequence === branchSequenceRef.current) setBranch(nextBranch);
    } catch (error) {
      if (sequence !== branchSequenceRef.current) return;
      setNotice(error instanceof Error ? error.message : "分支谱系暂时不可用 / Lineage unavailable");
    }
  }, [runId]);

  useEffect(() => {
    if (!enabled || !runId) {
      sessionSequenceRef.current += 1;
      branchSequenceRef.current += 1;
      loadedRunRef.current = null;
      observedRefreshTokenRef.current = refreshToken;
      setSession(null);
      setBranch(null);
      setLoading(false);
      return;
    }
    observedRefreshTokenRef.current = refreshToken;
    void refreshSession();
  }, [enabled, refreshSession, runId]);

  useEffect(() => {
    if (!enabled || !runId || Object.is(observedRefreshTokenRef.current, refreshToken)) {
      return;
    }
    observedRefreshTokenRef.current = refreshToken;
    const timer = window.setTimeout(() => {
      void refreshSession();
    }, 150);
    return () => window.clearTimeout(timer);
  }, [enabled, refreshSession, refreshToken, runId]);

  useEffect(() => {
    if (!enabled || !runId) return;
    void refreshBranch();
  }, [enabled, refreshBranch, runId]);

  const pause = useCallback(async () => {
    if (!runId) return;
    setBusy(true);
    setNotice(null);
    try {
      const result = await api.pauseAgentRun(runId, {
        request_id: requestId("pause-ui"),
        reason: "用户从控制台请求安全暂停 / Human requested a safe pause",
      });
      setNotice(result.status === "paused"
        ? "已在安全边界暂停 / Paused at a safe boundary"
        : "本轮结束后暂停 / Pausing after the active turn");
      await refreshSession();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "暂停失败 / Pause failed");
    } finally {
      setBusy(false);
    }
  }, [refreshSession, runId]);

  const resume = useCallback(async () => {
    if (!runId) return;
    setBusy(true);
    setNotice(null);
    try {
      await api.resumeAgentRun(runId, {
        request_id: requestId("resume-ui"),
        reason: "用户从控制台继续运行 / Human resumed the run",
      });
      setNotice("运行已恢复 / Run resumed");
      await refreshSession();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "恢复失败 / Resume failed");
    } finally {
      setBusy(false);
    }
  }, [refreshSession, runId]);

  const sendNudge = useCallback(async () => {
    const message = nudge.trim();
    if (!runId || !message) return;
    setBusy(true);
    setNotice(null);
    try {
      const result = await api.nudgeAgentRun(runId, {
        request_id: requestId("nudge-ui"),
        message,
      });
      setNudge("");
      setNotice(result.supersedes_event_id
        ? "新的 Nudge 已替换旧内容，将在下一轮生效"
        : "Nudge 已持久化，将在下一轮生效");
      await refreshSession();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Nudge 发送失败");
    } finally {
      setBusy(false);
    }
  }, [nudge, refreshSession, runId]);

  const fork = useCallback(async () => {
    if (!runId) return;
    setBusy(true);
    setNotice(null);
    try {
      const restored = await api.forkAgentRun(runId, {
        request_id: requestId("fork-ui"),
        label: forkLabel.trim(),
      });
      setForkLabel("");
      setNotice(`已派生分支 Run：${restored.run_id}`);
      onSelectRun(restored.run_id);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "派生分支失败 / Fork failed");
    } finally {
      setBusy(false);
    }
  }, [forkLabel, onSelectRun, runId]);

  return {
    session,
    branch,
    nudge,
    forkLabel,
    loading,
    busy,
    notice,
    setNudge,
    setForkLabel,
    setNotice,
    pause,
    resume,
    sendNudge,
    fork,
    refresh: refreshSession,
  };
}
