import {
  Bot,
  BrainCircuit,
  Check,
  CircleAlert,
  Clock3,
  Coins,
  Eye,
  MessagesSquare,
  Scale,
  ShieldCheck,
  Users,
  Wrench,
  X,
} from "lucide-react";

import type { PairedEvalArmReport, PairedEvalReport } from "../api/client";
import { fairnessChecks, recommendationPresentation } from "../lib/evals";
import { statusLabel } from "../lib/i18n";
import type { EvalArm } from "../hooks/usePairedEval";

interface EvalComparisonBandProps {
  evalId: string;
  report: PairedEvalReport | null;
  loading: boolean;
  selectedArm: EvalArm;
  onSelectArm: (arm: EvalArm) => void;
  onClose: () => void;
}

function formatDuration(value: number | undefined): string {
  if (value === undefined) return "—";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)} s`;
}

function formatCount(value: number | undefined): string {
  return value === undefined ? "—" : value.toLocaleString("zh-CN");
}

function formatCost(value: number | undefined): string {
  if (value === undefined) return "—";
  return `$${(value / 1_000_000).toFixed(6)}`;
}

function statusTone(status: string): string {
  if (status === "succeeded" || status === "completed") return "success";
  if (status === "failed" || status === "cancelled") return "danger";
  return "active";
}

function ArmSummary({
  arm,
  data,
  selected,
  onSelect,
}: {
  arm: EvalArm;
  data: PairedEvalArmReport;
  selected: boolean;
  onSelect: () => void;
}) {
  const Icon = arm === "single" ? Bot : Users;
  const trace = data.trace;
  const score = data.score;
  return (
    <article className={`eval-arm-summary ${selected ? "selected" : ""}`}>
      <div className="eval-arm-heading">
        <div className="eval-arm-identity">
          <Icon size={18} aria-hidden="true" />
          <div>
            <strong>{arm === "single" ? "单 Agent / Single" : "Agent 团队 / Team"}</strong>
            <code>{data.run_id}</code>
          </div>
        </div>
        <span className={`eval-run-state tone-${statusTone(data.status)}`}>{statusLabel(data.status)}</span>
      </div>
      <div className="eval-score-line">
        <span>机器评分 / Machine score</span>
        <strong>{score ? `${Math.round(score.score * 100)} / 100` : "等待评分 / Pending"}</strong>
        {score && <small className={score.passed ? "passed" : "failed"}>{score.passed ? "门禁通过" : "门禁未通过"}</small>}
      </div>
      <div className="eval-arm-metrics">
        <div><BrainCircuit size={14} /><span>模型 / Model</span><strong>{formatCount(trace?.model_requests)}</strong></div>
        <div><Wrench size={14} /><span>工具 / Tool</span><strong>{formatCount(trace?.tool_requests)}</strong></div>
        <div><MessagesSquare size={14} /><span>A2A</span><strong>{formatCount(trace?.a2a_requests)}</strong></div>
        <div><Clock3 size={14} /><span>耗时 / Duration</span><strong>{formatDuration(trace?.duration_ms)}</strong></div>
        <div className="wide"><Scale size={14} /><span>Token 已花费 / 已承诺</span><strong>{formatCount(trace?.spent_tokens)} / {formatCount(trace?.committed_tokens)}</strong></div>
        <div className="wide"><Coins size={14} /><span>费用已花费 / 已承诺</span><strong>{formatCost(trace?.spent_cost_microusd)} / {formatCost(trace?.committed_cost_microusd)}</strong></div>
      </div>
      <button className={`eval-arm-select ${selected ? "selected" : ""}`} onClick={onSelect}>
        <Eye size={15} aria-hidden="true" />
        <span>{selected ? "正在查看此臂时间线 / Viewing timeline" : "查看此臂时间线 / View timeline"}</span>
      </button>
    </article>
  );
}

export function EvalComparisonBand({
  evalId,
  report,
  loading,
  selectedArm,
  onSelectArm,
  onClose,
}: EvalComparisonBandProps) {
  if (!report) {
    return (
      <section className="eval-comparison-band eval-loading" aria-live="polite">
        <Scale size={20} aria-hidden="true" />
        <div><strong>正在恢复公平评测 / Restoring fair eval</strong><code>{evalId}</code></div>
        <span>{loading ? "读取持久报告中…" : "等待评测报告…"}</span>
        <button className="icon-only" onClick={onClose} title="关闭公平评测 / Close eval"><X size={16} /></button>
      </section>
    );
  }

  const checks = fairnessChecks(report);
  const recommendation = recommendationPresentation(report);
  return (
    <section className="eval-comparison-band" aria-label="公平评测比较 / Fair eval comparison">
      <div className="eval-band-heading">
        <div className="eval-title">
          <Scale size={20} aria-hidden="true" />
          <div>
            <span className="eyebrow">公平评测 / Fair eval</span>
            <strong>Single 与 Team 同条件对照</strong>
            <code>{report.eval_id}</code>
          </div>
        </div>
        <div className="fairness-badges" aria-label="公平性证明 / Fairness proofs">
          {checks.map((check) => (
            <span key={check.id} className={check.passed ? "passed" : "failed"}>
              {check.passed ? <ShieldCheck size={13} /> : <CircleAlert size={13} />}
              {check.label}
            </span>
          ))}
        </div>
        <button className="icon-only" onClick={onClose} title="关闭公平评测 / Close eval"><X size={16} /></button>
      </div>

      <div className="eval-comparison-body">
        <ArmSummary arm="single" data={report.single} selected={selectedArm === "single"} onSelect={() => onSelectArm("single")} />
        <div className={`eval-recommendation tone-${recommendation.tone}`}>
          {recommendation.tone === "positive" ? <Check size={18} /> : <CircleAlert size={18} />}
          <div>
            <span>编排建议 / Recommendation</span>
            <strong>{recommendation.title}</strong>
            <small>{recommendation.detail}</small>
            <code>{report.status === "completed" ? "评测完成 / Completed" : "评测运行中 / Running"}</code>
          </div>
        </div>
        <ArmSummary arm="team" data={report.team} selected={selectedArm === "team"} onSelect={() => onSelectArm("team")} />
      </div>
    </section>
  );
}
