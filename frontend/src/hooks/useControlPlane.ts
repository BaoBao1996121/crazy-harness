import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, type EventRecord, type FaultPoint, type Snapshot, type TaskRequest } from "../api/client";

type StreamState = "connecting" | "live" | "reconnecting" | "offline";

interface StreamFrame {
  cursor: number;
  type: string;
  event: EventRecord["event"];
}

export function resolveInitialRunId(search: string, storedRun: string | null): string | undefined {
  const requestedRun = new URLSearchParams(search).get("run")?.trim();
  return requestedRun || storedRun?.trim() || undefined;
}

const rememberedRun = () => resolveInitialRunId(
  window.location.search,
  window.localStorage.getItem("crazy.activeRun"),
);

export function useControlPlane() {
  const [runId, setRunId] = useState<string | undefined>(rememberedRun);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [followLive, setFollowLive] = useState(true);
  const [streamState, setStreamState] = useState<StreamState>("connecting");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const cursorRef = useRef(0);
  const snapshotTimer = useRef<number | null>(null);

  const refreshSnapshot = useCallback(async (targetRun = runId) => {
    try {
      const next = await api.snapshot(targetRun);
      setSnapshot(next);
      if (next.run?.run_id) {
        window.localStorage.setItem("crazy.activeRun", next.run.run_id);
        if (!targetRun) {
          setRunId(next.run.run_id);
          window.history.replaceState(null, "", `?run=${encodeURIComponent(next.run.run_id)}`);
        }
      }
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "快照暂不可用 / Snapshot unavailable");
      if (targetRun) {
        window.localStorage.removeItem("crazy.activeRun");
        setRunId(undefined);
      }
    }
  }, [runId]);

  const queueSnapshot = useCallback((targetRun: string) => {
    if (snapshotTimer.current !== null) window.clearTimeout(snapshotTimer.current);
    snapshotTimer.current = window.setTimeout(() => void refreshSnapshot(targetRun), 180);
  }, [refreshSnapshot]);

  useEffect(() => {
    if (!runId) {
      void refreshSnapshot(undefined);
      setStreamState("offline");
      return;
    }
    let disposed = false;
    let source: EventSource | null = null;
    setStreamState("connecting");

    const connect = async () => {
      try {
        const page = await api.events(runId, 0);
        if (disposed) return;
        setEvents(page.items);
        cursorRef.current = page.next_cursor;
        if (page.items.length > 0) {
          setSelectedId((current) => current ?? page.items[page.items.length - 1].event.id ?? null);
        }
        await refreshSnapshot(runId);
        if (disposed) return;
        source = new EventSource(
          `/api/events/stream?run_id=${encodeURIComponent(runId)}&after=${cursorRef.current}`,
        );
        source.addEventListener("open", () => setStreamState("live"));
        source.addEventListener("runtime", (message) => {
          const frame = JSON.parse((message as MessageEvent<string>).data) as StreamFrame;
          cursorRef.current = Math.max(cursorRef.current, frame.cursor);
          const record: EventRecord = { cursor: frame.cursor, event: frame.event };
          setEvents((current) => {
            if (current.some((item) => item.cursor === record.cursor)) return current;
            return [...current, record];
          });
          if (followLive && frame.event.id) setSelectedId(frame.event.id);
          queueSnapshot(runId);
        });
        source.addEventListener("error", () => setStreamState("reconnecting"));
      } catch (error) {
        if (!disposed) {
          setStreamState("offline");
          setNotice(error instanceof Error ? error.message : "控制面暂不可用 / Control plane unavailable");
        }
      }
    };
    void connect();
    return () => {
      disposed = true;
      source?.close();
    };
  }, [followLive, queueSnapshot, refreshSnapshot, runId]);

  useEffect(() => () => {
    if (snapshotTimer.current !== null) window.clearTimeout(snapshotTimer.current);
  }, []);

  const createRun = useCallback(async (request: TaskRequest) => {
    setBusy(true);
    setNotice(null);
    try {
      const created = await api.createRun(request);
      cursorRef.current = 0;
      setEvents([]);
      setSelectedId(null);
      setFollowLive(true);
      setRunId(created.run_id);
      window.localStorage.setItem("crazy.activeRun", created.run_id);
      window.history.replaceState(null, "", `?run=${encodeURIComponent(created.run_id)}`);
      return created;
    } finally {
      setBusy(false);
    }
  }, []);

  const armFault = useCallback(async (point: FaultPoint) => {
    setBusy(true);
    try {
      const result = await api.armFault(point);
      setNotice(`一次性故障已装载 / One-shot fault armed: ${result.armed}`);
    } finally {
      setBusy(false);
    }
  }, []);

  const probeDepth = useCallback(async () => {
    if (!runId) return;
    setBusy(true);
    try {
      const result = await api.peerProbe({
        run_id: runId,
        sender: "scout",
        receiver: "reviewer",
        depth: 2,
      });
      setNotice(result.accepted ? "探测意外通过 / Probe unexpectedly passed" : `探测已拒绝 / Probe denied: ${result.reason}`);
      await refreshSnapshot(runId);
    } finally {
      setBusy(false);
    }
  }, [refreshSnapshot, runId]);

  const rebuildProjections = useCallback(async () => {
    setBusy(true);
    try {
      await api.rebuildProjections();
      if (runId) await refreshSnapshot(runId);
      setNotice("已从 SQLite 事件重建读取视图 / Read projections rebuilt from SQLite events");
    } finally {
      setBusy(false);
    }
  }, [refreshSnapshot, runId]);

  const selectEvent = useCallback((eventId: string) => {
    setSelectedId(eventId);
    setFollowLive(false);
  }, []);

  const resumeLive = useCallback(() => {
    setFollowLive(true);
    const latest = events[events.length - 1]?.event.id;
    if (latest) setSelectedId(latest);
  }, [events]);

  const selected = useMemo(
    () => events.find((record) => record.event.id === selectedId) ?? null,
    [events, selectedId],
  );

  return {
    runId,
    snapshot,
    events,
    selected,
    followLive,
    streamState,
    busy,
    notice,
    setNotice,
    createRun,
    armFault,
    probeDepth,
    rebuildProjections,
    selectEvent,
    resumeLive,
  };
}
