import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type Checkpoint } from "../api/client";

interface UseCheckpointsOptions {
  runId: string | undefined;
  enabled: boolean;
  onSelectRun: (runId: string) => void;
}
function requestId(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
  return `${prefix}-${suffix}`;
}

export function useCheckpoints({ runId, enabled, onSelectRun }: UseCheckpointsOptions) {
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!runId) return;
    setLoading(true);
    try {
      const next = await api.listCheckpoints(runId);
      setCheckpoints(next);
      setSelectedId((current) => (
        current && next.some((item) => item.checkpoint_id === current)
          ? current
          : next.at(-1)?.checkpoint_id ?? null
      ));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "检查点暂时不可用 / Checkpoints unavailable");
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    setCheckpoints([]);
    setSelectedId(null);
    setLabel("");
    if (enabled && runId) void refresh();
  }, [enabled, refresh, runId]);

  const createCheckpoint = useCallback(async () => {
    if (!runId) return;
    setBusy(true);
    setNotice(null);
    try {
      const created = await api.createCheckpoint(runId, {
        request_id: requestId("checkpoint-ui"),
        label: label.trim(),
      });
      setCheckpoints((current) => [
        ...current.filter((item) => item.checkpoint_id !== created.checkpoint_id),
        created,
      ]);
      setSelectedId(created.checkpoint_id);
      setLabel("");
      setNotice("检查点已持久化 / Checkpoint committed");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "创建检查点失败 / Checkpoint failed");
    } finally {
      setBusy(false);
    }
  }, [label, runId]);

  const restoreCheckpoint = useCallback(async (checkpointId: string) => {
    setBusy(true);
    setNotice(null);
    try {
      const restored = await api.restoreCheckpoint(checkpointId, {
        request_id: requestId("restore-ui"),
      });
      setNotice(`已派生恢复 Run：${restored.run_id}`);
      onSelectRun(restored.run_id);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "恢复失败 / Restore failed");
    } finally {
      setBusy(false);
    }
  }, [onSelectRun]);

  const selected = useMemo(
    () => checkpoints.find((item) => item.checkpoint_id === selectedId) ?? null,
    [checkpoints, selectedId],
  );

  return {
    checkpoints,
    selected,
    label,
    loading,
    busy,
    notice,
    setLabel,
    setNotice,
    selectCheckpoint: setSelectedId,
    createCheckpoint,
    restoreCheckpoint,
    refresh,
  };
}
