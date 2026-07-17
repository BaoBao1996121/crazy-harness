import { Bot, Cpu, Play, Users, X } from "lucide-react";
import { useState } from "react";

import type { TaskRequest } from "../api/client";

interface CreateRunDialogProps {
  open: boolean;
  busy: boolean;
  deepseekConfigured: boolean;
  onClose: () => void;
  onSubmit: (request: TaskRequest) => Promise<unknown>;
}

export function CreateRunDialog({
  open,
  busy,
  deepseekConfigured,
  onClose,
  onSubmit,
}: CreateRunDialogProps) {
  const [executionMode, setExecutionMode] = useState<TaskRequest["execution_mode"]>("single");
  const [modelMode, setModelMode] = useState<TaskRequest["model_mode"]>("scripted");
  const [title, setTitle] = useState("修复一次可回收仓库 / Repair a disposable repository");
  const [brief, setBrief] = useState(
    "定位实现缺陷，只修改允许的实现文件，运行真实测试并用差异证据证明结果。 / Find the implementation defect, edit only allowlisted files, run real tests, and prove the result with a diff.",
  );
  if (!open) return null;

  const chooseExecutionMode = (mode: TaskRequest["execution_mode"]) => {
    setExecutionMode(mode);
    if (mode === "single") {
      setTitle("修复一次可回收仓库 / Repair a disposable repository");
      setBrief("定位实现缺陷，只修改允许的实现文件，运行真实测试并用差异证据证明结果。 / Find the implementation defect, edit only allowlisted files, run real tests, and prove the result with a diff.");
      return;
    }
    setModelMode("scripted");
    setTitle("检查一次常驻协作 / Inspect resident teamwork");
    setBrief("收集证据、生成受控制品、进行一跳对账，并在独立审查后完成。 / Collect evidence, compose a bounded artifact, perform one peer check, and complete after independent review.");
  };

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="run-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-run-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-heading">
          <div>
          <span className="eyebrow">外部事件 / External event</span>
          <h2 id="new-run-title">启动常驻运行 / Start resident run</h2>
          </div>
          <button className="icon-only" onClick={onClose} title="关闭 / Close"><X size={18} /></button>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void onSubmit({
              title,
              brief,
              execution_mode: executionMode,
              model_mode: executionMode === "team" ? "scripted" : modelMode,
              task_pack: executionMode === "single" ? "repo-maintainer" : "resident-demo",
            }).then(onClose);
          }}
        >
          <fieldset className="run-mode-fieldset">
            <legend>执行模式 / Execution mode</legend>
            <div className="run-mode-choice">
              <button type="button" className={executionMode === "single" ? "active" : ""} onClick={() => chooseExecutionMode("single")}>
                <Bot size={17} aria-hidden="true" />
                <span><strong>单 Agent 真循环</strong><small>读代码、改文件、跑测试 / Real tool loop</small></span>
              </button>
              <button type="button" className={executionMode === "team" ? "active" : ""} onClick={() => chooseExecutionMode("team")}>
                <Users size={17} aria-hidden="true" />
                <span><strong>Agent Team 演示</strong><small>总控、侦察、构建、审查 / A2A story</small></span>
              </button>
            </div>
          </fieldset>
          <label>
            <span>任务标题 / Task title</span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={120} required />
          </label>
          <label>
            <span>委派说明 / Assignment brief</span>
            <textarea value={brief} onChange={(event) => setBrief(event.target.value)} rows={5} required />
          </label>
          <div className="model-choice">
            <button type="button" className={`model-option ${modelMode === "scripted" ? "selected" : ""}`} onClick={() => setModelMode("scripted")}>
              <span className="status-dot idle" />
              <div><strong>脚本模型 / Scripted provider</strong><span>确定性、可重放 / Deterministic, replayable</span></div>
            </button>
            <button
              type="button"
              className={`model-option ${modelMode === "deepseek" ? "selected" : ""}`}
              disabled={!deepseekConfigured || executionMode !== "single"}
              onClick={() => setModelMode("deepseek")}
              title={!deepseekConfigured ? "需要 DEEPSEEK_API_KEY" : executionMode !== "single" ? "Team 暂为确定性演示" : "DeepSeek V4 Flash"}
            >
              <Cpu size={15} aria-hidden="true" />
              <div><strong>DeepSeek V4 Flash</strong><span>{deepseekConfigured ? "真实模型已就绪 / Live model ready" : "尚未配置 API Key / API key not configured"}</span></div>
            </button>
          </div>
          <div className="dialog-actions">
            <button type="button" className="text-button" onClick={onClose}>取消</button>
            <button type="submit" className="icon-command primary" disabled={busy}>
              <Play size={16} fill="currentColor" />
              <span>{busy ? "启动中…" : "开始运行"}</span>
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
