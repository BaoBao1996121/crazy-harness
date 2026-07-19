import {
  Activity,
  CircleStop,
  CirclePlus,
  Database,
  FlaskConical,
  Gauge,
  ListTodo,
  Radio,
  Scale,
  Waves,
} from "lucide-react";

import type { Snapshot } from "../api/client";
import { statusLabel, streamLabel } from "../lib/i18n";
import { normalizeScheduler, schedulerPressure } from "../lib/scheduler";

interface TopBarProps {
  snapshot: Snapshot | null;
  streamState: "connecting" | "live" | "reconnecting" | "offline";
  eventCount: number;
  busy: boolean;
  onNewRun: () => void;
  onNewEval: () => void;
  onCancel: () => void;
  onChaos: () => void;
}

export function TopBar({
  snapshot,
  streamState,
  eventCount,
  busy,
  onNewRun,
  onNewEval,
  onCancel,
  onChaos,
}: TopBarProps) {
  const run = snapshot?.run;
  const scheduler = normalizeScheduler(snapshot?.runtime.scheduler);
  const pressure = schedulerPressure(scheduler);
  const canCancel = Boolean(
    run && !["succeeded", "failed", "cancelled", "cancelling"].includes(run.status),
  );
  return (
    <header className="topbar">
      <div className="brand-lockup">
        <div className="brand-mark" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <div>
          <strong>Crazy A2A</strong>
          <span>常驻控制室 / Resident Control Room</span>
        </div>
      </div>

      <div className="run-strip" aria-label="运行时状态 / Runtime status">
        <div className={`live-indicator ${streamState}`}>
          <Radio size={14} aria-hidden="true" />
          <span>{streamLabel(streamState)}</span>
        </div>
        <div className="run-strip-item">
          <Activity size={14} aria-hidden="true" />
          <span className="run-strip-label">运行 / Run</span>
          <strong>{statusLabel(run?.status ?? "idle")}</strong>
        </div>
        <div className="run-strip-item" title="本调度实例执行数 / 容量 · Local active / capacity">
          <Gauge size={14} aria-hidden="true" />
          <span className="run-strip-label">本机活跃/容量 / Local Active/Capacity</span>
          <strong>{scheduler.active}/{scheduler.capacity}</strong>
        </div>
        <div className="run-strip-item" title="本调度实例等待准入的投递 / Locally queued deliveries">
          <ListTodo size={14} aria-hidden="true" />
          <span className="run-strip-label">本机排队 / Local Queued</span>
          <strong>{scheduler.queued}</strong>
        </div>
        <div className={`run-strip-item pressure-${pressure.kind}`} title={pressure.label}>
          <Waves size={14} aria-hidden="true" />
          <span className="run-strip-label">压力 / Pressure</span>
          <strong>{pressure.percent}%</strong>
        </div>
        <div className="run-strip-item wide-only">
          <Database size={14} aria-hidden="true" />
          <span className="run-strip-label">事实 / Facts</span>
          <strong>{eventCount}</strong>
        </div>
        <div className="run-strip-item wide-only">
          <span className="version-dot" aria-hidden="true" />
          <span className="run-strip-label">行为版本 / Behavior</span>
          <strong>{run?.behavior_version ?? "v0.1.0"}</strong>
        </div>
      </div>

      <div className="topbar-actions">
        {canCancel && (
          <button
            className="icon-command cancel-command"
            onClick={onCancel}
            disabled={busy}
            title="取消当前运行 / Cancel current run"
          >
            <CircleStop size={17} aria-hidden="true" />
            <span>取消 / Cancel</span>
          </button>
        )}
        <button className="icon-command secondary" onClick={onChaos} title="打开故障实验 / Open Chaos Lab">
          <FlaskConical size={17} aria-hidden="true" />
          <span>故障实验</span>
        </button>
        <button
          className="icon-command eval-command"
          onClick={onNewEval}
          disabled={busy}
          title="创建公平评测 / Create fair eval"
        >
          <Scale size={17} aria-hidden="true" />
          <span>公平评测 / Eval</span>
        </button>
        <button
          className="icon-command primary"
          onClick={onNewRun}
          disabled={busy}
          title="新建运行 / Create run"
        >
          <CirclePlus size={17} aria-hidden="true" />
          <span>新建运行</span>
        </button>
      </div>
    </header>
  );
}
