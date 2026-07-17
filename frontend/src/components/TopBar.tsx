import {
  Activity,
  CirclePlus,
  Database,
  FlaskConical,
  Radio,
} from "lucide-react";

import type { Snapshot } from "../api/client";
import { statusLabel, streamLabel } from "../lib/i18n";

interface TopBarProps {
  snapshot: Snapshot | null;
  streamState: "connecting" | "live" | "reconnecting" | "offline";
  eventCount: number;
  busy: boolean;
  onNewRun: () => void;
  onChaos: () => void;
}

export function TopBar({
  snapshot,
  streamState,
  eventCount,
  busy,
  onNewRun,
  onChaos,
}: TopBarProps) {
  const run = snapshot?.run;
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
        <div className="run-strip-item desktop-only">
          <Database size={14} aria-hidden="true" />
          <span className="run-strip-label">事实 / Facts</span>
          <strong>{eventCount}</strong>
        </div>
        <div className="run-strip-item desktop-only">
          <span className="version-dot" aria-hidden="true" />
          <span className="run-strip-label">行为版本 / Behavior</span>
          <strong>{run?.behavior_version ?? "v0.1.0"}</strong>
        </div>
      </div>

      <div className="topbar-actions">
        <button className="icon-command secondary" onClick={onChaos} title="打开故障实验 / Open Chaos Lab">
          <FlaskConical size={17} aria-hidden="true" />
          <span>故障实验</span>
        </button>
        <button className="icon-command primary" onClick={onNewRun} disabled={busy}>
          <CirclePlus size={17} aria-hidden="true" />
          <span>新建运行</span>
        </button>
      </div>
    </header>
  );
}
