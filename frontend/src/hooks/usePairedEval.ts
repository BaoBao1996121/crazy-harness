import { useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  ApiError,
  type PairedEvalDraft,
  type PairedEvalReport,
} from "../api/client";
import { createEvalRequestIds, submitPairedEval } from "../lib/evalRequests";
import { mergeSearchParam, restoreSearchParam } from "../lib/urlState";

const EVAL_STORAGE_KEY = "crazy.activeEval";

export function resolveInitialEvalId(search: string, storedEval: string | null): string | undefined {
  const requestedEval = new URLSearchParams(search).get("eval")?.trim();
  return requestedEval || storedEval?.trim() || undefined;
}

export function nextPollDelay(report: PairedEvalReport): number | undefined {
  return report.status === "completed" ? undefined : 900;
}

export type EvalArm = "single" | "team";

export function armForRun(report: PairedEvalReport, runId: string | undefined): EvalArm {
  return runId === report.team.run_id ? "team" : "single";
}

export function evalErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) {
    return "找不到这次公平评测，已清除失效链接 / Eval not found; stale link removed";
  }
  const detail = error instanceof Error ? error.message : "unknown error";
  return `公平评测暂不可用 / Eval unavailable: ${detail}`;
}

function rememberedEval(): string | undefined {
  return restoreSearchParam(
    window.location.search,
    "eval",
    EVAL_STORAGE_KEY,
    window.localStorage,
  );
}

function replaceBrowserParam(name: "eval" | "run", value: string | undefined): void {
  const search = mergeSearchParam(window.location.search, name, value);
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${search}${window.location.hash}`,
  );
}

interface UsePairedEvalOptions {
  activeRunId: string | undefined;
  onSelectRun: (runId: string) => void;
}

export function usePairedEval({ activeRunId, onSelectRun }: UsePairedEvalOptions) {
  const [evalId, setEvalId] = useState<string | undefined>(rememberedEval);
  const [report, setReport] = useState<PairedEvalReport | null>(null);
  const [selectedArm, setSelectedArm] = useState<EvalArm>("single");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(Boolean(evalId));
  const [notice, setNotice] = useState<string | null>(null);
  const [requestIds] = useState(() => createEvalRequestIds(undefined, window.localStorage));
  const initializedEval = useRef<string | null>(null);

  const forgetEval = useCallback((message?: string) => {
    initializedEval.current = null;
    setEvalId(undefined);
    setReport(null);
    setSelectedArm("single");
    setLoading(false);
    window.localStorage.removeItem(EVAL_STORAGE_KEY);
    replaceBrowserParam("eval", undefined);
    if (message) setNotice(message);
  }, []);

  useEffect(() => {
    if (!evalId) return;
    let disposed = false;
    let timer: ReturnType<typeof window.setTimeout> | undefined;

    const poll = async () => {
      try {
        const next = await api.evalPair(evalId);
        if (disposed) return;
        setReport(next);
        setLoading(false);
        const delay = nextPollDelay(next);
        if (delay !== undefined) timer = window.setTimeout(poll, delay);
      } catch (error) {
        if (disposed) return;
        setLoading(false);
        if (error instanceof ApiError && error.status === 404) {
          forgetEval(evalErrorMessage(error));
          return;
        }
        setNotice(evalErrorMessage(error));
        timer = window.setTimeout(poll, 1800);
      }
    };

    void poll();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [evalId, forgetEval]);

  useEffect(() => {
    if (!report || initializedEval.current === report.eval_id) return;
    const requestedRun = new URLSearchParams(window.location.search).get("run")?.trim();
    const initialArm = armForRun(report, requestedRun || activeRunId);
    const targetRun = report[initialArm].run_id;
    initializedEval.current = report.eval_id;
    setSelectedArm(initialArm);
    if (targetRun !== activeRunId) onSelectRun(targetRun);
  }, [activeRunId, onSelectRun, report]);

  const createEval = useCallback(async (request: PairedEvalDraft): Promise<boolean> => {
    setBusy(true);
    setNotice(null);
    try {
      const created = await submitPairedEval(request, requestIds, api.createEvalPair);
      initializedEval.current = created.eval_id;
      setEvalId(created.eval_id);
      setReport(null);
      setSelectedArm("single");
      setLoading(true);
      window.localStorage.setItem(EVAL_STORAGE_KEY, created.eval_id);
      replaceBrowserParam("eval", created.eval_id);
      onSelectRun(created.single_run_id);
      void api.drainEvalPair(created.eval_id).catch((error) => {
        setNotice(`公平评测执行失败 / Eval execution failed: ${evalErrorMessage(error)}`);
      });
      return true;
    } catch (error) {
      setNotice(evalErrorMessage(error));
      return false;
    } finally {
      setBusy(false);
    }
  }, [onSelectRun, requestIds]);

  const selectArm = useCallback((arm: EvalArm) => {
    if (!report) return;
    setSelectedArm(arm);
    onSelectRun(report[arm].run_id);
  }, [onSelectRun, report]);

  return {
    evalId,
    report,
    selectedArm,
    busy,
    loading,
    notice,
    setNotice,
    createEval,
    selectArm,
    clearEval: forgetEval,
  };
}
