import {
  Activity,
  CirclePause,
  GitBranch,
  GitFork,
  Hash,
  LoaderCircle,
  Play,
  Send,
  ShieldCheck,
  X,
} from "lucide-react";

import type { AgentRunBranch, AgentRunSession } from "../api/client";

interface AgentRunControlBandProps {
  runId: string;
  session: AgentRunSession | null;
  branch: AgentRunBranch | null;
  nudge: string;
  forkLabel: string;
  loading: boolean;
  busy: boolean;
  onNudgeChange: (value: string) => void;
  onForkLabelChange: (value: string) => void;
  onPause: () => void;
  onResume: () => void;
  onSendNudge: () => void;
  onFork: () => void;
  onSelectRun: (runId: string) => void;
  onClose: () => void;
}

const STATUS_LABELS: Record<AgentRunSession["status"], string> = {
  ready: "待启动 / Ready",
  running: "运行中 / Running",
  pausing: "等待安全边界 / Pausing",
  paused: "已暂停 / Paused",
  waiting: "等待事件 / Waiting",
  blocked: "受阻 / Blocked",
  cancelling: "正在取消 / Cancelling",
  cancelled: "已取消 / Cancelled",
  completed: "已完成 / Completed",
  failed: "失败 / Failed",
};

const PHASE_LABELS: Record<string, string> = {
  context_building: "组装上下文",
  model_calling: "调用模型",
  decision_validating: "校验决策",
  action_authorizing: "授权动作",
  action_executing: "执行动作",
  result_recording: "记录结果",
  waiting: "等待下一轮",
  submitted: "申请提交",
  failed: "执行失败",
};

function shortId(value: string | null | undefined): string {
  if (!value) return "-";
  return value.length > 22 ? `${value.slice(0, 12)}...${value.slice(-6)}` : value;
}

function phaseLabel(value: string | null | undefined): string {
  if (!value) return "尚未开始 / Not started";
  return `${PHASE_LABELS[value] ?? value} / ${value}`;
}

export function AgentRunControlBand({
  runId,
  session,
  branch,
  nudge,
  forkLabel,
  loading,
  busy,
  onNudgeChange,
  onForkLabelChange,
  onPause,
  onResume,
  onSendNudge,
  onFork,
  onSelectRun,
  onClose,
}: AgentRunControlBandProps) {
  const status = session?.status ?? "ready";
  const controlLocked = ["cancelling", "cancelled", "completed", "failed"].includes(status);
  const canPause = !controlLocked && !["paused", "pausing"].includes(status);
  const canResume = status === "paused" || status === "pausing";
  const forkSupported = session?.fork_supported ?? false;
  const forkReady = session?.fork_ready ?? false;
  const canFork = forkSupported && forkReady;
  const forkBlocker = session?.fork_blocker;

  return (
    <section className="agent-control-band" aria-label="Agent 运行控制 / AgentRun controls">
      <header className="agent-control-header">
        <div className="agent-control-title">
          <Activity size={19} aria-hidden="true" />
          <div>
            <strong>运行控制 <span>/ AgentRun Control</span></strong>
            <small>{shortId(runId)} · 命令和状态均写入持久事件</small>
          </div>
        </div>
        <div className={`agent-run-status status-${status}`}>
          {loading ? <LoaderCircle className="spin" size={15} /> : <ShieldCheck size={15} />}
          <span>{loading ? "读取中 / Loading" : STATUS_LABELS[status]}</span>
        </div>
        <div className="agent-control-primary-actions">
          {canResume ? (
            <button className="icon-command resume-command" onClick={onResume} disabled={busy} aria-label="继续运行 / Resume">
              {busy ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
              <span>继续运行</span>
            </button>
          ) : (
            <button className="icon-command pause-command" onClick={onPause} disabled={busy || !canPause} aria-label="安全暂停 / Pause safely">
              {busy ? <LoaderCircle className="spin" size={16} /> : <CirclePause size={16} />}
              <span>安全暂停</span>
            </button>
          )}
          <button className="icon-only" onClick={onClose} title="关闭运行控制">
            <X size={17} />
          </button>
        </div>
      </header>

      <div className="agent-control-body">
        <dl className="agent-session-facts">
          <div>
            <dt>当前阶段 / Phase</dt>
            <dd>{phaseLabel(session?.latest_phase)}</dd>
          </div>
          <div>
            <dt>已完成轮次 / Turns</dt>
            <dd>{session?.completed_turns ?? 0} 轮</dd>
          </div>
          <div>
            <dt>最后事实 / Last fact</dt>
            <dd>{session?.latest_event_type || "-"}</dd>
          </div>
          <div>
            <dt><Hash size={12} />能力清单 / Capability</dt>
            <dd title={session?.capability_manifest_hash ?? undefined}>
              {shortId(session?.capability_manifest_hash)}
            </dd>
          </div>
        </dl>

        <form
          className="agent-control-form nudge-form"
          onSubmit={(event) => {
            event.preventDefault();
            onSendNudge();
          }}
        >
          <label htmlFor="agent-nudge">下一轮纠偏 / Nudge</label>
          <div>
            <input
              id="agent-nudge"
              value={nudge}
              maxLength={2000}
              disabled={controlLocked}
              onChange={(event) => onNudgeChange(event.target.value)}
              placeholder="例如：先补齐失败测试的证据"
            />
            <button
              className="icon-only nudge-send"
              type="submit"
              disabled={busy || controlLocked || !nudge.trim()}
              title="替换下一轮纠偏内容"
            >
              <Send size={16} />
            </button>
          </div>
          <small>Latest-only：新内容会替换旧 Nudge，不会逐轮累加。</small>
        </form>

        <form
          className="agent-control-form fork-form"
          onSubmit={(event) => {
            event.preventDefault();
            onFork();
          }}
        >
          <label htmlFor="agent-fork-label">派生分支 / Fork</label>
          <div>
            <input
              id="agent-fork-label"
              value={forkLabel}
              maxLength={200}
              disabled={!canFork}
              onChange={(event) => onForkLabelChange(event.target.value)}
              placeholder="分支标签，例如：保守方案"
            />
            <button
              className="icon-only fork-send"
              type="submit"
              disabled={busy || !canFork}
              title={forkBlocker ?? "从当前验证边界派生新 Run"}
            >
              <GitFork size={16} />
            </button>
          </div>
          <small className={!canFork ? "fork-blocker" : undefined}>
            {forkBlocker ?? "复制工作区与可信引用，不复制模型隐藏上下文。"}
          </small>
        </form>

        <div className="agent-branch-lineage">
          <div className="branch-lineage-heading">
            <GitBranch size={15} />
            <strong>分支谱系 / Lineage</strong>
          </div>
          <div className="branch-relations">
            <span>父分支</span>
            {branch?.parent_run_id ? (
              <button type="button" onClick={() => onSelectRun(branch.parent_run_id!)} title="打开父 Run">
                {shortId(branch.parent_run_id)}
              </button>
            ) : <code>根 Run</code>}
            <span>子分支</span>
            {branch?.children_run_ids.length ? (
              <div className="branch-children">
                {branch.children_run_ids.map((childRunId) => (
                  <button key={childRunId} type="button" onClick={() => onSelectRun(childRunId)} title="打开子 Run">
                    {shortId(childRunId)}
                  </button>
                ))}
              </div>
            ) : <code>0</code>}
          </div>
        </div>
      </div>
    </section>
  );
}
