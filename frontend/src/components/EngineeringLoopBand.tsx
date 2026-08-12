import {
  ArrowRight,
  Bot,
  Check,
  CircleAlert,
  CirclePause,
  CircleStop,
  FastForward,
  FlaskConical,
  Gauge,
  GitCommitHorizontal,
  LoaderCircle,
  Play,
  StepForward,
  Target,
  X,
} from "lucide-react";

import type { EngineeringLoopReport } from "../api/client";

interface EngineeringLoopBandProps {
  loopId: string;
  report: EngineeringLoopReport | null;
  loading: boolean;
  busy: boolean;
  onAdvance: () => void;
  onDrain: () => void;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onSelectRun: (runId: string) => void;
  onClose: () => void;
}

const STATUS_LABELS: Record<EngineeringLoopReport["status"], string> = {
  running: "迭代中 / Running",
  pausing: "等待父级边界 / Pausing",
  paused: "父循环已暂停 / Paused",
  resuming: "正在恢复 / Resuming",
  awaiting_approval: "等待人工批准 / Approval",
  completed: "达到目标 / Completed",
  blocked: "无法继续 / Blocked",
  cancelled: "已取消 / Cancelled",
};

const ITERATION_LABELS: Record<string, string> = {
  planned: "已规划 / Planned",
  candidate_proposed: "候选已提出 / Proposed",
  candidate_validated: "候选已校验 / Validated",
  candidate_rejected: "候选被拒 / Rejected",
  running: "子运行中 / Child running",
  completed: "子运行完成 / Child completed",
  failed: "子运行失败 / Child failed",
  evaluated: "已评测 / Evaluated",
  decided: "已决策 / Decided",
};

const DECISION_LABELS: Record<string, string> = {
  accept_continue: "接受并继续 / Accept + Continue",
  reject_continue: "拒绝并继续 / Reject + Continue",
  complete: "达到准出 / Complete",
  awaiting_approval: "等待批准 / Await Approval",
  blocked: "停止并保留缺口 / Blocked",
};

function shortId(value: string | null | undefined): string {
  if (!value) return "-";
  return value.length > 24 ? `${value.slice(0, 13)}...${value.slice(-7)}` : value;
}

function score(value: string | null | undefined): string {
  if (value === null || value === undefined) return "-";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toLocaleString("zh-CN", { maximumFractionDigits: 3 }) : value;
}

function iterationScore(
  report: EngineeringLoopReport,
  index: number,
): string {
  const evaluation = report.iterations[index]?.evaluation;
  if (!evaluation) return "-";
  return score(evaluation.metrics[report.contract.metric.name]);
}

export function EngineeringLoopBand({
  loopId,
  report,
  loading,
  busy,
  onAdvance,
  onDrain,
  onPause,
  onResume,
  onCancel,
  onSelectRun,
  onClose,
}: EngineeringLoopBandProps) {
  if (!report) {
    return (
      <section className="engineering-loop-band loop-loading" aria-live="polite">
        <LoaderCircle className="spin" size={19} aria-hidden="true" />
        <div>
          <strong>正在恢复工程循环 / Restoring Engineering Loop</strong>
          <code>{loopId}</code>
        </div>
        <span>{loading ? "读取持久事实中..." : "等待父循环报告..."}</span>
        <button className="icon-only" onClick={onClose} title="关闭工程循环"><X size={16} /></button>
      </section>
    );
  }

  const terminal = ["completed", "blocked", "cancelled"].includes(report.status);
  const canPause = ["running", "resuming"].includes(report.status);
  const canResume = ["paused", "pausing"].includes(report.status);
  const target = score(report.contract.metric.target);
  const current = score(report.active_score);
  const progress = Math.min(
    100,
    Math.round((report.iterations.length / report.contract.budget.max_iterations) * 100),
  );

  return (
    <section className="engineering-loop-band" aria-label="工程循环 / Engineering Loop">
      <header className="engineering-loop-header">
        <div className="engineering-loop-title">
          <FlaskConical size={20} aria-hidden="true" />
          <div>
            <span className="eyebrow">工程循环 / Engineering Loop</span>
            <strong>{report.contract.title}</strong>
            <code>{shortId(loopId)}</code>
          </div>
        </div>
        <div className={`engineering-loop-status status-${report.status}`}>
          {report.status === "completed" ? <Check size={15} /> : report.status === "blocked" ? <CircleAlert size={15} /> : <span className="status-dot running" />}
          <strong>{STATUS_LABELS[report.status]}</strong>
        </div>
        <div className="engineering-loop-actions">
          <button className="icon-command secondary" onClick={onAdvance} disabled={busy || report.status !== "running"} title="只推进一个父级阶段 / Advance one parent phase">
            <StepForward size={15} /><span>单步 / Step</span>
          </button>
          <button className="icon-command loop-drain" onClick={onDrain} disabled={busy || report.status !== "running"} title="只运行当前 Loop 及其 child / Drain this loop only">
            <FastForward size={15} /><span>运行到底 / Drain</span>
          </button>
          {canResume ? (
            <button className="icon-command resume-command" onClick={onResume} disabled={busy}>
              <Play size={15} /><span>继续循环 / Resume</span>
            </button>
          ) : (
            <button className="icon-command pause-command" onClick={onPause} disabled={busy || !canPause}>
              <CirclePause size={15} /><span>暂停父循环 / Pause</span>
            </button>
          )}
          {!terminal && (
            <button className="icon-only loop-cancel" onClick={onCancel} disabled={busy} title="取消父循环 / Cancel loop"><CircleStop size={16} /></button>
          )}
          <button className="icon-only" onClick={onClose} title="关闭工程循环"><X size={16} /></button>
        </div>
      </header>

      <div className="engineering-loop-summary">
        <div className="loop-objective">
          <Target size={16} />
          <div><span>目标 / Objective</span><strong>{report.contract.objective}</strong></div>
        </div>
        <div className="loop-score">
          <Gauge size={16} />
          <div><span>当前 / 目标</span><strong>{current} <em>/ {target}</em></strong></div>
        </div>
        <div className="loop-progress">
          <span>迭代 / Iterations</span>
          <strong>{report.iterations.length} / {report.contract.budget.max_iterations}</strong>
          <div><i style={{ width: `${progress}%` }} /></div>
        </div>
      </div>

      {(report.status === "paused" || report.status === "pausing") && (
        <div className="loop-boundary-note">
          <CirclePause size={14} />
          <span>父循环已暂停；已经发布的子运行可能仍在收尾 / Parent paused; an already released child may still finish.</span>
        </div>
      )}

      <div className="engineering-iteration-list">
        {report.iterations.length === 0 ? (
          <div className="loop-empty">尚未规划候选 / No candidate planned yet</div>
        ) : report.iterations.map((iteration, index) => {
          const gateValues = Object.values(iteration.evaluation?.hard_gates ?? {});
          const gatesPassed = gateValues.filter(Boolean).length;
          return (
            <article className={`engineering-iteration state-${iteration.status}`} key={iteration.identity.iteration_id}>
              <div className="iteration-index">
                <span>{String(iteration.identity.iteration).padStart(2, "0")}</span>
                <i />
              </div>
              <div className="iteration-story">
                <div className="iteration-heading">
                  <strong>Iteration {iteration.identity.iteration}</strong>
                  <span>{ITERATION_LABELS[iteration.status] ?? iteration.status}</span>
                </div>
                <div className="iteration-stages">
                  <div>
                    <GitCommitHorizontal size={14} />
                    <span>候选 / Candidate</span>
                    <strong>{iteration.candidate?.expected_effect ?? "等待候选"}</strong>
                    <code>{shortId(iteration.identity.candidate_id)}</code>
                  </div>
                  <ArrowRight size={14} className="iteration-arrow" />
                  <div>
                    <Bot size={14} />
                    <span>子运行 / Child Run</span>
                    <button type="button" onClick={() => onSelectRun(iteration.identity.child_run_id)} title="打开子运行 / Open child run">
                      {shortId(iteration.identity.child_run_id)}
                    </button>
                    <small>{iteration.outcome?.status ?? "等待执行"}</small>
                  </div>
                  <ArrowRight size={14} className="iteration-arrow" />
                  <div>
                    <Gauge size={14} />
                    <span>独立评测 / Independent Eval</span>
                    <strong>{iterationScore(report, index)}</strong>
                    <small>{gateValues.length ? `${gatesPassed}/${gateValues.length} gates` : "等待评测"}</small>
                  </div>
                  <ArrowRight size={14} className="iteration-arrow" />
                  <div>
                    <Check size={14} />
                    <span>决策 / Decision</span>
                    <strong>{iteration.decision ? DECISION_LABELS[iteration.decision.kind] : "等待决策"}</strong>
                    <small>{iteration.decision?.reason ?? iteration.failure_reason ?? "-"}</small>
                  </div>
                </div>
              </div>
            </article>
          );
        })}
      </div>
      {report.terminal_reason && <div className="loop-terminal-reason">{report.terminal_reason}</div>}
    </section>
  );
}
