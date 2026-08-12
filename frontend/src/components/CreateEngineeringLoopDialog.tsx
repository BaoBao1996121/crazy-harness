import { Cpu, FlaskConical, Gauge, Play, Target, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { EngineeringLoopDraft } from "../api/client";

interface CreateEngineeringLoopDialogProps {
  open: boolean;
  busy: boolean;
  deepseekConfigured: boolean;
  onClose: () => void;
  onSubmit: (request: EngineeringLoopDraft) => Promise<boolean>;
}

export function CreateEngineeringLoopDialog({
  open,
  busy,
  deepseekConfigured,
  onClose,
  onSubmit,
}: CreateEngineeringLoopDialogProps) {
  const [title, setTitle] = useState("仓库质量爬坡");
  const [objective, setObjective] = useState("先修复仓库行为，再清理临时质量标记");
  const [modelMode, setModelMode] = useState<EngineeringLoopDraft["model_mode"]>("scripted");
  const [maxIterations, setMaxIterations] = useState(3);
  const [maxNoProgress, setMaxNoProgress] = useState(2);
  const dialogRef = useRef<HTMLElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const frame = window.requestAnimationFrame(() => titleRef.current?.focus());
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", escape);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", escape);
      previous?.focus();
    };
  }, [onClose, open]);

  if (!open) return null;
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        ref={dialogRef}
        className="run-dialog engineering-loop-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-engineering-loop-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">持续改进 / Durable iteration</span>
            <h2 id="new-engineering-loop-title">创建工程循环 / Create Engineering Loop</h2>
          </div>
          <button className="icon-only" onClick={onClose} title="关闭 / Close"><X size={18} /></button>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void onSubmit({
              title,
              objective,
              exit_criteria: ["quality_score reaches 1"],
              loop_pack: "repo-quality",
              model_mode: modelMode,
              model_budget: {
                max_total_tokens: 250000,
                max_cost_usd: "0.10",
                max_concurrent_calls: 2,
                max_output_tokens_per_call: 4096,
                max_retries_per_call: 2,
              },
              budget: {
                max_iterations: maxIterations,
                max_no_progress_iterations: maxNoProgress,
              },
            }).then((created) => {
              if (created) onClose();
            });
          }}
        >
          <div className="loop-contract-preview">
            <span><FlaskConical size={15} />repo-quality</span>
            <span><Target size={15} />quality_score = 1</span>
            <span><Gauge size={15} />最多 {maxIterations} 轮</span>
          </div>
          <div className="campaign-form-grid">
            <label className="wide">
              <span>循环标题 / Loop title</span>
              <input ref={titleRef} value={title} maxLength={120} required onChange={(event) => setTitle(event.target.value)} />
            </label>
            <label className="wide">
              <span>工程目标 / Objective</span>
              <textarea value={objective} rows={3} maxLength={4000} required onChange={(event) => setObjective(event.target.value)} />
            </label>
            <label>
              <span>最大迭代 / Max iterations</span>
              <input type="number" min={1} max={100} value={maxIterations} required onChange={(event) => setMaxIterations(event.currentTarget.valueAsNumber)} />
            </label>
            <label>
              <span>无进展上限 / No-progress limit</span>
              <input type="number" min={0} max={100} value={maxNoProgress} required onChange={(event) => setMaxNoProgress(event.currentTarget.valueAsNumber)} />
            </label>
          </div>
          <fieldset className="run-mode-fieldset">
            <legend>子 Agent 模型 / Child model</legend>
            <div className="model-choice">
              <button type="button" aria-pressed={modelMode === "scripted"} className={`model-option ${modelMode === "scripted" ? "selected" : ""}`} onClick={() => setModelMode("scripted")}>
                <span className="status-dot idle" /><div><strong>脚本模型 / Scripted</strong><span>确定性学习演示</span></div>
              </button>
              <button type="button" aria-pressed={modelMode === "deepseek"} className={`model-option ${modelMode === "deepseek" ? "selected" : ""}`} disabled={!deepseekConfigured} onClick={() => setModelMode("deepseek")} title={!deepseekConfigured ? "需要 DEEPSEEK_API_KEY" : "DeepSeek V4 Flash"}>
                <Cpu size={15} /><div><strong>DeepSeek V4 Flash</strong><span>{deepseekConfigured ? "真实模型" : "尚未配置 API Key"}</span></div>
              </button>
            </div>
          </fieldset>
          <div className="dialog-actions">
            <button type="button" className="text-button" onClick={onClose}>取消</button>
            <button type="submit" className="icon-command primary" disabled={busy}>
              <Play size={16} fill="currentColor" /><span>{busy ? "创建中..." : "开始循环"}</span>
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
