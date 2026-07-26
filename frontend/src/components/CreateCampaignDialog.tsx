import {
  Bot,
  Coins,
  Cpu,
  FlaskConical,
  Gauge,
  Layers3,
  Play,
  Scale,
  Users,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { EvalCampaignDraft } from "../api/client";
import { singleTaskPackDefaults } from "../lib/taskpacks";

interface CreateCampaignDialogProps {
  open: boolean;
  busy: boolean;
  deepseekConfigured: boolean;
  onClose: () => void;
  onSubmit: (request: EvalCampaignDraft) => Promise<boolean>;
}

const defaults = singleTaskPackDefaults("repo-maintainer");

export function nextDialogFocusIndex(
  currentIndex: number,
  focusableCount: number,
  backwards: boolean,
): number | undefined {
  if (focusableCount <= 0) return undefined;
  if (currentIndex < 0) return backwards ? focusableCount - 1 : 0;
  if (backwards && currentIndex === 0) return focusableCount - 1;
  if (!backwards && currentIndex === focusableCount - 1) return 0;
  return undefined;
}

export function CreateCampaignDialog({
  open,
  busy,
  deepseekConfigured,
  onClose,
  onSubmit,
}: CreateCampaignDialogProps) {
  const [title, setTitle] = useState("Single vs Team 多轮配对评测");
  const [brief, setBrief] = useState(defaults.brief);
  const [modelMode, setModelMode] = useState<EvalCampaignDraft["model_mode"]>("scripted");
  const [trialCount, setTrialCount] = useState(3);
  const [parallelPairs, setParallelPairs] = useState(1);
  const [maxTokens, setMaxTokens] = useState(250000);
  const [maxCost, setMaxCost] = useState("0.10");
  const [maxConcurrent, setMaxConcurrent] = useState(2);
  const [maxOutput, setMaxOutput] = useState(4096);
  const [maxRetries, setMaxRetries] = useState(2);
  const dialogRef = useRef<HTMLElement>(null);
  const titleInputRef = useRef<HTMLInputElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    const frame = window.requestAnimationFrame(() => titleInputRef.current?.focus());
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          "button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])",
        ) ?? [],
      );
      const currentIndex = focusable.indexOf(document.activeElement as HTMLElement);
      const nextIndex = nextDialogFocusIndex(
        currentIndex,
        focusable.length,
        event.shiftKey,
      );
      if (nextIndex === undefined) return;
      event.preventDefault();
      focusable[nextIndex]?.focus();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleKeyDown);
      previousFocus?.focus();
    };
  }, [open]);

  if (!open) return null;
  const campaignTokens = 2 * trialCount * maxTokens;
  const campaignCost = Math.max(0, 2 * trialCount * Number(maxCost || 0)).toFixed(6);

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        ref={dialogRef}
        className="run-dialog campaign-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-campaign-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">持久实验 / Persistent experiment</span>
            <h2 id="new-campaign-title">创建多轮评测 / Create Campaign</h2>
          </div>
          <button className="icon-only" onClick={onClose} title="关闭 / Close" aria-label="关闭 / Close"><X size={18} /></button>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void onSubmit({
              title,
              brief,
              model_mode: modelMode,
              task_pack: "repo-maintainer",
              trial_count: trialCount,
              max_parallel_pairs: parallelPairs,
              model_budget: {
                max_total_tokens: maxTokens,
                max_cost_usd: maxCost,
                max_concurrent_calls: maxConcurrent,
                max_output_tokens_per_call: maxOutput,
                max_retries_per_call: maxRetries,
              },
              campaign_max_total_tokens: campaignTokens,
              campaign_max_cost_usd: campaignCost,
            }).then((created) => {
              if (created) onClose();
            });
          }}
        >
          <div className="campaign-contract-preview">
            <span><Layers3 size={15} />{trialCount} 个 Trial</span>
            <span><Bot size={15} />Single</span>
            <Scale size={15} aria-hidden="true" />
            <span><Users size={15} />Team</span>
            <span><FlaskConical size={15} />配对统计</span>
          </div>
          <div className="campaign-form-grid">
            <label className="wide">
              <span>实验标题 / Campaign title</span>
              <input ref={titleInputRef} value={title} onChange={(event) => setTitle(event.target.value)} maxLength={120} required />
            </label>
            <label className="wide">
              <span>所有 Trial 共用的任务 / Shared assignment</span>
              <textarea value={brief} onChange={(event) => setBrief(event.target.value)} rows={3} maxLength={4000} required />
            </label>
            <label>
              <span>Trial 数量</span>
              <input
                type="number"
                min={1}
                max={30}
                value={trialCount}
                onChange={(event) => {
                  const value = event.currentTarget.valueAsNumber;
                  setTrialCount(value);
                  setParallelPairs((current) => Math.min(current, value));
                }}
                required
              />
            </label>
            <label>
              <span>Pair 并发窗口</span>
              <input type="number" min={1} max={Math.min(8, trialCount)} value={parallelPairs} onChange={(event) => setParallelPairs(event.currentTarget.valueAsNumber)} required />
            </label>
          </div>
          <fieldset className="run-mode-fieldset">
            <legend>模型证据 / Model evidence</legend>
            <div className="model-choice">
              <button type="button" aria-pressed={modelMode === "scripted"} className={`model-option ${modelMode === "scripted" ? "selected" : ""}`} onClick={() => setModelMode("scripted")}>
                <span className="status-dot idle" />
                <div><strong>脚本模型 / Scripted</strong><span>可否决退化，不用于晋升</span></div>
              </button>
              <button
                type="button"
                aria-pressed={modelMode === "deepseek"}
                className={`model-option ${modelMode === "deepseek" ? "selected" : ""}`}
                disabled={!deepseekConfigured}
                onClick={() => setModelMode("deepseek")}
                title={!deepseekConfigured ? "需要 DEEPSEEK_API_KEY" : "DeepSeek V4 Flash"}
              >
                <Cpu size={15} aria-hidden="true" />
                <div><strong>DeepSeek V4 Flash</strong><span>{deepseekConfigured ? "真实多次采样" : "尚未配置 API Key"}</span></div>
              </button>
            </div>
          </fieldset>
          <fieldset className="run-mode-fieldset">
            <legend>每臂预算 / Budget per arm</legend>
            <div className="eval-budget-grid">
              <label><span><Gauge size={13} />总 Token</span><input type="number" min={1} value={maxTokens} onChange={(event) => setMaxTokens(event.currentTarget.valueAsNumber)} required /></label>
              <label><span><Coins size={13} />费用 USD</span><input type="number" min="0.000001" step="any" value={maxCost} onChange={(event) => setMaxCost(event.target.value)} required /></label>
              <label><span>模型并发</span><input type="number" min={1} max={64} value={maxConcurrent} onChange={(event) => setMaxConcurrent(event.currentTarget.valueAsNumber)} required /></label>
              <label><span>单次输出</span><input type="number" min={1} value={maxOutput} onChange={(event) => setMaxOutput(event.currentTarget.valueAsNumber)} required /></label>
              <label><span>单次重试</span><input type="number" min={0} max={5} value={maxRetries} onChange={(event) => setMaxRetries(event.currentTarget.valueAsNumber)} required /></label>
            </div>
          </fieldset>
          <div className="campaign-budget-proof">
            <span>Campaign 最坏预算 / Worst-case envelope</span>
            <strong>{campaignTokens.toLocaleString("zh-CN")} Token · ${campaignCost}</strong>
            <code>2 × {trialCount} × 每臂上限</code>
          </div>
          <div className="dialog-actions">
            <button type="button" className="text-button" onClick={onClose}>取消</button>
            <button type="submit" className="icon-command primary" disabled={busy}>
              <Play size={16} fill="currentColor" />
              <span>{busy ? "创建中…" : "开始 Campaign"}</span>
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
