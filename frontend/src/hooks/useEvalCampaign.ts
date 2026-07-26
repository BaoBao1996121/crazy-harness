import { useCallback, useEffect, useState } from "react";

import {
  api,
  ApiError,
  type EvalCampaignCreated,
  type EvalCampaignDraft,
  type EvalCampaignReport,
} from "../api/client";
import {
  cancelEvalCampaignWithRetry,
  createCampaignCancelIntents,
  createCampaignRequestIds,
  recoverPendingEvalCampaign,
  submitEvalCampaign,
} from "../lib/campaignRequests";
import {
  mergeSearchParam,
  replaceIdentitySelection,
  resolveIdentityParam,
  restoreSearchParam,
  hasExplicitIdentity,
} from "../lib/urlState";

const CAMPAIGN_STORAGE_KEY = "crazy.activeCampaign";

export function resolveInitialCampaignId(
  search: string,
  storedCampaign: string | null,
): string | undefined {
  return resolveIdentityParam(search, "campaign", storedCampaign);
}

export function resolveStartupCampaignId(
  search: string,
  storedCampaign: string | null,
  hasPendingRequest: boolean,
): string | undefined {
  if (hasPendingRequest && !hasExplicitIdentity(search)) return undefined;
  return resolveInitialCampaignId(search, storedCampaign);
}

export function shouldRecoverPendingCampaign(
  search: string,
  campaignId: string | undefined,
  hasPendingRequest: boolean,
): boolean {
  return hasPendingRequest && !campaignId && !hasExplicitIdentity(search);
}

export function nextCampaignPollDelay(
  report: EvalCampaignReport,
): number | undefined {
  return report.status === "running" ? 900 : undefined;
}

function rememberedCampaign(): string | undefined {
  return restoreSearchParam(
    window.location.search,
    "campaign",
    CAMPAIGN_STORAGE_KEY,
    window.localStorage,
  );
}

function replaceCampaignParam(value: string | undefined): void {
  const search = mergeSearchParam(window.location.search, "campaign", value);
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${search}${window.location.hash}`,
  );
}

function replaceRecoveredCampaign(value: string): void {
  const search = replaceIdentitySelection(
    window.location.search,
    { campaign: value },
  );
  window.history.replaceState(
    null,
    "",
    `${window.location.pathname}${search}${window.location.hash}`,
  );
}

function campaignErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) {
    return "找不到这次 Campaign，已清除失效链接 / Campaign not found";
  }
  const detail = error instanceof Error ? error.message : "unknown error";
  return `多轮评测暂不可用 / Campaign unavailable: ${detail}`;
}

export function useEvalCampaign() {
  const [requestIds] = useState(() => (
    createCampaignRequestIds(undefined, window.localStorage)
  ));
  const [cancelIntents] = useState(() => (
    createCampaignCancelIntents(window.localStorage)
  ));
  const [campaignId, setCampaignId] = useState<string | undefined>(() => (
    resolveStartupCampaignId(
      window.location.search,
      rememberedCampaign() ?? null,
      Boolean(requestIds.pending()),
    )
  ));
  const [report, setReport] = useState<EvalCampaignReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(Boolean(campaignId));
  const [notice, setNotice] = useState<string | null>(null);

  const persistCampaignIdentity = useCallback((created: EvalCampaignCreated) => {
    window.localStorage.setItem(CAMPAIGN_STORAGE_KEY, created.campaign_id);
    replaceCampaignParam(created.campaign_id);
  }, []);

  const persistRecoveredCampaignIdentity = useCallback((created: EvalCampaignCreated) => {
    window.localStorage.setItem(CAMPAIGN_STORAGE_KEY, created.campaign_id);
    replaceRecoveredCampaign(created.campaign_id);
  }, []);

  const activateCampaign = useCallback((created: EvalCampaignCreated) => {
    setCampaignId(created.campaign_id);
    setReport(null);
    setLoading(true);
    void api.drainEvalCampaign(created.campaign_id).catch((error) => {
      setNotice(`Campaign 执行失败 / Execution failed: ${campaignErrorMessage(error)}`);
    });
  }, []);

  const clearCampaign = useCallback((message?: string) => {
    setCampaignId(undefined);
    setReport(null);
    setLoading(false);
    window.localStorage.removeItem(CAMPAIGN_STORAGE_KEY);
    replaceCampaignParam(undefined);
    if (message) setNotice(message);
  }, []);

  useEffect(() => {
    if (!requestIds.pending()) return;
    const activateRecovered = shouldRecoverPendingCampaign(
      window.location.search,
      campaignId,
      true,
    );
    let disposed = false;
    if (activateRecovered) {
      setBusy(true);
      setLoading(true);
      setNotice("正在确认上次 Campaign 请求 / Recovering pending Campaign request");
    }

    void recoverPendingEvalCampaign(
      requestIds,
      api.createEvalCampaign,
      activateRecovered ? persistRecoveredCampaignIdentity : undefined,
      {
        onRetry: (completedAttempts, maxAttempts, delayMs) => {
          if (!activateRecovered || disposed) return;
          setNotice(
            `创建请求仍在确认，${delayMs}ms 后重试 ${completedAttempts + 1}/${maxAttempts} / Campaign confirmation retry scheduled`,
          );
        },
      },
    ).then((created) => {
      if (!created || disposed) return;
      if (activateRecovered) {
        activateCampaign(created);
        setNotice("已恢复上次 Campaign / Pending Campaign recovered");
      }
    }).catch((error) => {
      if (disposed) return;
      if (activateRecovered) {
        setLoading(false);
        setNotice(
          `创建确认暂未取回，已保留原 request_id / Confirmation pending; request_id preserved: ${campaignErrorMessage(error)}`,
        );
      }
    }).finally(() => {
      if (!disposed && activateRecovered) setBusy(false);
    });

    return () => {
      disposed = true;
    };
  }, [activateCampaign, campaignId, persistRecoveredCampaignIdentity, requestIds]);

  useEffect(() => {
    const pendingCampaignId = cancelIntents.pending();
    if (!pendingCampaignId) return;
    let disposed = false;

    void cancelEvalCampaignWithRetry(
      pendingCampaignId,
      api.cancelEvalCampaign,
    ).then((cancelled) => {
      cancelIntents.clear(pendingCampaignId);
      if (disposed || campaignId !== pendingCampaignId) return;
      setReport(cancelled);
      setLoading(false);
      setNotice("已恢复并确认取消意图 / Pending cancellation confirmed");
    }).catch((error) => {
      if (error instanceof ApiError && error.status === 404) {
        cancelIntents.clear(pendingCampaignId);
      }
      if (disposed || campaignId !== pendingCampaignId) return;
      setNotice(
        `取消意图仍待确认，将在刷新后继续 / Cancellation remains pending: ${campaignErrorMessage(error)}`,
      );
    });

    return () => {
      disposed = true;
    };
  }, [campaignId, cancelIntents]);

  useEffect(() => {
    if (!campaignId) return;
    let disposed = false;
    let timer: ReturnType<typeof window.setTimeout> | undefined;

    const poll = async () => {
      try {
        const next = await api.evalCampaign(campaignId);
        if (disposed) return;
        setReport(next);
        setLoading(false);
        const delay = nextCampaignPollDelay(next);
        if (delay !== undefined) timer = window.setTimeout(poll, delay);
      } catch (error) {
        if (disposed) return;
        setLoading(false);
        if (error instanceof ApiError && error.status === 404) {
          clearCampaign(campaignErrorMessage(error));
          return;
        }
        setNotice(campaignErrorMessage(error));
        timer = window.setTimeout(poll, 1800);
      }
    };

    void poll();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [campaignId, clearCampaign]);

  const createCampaign = useCallback(async (
    draft: EvalCampaignDraft,
  ): Promise<boolean> => {
    setBusy(true);
    setNotice(null);
    try {
      const created = await submitEvalCampaign(
        draft,
        requestIds,
        api.createEvalCampaign,
        persistCampaignIdentity,
      );
      activateCampaign(created);
      return true;
    } catch (error) {
      setNotice(campaignErrorMessage(error));
      return false;
    } finally {
      setBusy(false);
    }
  }, [activateCampaign, persistCampaignIdentity, requestIds]);

  const cancelCampaign = useCallback(async (): Promise<void> => {
    if (!campaignId) return;
    cancelIntents.remember(campaignId);
    setBusy(true);
    setNotice(null);
    try {
      const cancelled = await cancelEvalCampaignWithRetry(
        campaignId,
        api.cancelEvalCampaign,
        {
          onRetry: (completedAttempts, maxAttempts, delayMs) => {
            setNotice(
              `取消意图已保留，推进任务占用中；${delayMs}ms 后重试 ${completedAttempts + 1}/${maxAttempts} / Cancellation intent retained; retrying after claim conflict`,
            );
          },
        },
      );
      cancelIntents.clear(campaignId);
      setReport(cancelled);
      setLoading(false);
      setNotice(
        "Campaign 已取消，活动子 Run 已停止 / Campaign and active runs cancelled",
      );
    } catch (error) {
      setNotice(
        `取消尚未确认，可再次尝试 / Cancellation not confirmed; retry available: ${campaignErrorMessage(error)}`,
      );
    } finally {
      setBusy(false);
    }
  }, [campaignId, cancelIntents]);

  return {
    campaignId,
    report,
    busy,
    loading,
    notice,
    setNotice,
    createCampaign,
    cancelCampaign,
    clearCampaign,
  };
}
