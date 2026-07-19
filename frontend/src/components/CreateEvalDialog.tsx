import { Bot, Coins, Cpu, Gauge, Play, Scale, ShieldCheck, Users, X } from "lucide-react";
import { useState } from "react";

import type { PairedEvalDraft } from "../api/client";
import { draftModelComparisonLabel } from "../lib/evals";
import { singleTaskPackDefaults } from "../lib/taskpacks";

interface CreateEvalDialogProps {
  open: boolean;
  busy: boolean;
  deepseekConfigured: boolean;
  onClose: () => void;
  onSubmit: (request: PairedEvalDraft) => Promise<boolean>;
}

const defaults = singleTaskPackDefaults("repo-maintainer");

export function CreateEvalDialog({
  open,
  busy,
  deepseekConfigured,
  onClose,
  onSubmit,
}: CreateEvalDialogProps) {
  const [title, setTitle] = useState(defaults.title);
  const [brief, setBrief] = useState(defaults.brief);
  const [modelMode, setModelMode] = useState<PairedEvalDraft["model_mode"]>("scripted");
  const [maxTokens, setMaxTokens] = useState(250000);
  const [maxCost, setMaxCost] = useState("0.10");
  const [maxConcurrent, setMaxConcurrent] = useState(2);
  const [maxOutput, setMaxOutput] = useState(4096);
  const [maxRetries, setMaxRetries] = useState(2);

  if (!open) return null;

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="run-dialog eval-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-eval-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">配对实验 / Paired experiment</span>
            <h2 id="new-eval-title">创建公平评测 / Create fair eval</h2>
          </div>
          <button className="icon-only" onClick={onClose} title="关闭 / Close"><X size={18} /></button>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void onSubmit({
              title,
              brief,
              model_mode: modelMode,
              task_pack: "repo-maintainer",
              model_budget: {
                max_total_tokens: maxTokens,
                max_cost_usd: maxCost,
                max_concurrent_calls: maxConcurrent,
                max_output_tokens_per_call: maxOutput,
                max_retries_per_call: maxRetries,
              },
            }).then((created) => {
              if (created) onClose();
            });
          }}
        >
          <div className="eval-pair-preview" aria-label="比较方式 / Comparison arms">
            <span><Bot size={16} />Single</span>
            <Scale size={17} aria-hidden="true" />
            <span><Users size={16} />Team</span>
          </div>
          <div className="fairness-preview">
            <span><ShieldCheck size={14} />同输入</span>
            <span><ShieldCheck size={14} />{draftModelComparisonLabel(modelMode)}</span>
            <span><ShieldCheck size={14} />同总预算</span>
            <span><ShieldCheck size={14} />隔离工作区</span>
          </div>
          <label>
            <span>任务标题 / Task title</span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={120} required />
          </label>
          <label>
            <span>同一任务说明 / Shared assignment brief</span>
            <textarea value={brief} onChange={(event) => setBrief(event.target.value)} rows={4} maxLength={4000} required />
          </label>
          <fieldset className="run-mode-fieldset">
            <legend>模型 / Model</legend>
            <div className="model-choice">
              <button type="button" className={`model-option ${modelMode === "scripted" ? "selected" : ""}`} onClick={() => setModelMode("scripted")}>
                <span className="status-dot idle" />
                <div><strong>脚本模型 / Scripted</strong><span>确定性机制证据 / Deterministic evidence</span></div>
              </button>
              <button
                type="button"
                className={`model-option ${modelMode === "deepseek" ? "selected" : ""}`}
                disabled={!deepseekConfigured}
                onClick={() => setModelMode("deepseek")}
                title={!deepseekConfigured ? "需要 DEEPSEEK_API_KEY" : "DeepSeek V4 Flash"}
              >
                <Cpu size={15} aria-hidden="true" />
                <div><strong>DeepSeek V4 Flash</strong><span>{deepseekConfigured ? "真实配对证据 / Live paired evidence" : "尚未配置 API Key"}</span></div>
              </button>
            </div>
          </fieldset>
          <fieldset className="run-mode-fieldset">
            <legend>每臂相同的总预算 / Identical budget per arm</legend>
            <div className="eval-budget-grid">
              <label><span><Gauge size={13} />总 Token</span><input type="number" min={1} value={maxTokens} onChange={(event) => setMaxTokens(event.currentTarget.valueAsNumber)} required /></label>
              <label><span><Coins size={13} />费用上限 USD</span><input type="number" min="0.000001" step="any" value={maxCost} onChange={(event) => setMaxCost(event.target.value)} required /></label>
              <label><span>并发调用</span><input type="number" min={1} max={64} value={maxConcurrent} onChange={(event) => setMaxConcurrent(event.currentTarget.valueAsNumber)} required /></label>
              <label><span>单次输出 Token</span><input type="number" min={1} value={maxOutput} onChange={(event) => setMaxOutput(event.currentTarget.valueAsNumber)} required /></label>
              <label><span>单次重试</span><input type="number" min={0} max={5} value={maxRetries} onChange={(event) => setMaxRetries(event.currentTarget.valueAsNumber)} required /></label>
            </div>
          </fieldset>
          <div className="dialog-actions">
            <button type="button" className="text-button" onClick={onClose}>取消</button>
            <button type="submit" className="icon-command primary" disabled={busy}>
              <Play size={16} fill="currentColor" />
              <span>{busy ? "创建中…" : "开始公平评测"}</span>
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
