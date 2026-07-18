import { Cpu, Database, FastForward, Filter, ShieldCheck, Sparkles } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";

import type { EventRecord } from "../api/client";
import { eventMeta, selectVisibleEvents, shortId, type TrustBoundary } from "../lib/events";
import { boundaryLabel, toolLabel } from "../lib/i18n";

const BOUNDARY_ICON: Record<TrustBoundary, LucideIcon> = {
  proposal: Sparkles,
  control: ShieldCheck,
  fact: Database,
  runtime: Cpu,
};

interface TimelineProps {
  events: EventRecord[];
  selectedId: string | null;
  showAll: boolean;
  followLive: boolean;
  onShowAll: (value: boolean) => void;
  onSelect: (eventId: string) => void;
  onResumeLive: () => void;
}

export function Timeline({
  events,
  selectedId,
  showAll,
  followLive,
  onShowAll,
  onSelect,
  onResumeLive,
}: TimelineProps) {
  const listRef = useRef<HTMLDivElement>(null);
  const visible = useMemo(() => selectVisibleEvents(events, showAll), [events, showAll]);

  useEffect(() => {
    if (followLive) listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [followLive, visible.length]);

  return (
    <main className="timeline-panel">
      <div className="timeline-toolbar">
        <div>
          <span className="eyebrow">因果轨迹 / Causal trajectory</span>
          <h1>{events.length ? "实时事件时间线 / Live Event Timeline" : "常驻运行时 / Resident Runtime"}</h1>
        </div>
        <div className="timeline-controls">
          <div className="segmented" aria-label="时间线密度 / Timeline density">
            <button className={!showAll ? "active" : ""} onClick={() => onShowAll(false)}>
              <Filter size={14} aria-hidden="true" /> 聚焦
            </button>
            <button className={showAll ? "active" : ""} onClick={() => onShowAll(true)}>
              全部事实
            </button>
          </div>
          {!followLive && (
            <button className="icon-only" onClick={onResumeLive} title="恢复实时跟随 / Resume live follow">
              <FastForward size={17} aria-hidden="true" />
            </button>
          )}
        </div>
      </div>

      <div className="boundary-legend" aria-label="信任边界图例 / Trust boundary legend">
        <span className="proposal"><Sparkles size={12} />提议</span>
        <span className="control"><ShieldCheck size={12} />控制</span>
        <span className="fact"><Database size={12} />事实</span>
        <span className="runtime"><Cpu size={12} />运行时</span>
        <strong>{visible.length} / {events.length}</strong>
      </div>

      <div className="timeline-list" ref={listRef}>
        {visible.length === 0 ? (
          <TimelineEmpty />
        ) : visible.map((record) => {
          const type = record.event.type;
          const meta = eventMeta(type);
          const toolName = eventToolName(record);
          const Icon = BOUNDARY_ICON[meta.boundary];
          const eventId = record.event.id ?? String(record.cursor);
          const time = record.event.created_at
            ? new Date(record.event.created_at).toLocaleTimeString("zh-CN", { hour12: false })
            : "—";
          return (
            <button
              id={`event-${eventId}`}
              key={record.cursor}
              className={`timeline-row tone-${meta.tone} ${selectedId === eventId ? "selected" : ""}`}
              onClick={() => onSelect(eventId)}
            >
              <span className="cursor">#{String(record.cursor).padStart(3, "0")}</span>
              <span className={`timeline-node ${meta.boundary}`}><Icon size={14} /></span>
              <span className="timeline-copy">
                <strong>{toolName ? `${meta.label} · ${toolLabel(toolName)}` : meta.label}</strong>
                <span>
                  <code>{type}</code> · {record.event.source} · 任务 / AgentRun{" "}
                  <code title={record.event.task_id}>{shortId(record.event.task_id, 18)}</code>
                </span>
              </span>
              <span className={`boundary-tag ${meta.boundary}`} title={boundaryLabel(meta.boundary)}>{boundaryLabel(meta.boundary).split(" / ")[0]}</span>
              <span className="timeline-time">{time}<small>{shortId(eventId)}</small></span>
            </button>
          );
        })}
      </div>
    </main>
  );
}

function TimelineEmpty() {
  return (
    <div className="timeline-empty">
      <div className="empty-network" aria-hidden="true">
        <span /><span /><span /><i /><i />
      </div>
      <strong>尚未选择运行 / No run selected</strong>
      <span>常驻智能体已经就绪 / Runtime identities are ready.</span>
    </div>
  );
}

function eventToolName(record: EventRecord): string | null {
  const payload = record.event.payload as Record<string, unknown>;
  if (typeof payload.tool_name === "string") return payload.tool_name;
  const result = payload.result;
  if (result && typeof result === "object" && "name" in result) {
    const name = (result as Record<string, unknown>).name;
    return typeof name === "string" ? name : null;
  }
  return null;
}
