import {
  ArchiveRestore,
  CheckCircle2,
  GitFork,
  HardDrive,
  LoaderCircle,
  Save,
  ShieldAlert,
  X,
} from "lucide-react";

import type { Checkpoint } from "../api/client";

interface CheckpointBandProps {
  runId: string;
  checkpoints: Checkpoint[];
  selected: Checkpoint | null;
  label: string;
  loading: boolean;
  busy: boolean;
  onLabelChange: (value: string) => void;
  onCreate: () => void;
  onSelect: (checkpointId: string) => void;
  onRestore: (checkpointId: string) => void;
  onClose: () => void;
}
function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function shortId(value: string): string {
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-5)}` : value;
}

export function CheckpointBand({
  runId,
  checkpoints,
  selected,
  label,
  loading,
  busy,
  onLabelChange,
  onCreate,
  onSelect,
  onRestore,
  onClose,
}: CheckpointBandProps) {
  const blockers = selected?.effects.restore_blockers ?? [];
  const restorable = Boolean(selected && blockers.length === 0);

  return (
    <section className="checkpoint-band" aria-label="检查点工作台 / Checkpoint workbench">
      <header className="checkpoint-header">
        <div className="checkpoint-title">
          <ArchiveRestore size={19} aria-hidden="true" />
          <div>
            <strong>检查点工作台 <span>/ Checkpoints</span></strong>
            <small>{shortId(runId)} · 保存事实边界，恢复时派生新 Run</small>
          </div>
        </div>
        <form
          className="checkpoint-create"
          onSubmit={(event) => {
            event.preventDefault();
            onCreate();
          }}
        >
          <input
            value={label}
            maxLength={200}
            onChange={(event) => onLabelChange(event.target.value)}
            placeholder="标签，例如：修改完成、测试前"
            aria-label="检查点标签"
          />
          <button className="icon-command checkpoint-save" disabled={busy} type="submit">
            {busy ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}
            <span>创建检查点</span>
          </button>
        </form>
        <button className="icon-only" onClick={onClose} title="关闭检查点工作台">
          <X size={17} />
        </button>
      </header>

      <div className="checkpoint-body">
        <nav className="checkpoint-list" aria-label="检查点列表">
          {loading && <div className="checkpoint-empty">正在读取持久化检查点…</div>}
          {!loading && checkpoints.length === 0 && (
            <div className="checkpoint-empty">当前 Run 还没有检查点。选择稳定边界后创建第一个。</div>
          )}
          {checkpoints.map((checkpoint, index) => {
            const blocked = checkpoint.effects.restore_blockers.length > 0;
            return (
              <button
                key={checkpoint.checkpoint_id}
                className={`checkpoint-item ${selected?.checkpoint_id === checkpoint.checkpoint_id ? "selected" : ""}`}
                onClick={() => onSelect(checkpoint.checkpoint_id)}
              >
                <span className="checkpoint-index">CP{index + 1}</span>
                <span className="checkpoint-item-copy">
                  <strong>{checkpoint.label || "未命名边界"}</strong>
                  <small>{checkpoint.source.phase} · {checkpoint.source.turn_id ?? "turn -"}</small>
                </span>
                {blocked ? <ShieldAlert size={16} /> : <CheckCircle2 size={16} />}
              </button>
            );
          })}
        </nav>

        {selected && (
          <div className="checkpoint-detail">
            <div className="checkpoint-detail-heading">
              <div>
                <span className={`checkpoint-state ${restorable ? "ready" : "blocked"}`}>
                  {restorable ? "可恢复 / Restorable" : "恢复受阻 / Blocked"}
                </span>
                <h3>{selected.label || "未命名检查点"}</h3>
                <code>{shortId(selected.checkpoint_id)}</code>
              </div>
              <button
                className="icon-command restore-command"
                disabled={busy || !restorable}
                onClick={() => onRestore(selected.checkpoint_id)}
                title="保留当前 Run，并从该边界创建一个新 Run"
              >
                <GitFork size={17} />
                <span>派生新 Run</span>
              </button>
            </div>

            <dl className="checkpoint-facts">
              <div><dt>来源阶段</dt><dd>{selected.source.phase}</dd></div>
              <div><dt>来源轮次</dt><dd>{selected.source.turn_id ?? "-"}</dd></div>
              <div><dt>工作区</dt><dd><HardDrive size={14} /> {selected.workspace.file_count} 文件 · {formatBytes(selected.workspace.total_bytes)}</dd></div>
              <div><dt>证据制品</dt><dd>{selected.state_refs.artifacts.length} 个引用</dd></div>
              <div><dt>副作用记录</dt><dd>{selected.effects.effects.length} 项</dd></div>
              <div><dt>上下文策略</dt><dd>从已验证事实重新规划</dd></div>
            </dl>

            {blockers.length > 0 ? (
              <div className="checkpoint-blockers">
                <strong><ShieldAlert size={15} /> 阻塞原因</strong>
                {blockers.map((blocker) => <code key={blocker}>{blocker}</code>)}
              </div>
            ) : (
              <p className="checkpoint-note">
                恢复会复制工作区和可信引用，不复制旧模型的隐藏推理或完整上下文；当前 Run 不会被覆盖。
              </p>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
