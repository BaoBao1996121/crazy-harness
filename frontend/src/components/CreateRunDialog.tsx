import { Bot, Cpu, Play, Search, Users, Wrench, X } from "lucide-react";
import { useState } from "react";

import type { TaskRequest } from "../api/client";
import {
  singleTaskPackDefaults,
  singleTaskPackOptions,
  taskPackForExecution,
  type SingleTaskPackId,
} from "../lib/taskpacks";

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
  const [taskPack, setTaskPack] = useState<SingleTaskPackId>("repo-maintainer");
  const [title, setTitle] = useState(() => singleTaskPackDefaults("repo-maintainer").title);
  const [brief, setBrief] = useState(() => singleTaskPackDefaults("repo-maintainer").brief);
  if (!open) return null;

  const chooseTaskPack = (id: SingleTaskPackId) => {
    const defaults = singleTaskPackDefaults(id);
    setTaskPack(id);
    setTitle(defaults.title);
    setBrief(defaults.brief);
  };

  const chooseExecutionMode = (mode: TaskRequest["execution_mode"]) => {
    setExecutionMode(mode);
    if (mode === "single") {
      chooseTaskPack(taskPack);
      return;
    }
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
              model_mode: modelMode,
              task_pack: taskPackForExecution(executionMode, taskPack),
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
                <span><strong>Agent Team 真协作</strong><small>总控、侦察、构建、审查 / Resident A2A</small></span>
              </button>
            </div>
          </fieldset>
          {executionMode === "single" && (
            <fieldset className="run-mode-fieldset">
              <legend>任务包 / TaskPack</legend>
              <div className="task-pack-choice">
                {singleTaskPackOptions().map((option) => {
                  const Icon = option.id === "repo-maintainer" ? Wrench : Search;
                  return (
                    <button
                      key={option.id}
                      type="button"
                      className={taskPack === option.id ? "active" : ""}
                      onClick={() => chooseTaskPack(option.id)}
                    >
                      <Icon size={17} aria-hidden="true" />
                      <span><strong>{option.label}</strong><small>{option.detail}</small></span>
                    </button>
                  );
                })}
              </div>
            </fieldset>
          )}
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
              disabled={!deepseekConfigured}
              onClick={() => setModelMode("deepseek")}
              title={!deepseekConfigured ? "需要 DEEPSEEK_API_KEY" : "DeepSeek V4 Flash"}
            >
              <Cpu size={15} aria-hidden="true" />
              <div><strong>DeepSeek V4 Flash</strong><span>{deepseekConfigured ? (executionMode === "team" ? "真实 Team + 持久预算 / Live Team governed" : "真实模型已就绪 / Live model ready") : "尚未配置 API Key / API key not configured"}</span></div>
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
