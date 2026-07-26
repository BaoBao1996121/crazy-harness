import {
  ArrowRight,
  BarChart3,
  Check,
  CircleAlert,
  CircleStop,
  Clock3,
  Coins,
  FlaskConical,
  Layers3,
  Scale,
  ShieldCheck,
  X,
} from "lucide-react";

import type {
  CampaignTrialSummary,
  EvalCampaignReport,
} from "../api/client";
import {
  campaignRecommendationReason,
  campaignProgress,
  formatCampaignMetric,
  trialStatusLabel,
} from "../lib/campaigns";

interface CampaignBandProps {
  campaignId: string;
  report: EvalCampaignReport | null;
  loading: boolean;
  busy: boolean;
  onOpenTrial: (evalId: string) => void;
  onCancel: () => void;
  onClose: () => void;
}

function recommendationText(report: EvalCampaignReport) {
  if (report.status === "cancelled") {
    return { tone: "warning", title: "已取消 / Cancelled" };
  }
  const outcome = report.recommendation?.outcome;
  if (outcome === "recommend_team") {
    return { tone: "positive", title: "建议 Team / Recommend Team" };
  }
  if (outcome === "keep_single") {
    return { tone: "warning", title: "保留 Single / Keep Single" };
  }
  return {
    tone: report.status === "completed" ? "neutral" : "active",
    title: report.status === "completed"
      ? "证据不足 / Insufficient evidence"
      : "正在收集配对证据 / Collecting evidence",
  };
}

function shortId(value: string): string {
  return value.length > 19 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value;
}

function quality(value: number | undefined): string {
  return value === undefined ? "—" : `${(value / 10_000).toFixed(0)}%`;
}

function duration(value: number | undefined): string {
  if (value === undefined) return "—";
  return value < 1000 ? `${value}ms` : `${(value / 1000).toFixed(1)}s`;
}

function TrialRow({
  trial,
  onOpen,
}: {
  trial: CampaignTrialSummary;
  onOpen: () => void;
}) {
  const openable = ["linked", "released", "observed"].includes(trial.status);
  return (
    <button
      className={`campaign-trial-row state-${trial.status}`}
      disabled={!openable}
      onClick={onOpen}
      title={openable ? "打开 Pair 证据 / Open Pair evidence" : trialStatusLabel(trial.status)}
    >
      <span className="campaign-trial-index">{String(trial.trial_index).padStart(2, "0")}</span>
      <span className="campaign-trial-state">
        {trial.evidence_valid === false
          ? <CircleAlert size={14} aria-hidden="true" />
          : trial.status === "observed"
            ? <Check size={14} aria-hidden="true" />
            : <span className="status-dot running" />}
        <strong>{trialStatusLabel(trial.status)}</strong>
        <code>{shortId(trial.eval_id)}</code>
      </span>
      <span className="campaign-trial-arm">
        <small>Single</small>
        <strong>{quality(trial.sample?.single_quality_ppm)}</strong>
        <em>{duration(trial.sample?.single_duration_ms)}</em>
      </span>
      <span className="campaign-trial-arm team">
        <small>Team</small>
        <strong>{quality(trial.sample?.team_quality_ppm)}</strong>
        <em>{duration(trial.sample?.team_duration_ms)}</em>
      </span>
      <ArrowRight size={15} className="campaign-trial-open" aria-hidden="true" />
    </button>
  );
}

export function CampaignBand({
  campaignId,
  report,
  loading,
  busy,
  onOpenTrial,
  onCancel,
  onClose,
}: CampaignBandProps) {
  if (!report) {
    return (
      <section className="campaign-band campaign-loading" aria-live="polite">
        <FlaskConical size={19} aria-hidden="true" />
        <div>
          <strong>正在恢复多轮评测 / Restoring Campaign</strong>
          <code>{campaignId}</code>
        </div>
        <span>{loading ? "读取持久实验事实中…" : "等待 Campaign 报告…"}</span>
        <button className="icon-only" onClick={onClose} title="关闭 Campaign"><X size={16} /></button>
      </section>
    );
  }

  const progress = campaignProgress(
    report.completed_trial_count,
    report.planned_trial_count,
  );
  const recommendation = recommendationText(report);
  const recommendationReason = campaignRecommendationReason(
    report.recommendation?.reason,
  ) ?? (
    report.status === "cancelled"
      ? "后续 Trial 已冻结，活动子 Run 已取消，审计事实保留"
      : "等待全部 Trial 进入可信终态"
  );
  const metrics = [
    {
      id: "success",
      icon: ShieldCheck,
      label: "成功率差 / Success Δ",
      value: formatCampaignMetric(report.aggregate?.success_rate_delta, "delta"),
    },
    {
      id: "quality",
      icon: BarChart3,
      label: "质量差 / Quality Δ",
      value: formatCampaignMetric(report.aggregate?.quality_delta, "delta"),
    },
    {
      id: "cost",
      icon: Coins,
      label: "成本比 / Cost ratio",
      value: formatCampaignMetric(report.aggregate?.cost_ratio, "ratio"),
    },
    {
      id: "duration",
      icon: Clock3,
      label: "耗时比 / Duration ratio",
      value: formatCampaignMetric(report.aggregate?.duration_ratio, "ratio"),
    },
  ];

  return (
    <section className="campaign-band" aria-label="多轮配对评测 / Eval Campaign">
      <div className="campaign-overview">
        <div className="campaign-title">
          <FlaskConical size={20} aria-hidden="true" />
          <div>
            <span className="eyebrow">多轮配对评测 / Eval Campaign</span>
            <strong>{report.contract.title}</strong>
            <code>{report.campaign_id}</code>
          </div>
        </div>
        <div className="campaign-progress" aria-label={`已完成 ${progress.completed} / ${progress.planned}`}>
          <span><Layers3 size={14} />Trial 进度</span>
          <strong>{progress.completed} / {progress.planned}</strong>
          <div><i style={{ width: `${progress.percent}%` }} /></div>
        </div>
        <div className={`campaign-recommendation tone-${recommendation.tone}`}>
          <Scale size={17} aria-hidden="true" />
          <div>
            <span>编排建议 / Recommendation</span>
            <strong>{recommendation.title}</strong>
            <small title={recommendationReason}>{recommendationReason}</small>
          </div>
        </div>
        <div className="campaign-band-actions">
          {report.status === "running" && (
            <button
              className="icon-command cancel-command campaign-cancel"
              onClick={onCancel}
              disabled={busy}
              title="取消实验及活动子 Run / Cancel campaign and active runs"
            >
              <CircleStop size={15} aria-hidden="true" />
              <span>取消实验 / Cancel</span>
            </button>
          )}
          <button className="icon-only" onClick={onClose} title="关闭 Campaign"><X size={16} /></button>
        </div>
      </div>

      <div className="campaign-metric-grid">
        {metrics.map(({ id, icon: Icon, label, value }) => (
          <div key={id} className="campaign-metric">
            <Icon size={15} aria-hidden="true" />
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>

      <div className="campaign-trials" aria-label="Trial 矩阵 / Trial matrix">
        <div className="campaign-trials-heading">
          <strong>Trial 矩阵 / Trial matrix</strong>
          <span>{report.contract.evidence_tier === "live_paired" ? "真实配对证据" : "确定性机制证据"}</span>
        </div>
        <div className="campaign-trial-list">
          {report.trials.map((trial) => (
            <TrialRow
              key={trial.trial_index}
              trial={trial}
              onOpen={() => onOpenTrial(trial.eval_id)}
            />
          ))}
        </div>
      </div>
    </section>
  );
}
