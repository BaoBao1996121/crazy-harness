import {
  BookOpen,
  BrainCircuit,
  Bug,
  ChevronRight,
  CircleAlert,
  Database,
  FileJson,
  GitCompareArrows,
  Layers3,
  Network,
  RefreshCw,
  Search,
  ShieldCheck,
  TestTubeDiagonal,
  Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { EventRecord, FaultPoint, Snapshot } from "../api/client";
import { capabilityIdentityEntries, capabilityRecallEntries } from "../lib/capabilities";
import { eventMeta, shortId } from "../lib/events";
import { skillViewFromEvents } from "../lib/skills";
import { agentLabel, boundaryLabel, contentLabel, statusLabel } from "../lib/i18n";

export type InspectorTab = "event" | "context" | "a2a" | "memory" | "evolution" | "chaos";

const TABS: { id: InspectorTab; label: string; title: string; icon: LucideIcon }[] = [
  { id: "event", label: "事件", title: "事件 / Event", icon: FileJson },
  { id: "context", label: "上下文", title: "上下文 / Context", icon: Layers3 },
  { id: "a2a", label: "协作", title: "智能体协作 / A2A", icon: Network },
  { id: "memory", label: "记忆", title: "记忆 / Memory", icon: BrainCircuit },
  { id: "evolution", label: "进化", title: "进化 / Evolution", icon: GitCompareArrows },
  { id: "chaos", label: "故障", title: "故障实验 / Chaos", icon: TestTubeDiagonal },
];

interface InspectorPanelProps {
  activeTab: InspectorTab;
  selected: EventRecord | null;
  events: EventRecord[];
  snapshot: Snapshot | null;
  busy: boolean;
  onTabChange: (tab: InspectorTab) => void;
  onSelectEvent: (eventId: string) => void;
  onArmFault: (point: FaultPoint) => Promise<void>;
  onProbeDepth: () => Promise<void>;
  onRebuild: () => Promise<void>;
}

export function InspectorPanel(props: InspectorPanelProps) {
  return (
    <aside className="inspector-panel">
      <div className="inspector-tabs" role="tablist" aria-label="控制室视图 / Control room views">
        {TABS.map(({ id, label, title, icon: Icon }) => (
          <button
            key={id}
            className={props.activeTab === id ? "active" : ""}
            onClick={() => props.onTabChange(id)}
            role="tab"
            aria-selected={props.activeTab === id}
            title={title}
          >
            <Icon size={15} aria-hidden="true" />
            <span>{label}</span>
          </button>
        ))}
      </div>
      <div className="inspector-content">
        {props.activeTab === "event" && (
          <EventInspector
            selected={props.selected}
            events={props.events}
            onSelectEvent={props.onSelectEvent}
          />
        )}
        {props.activeTab === "context" && <ContextInspector snapshot={props.snapshot} events={props.events} />}
        {props.activeTab === "a2a" && <A2AInspector events={props.events} />}
        {props.activeTab === "memory" && <MemoryInspector snapshot={props.snapshot} events={props.events} />}
        {props.activeTab === "evolution" && <EvolutionInspector snapshot={props.snapshot} />}
        {props.activeTab === "chaos" && (
          <ChaosInspector
            busy={props.busy}
            hasRun={Boolean(props.snapshot?.run)}
            onArmFault={props.onArmFault}
            onProbeDepth={props.onProbeDepth}
            onRebuild={props.onRebuild}
          />
        )}
      </div>
    </aside>
  );
}

function PanelHeading({ eyebrow, title, value }: { eyebrow: string; title: string; value?: string | number }) {
  return (
    <div className="panel-heading">
      <div><span className="eyebrow">{eyebrow}</span><h2>{title}</h2></div>
      {value !== undefined && <span className="panel-value">{value}</span>}
    </div>
  );
}

function EventInspector({
  selected,
  events,
  onSelectEvent,
}: {
  selected: EventRecord | null;
  events: EventRecord[];
  onSelectEvent: (eventId: string) => void;
}) {
  if (!selected) {
    return <InspectorEmpty icon={FileJson} title="请选择一个事件 / Select an event" detail="这里将显示已持久化的数据 / The durable payload will appear here." />;
  }
  const event = selected.event;
  const meta = eventMeta(event.type);
  const parent = events.find((record) => record.event.id === event.causation_id);
  const boundaryCopy = {
    proposal: "模型或服务提出的建议，尚无执行权 / Model or service proposal; no execution authority yet.",
    control: "Harness 作出的确定性策略或门禁决定 / Deterministic Harness policy or gate decision.",
    fact: "已经持久化，后续轮次可以作为证据使用 / Persisted outcome usable as later evidence.",
    runtime: "调度、恢复或上下文管理产生的运行观察 / Scheduler, recovery, or context observation.",
  }[meta.boundary];
  return (
    <>
      <PanelHeading eyebrow={`游标 / Cursor #${selected.cursor}`} title={meta.label} />
      <div className={`trust-boundary ${meta.boundary}`}>
        <span>{boundaryLabel(meta.boundary)}</span>
        <p>{boundaryCopy}</p>
      </div>
      <dl className="event-fields">
        <div><dt>事件类型 / Type</dt><dd><code>{event.type}</code></dd></div>
        <div><dt>来源 / Source</dt><dd>{event.source}</dd></div>
        <div><dt>事件 ID</dt><dd title={event.id}><code>{shortId(event.id, 16)}</code></dd></div>
        <div><dt>运行 ID / Run</dt><dd title={event.run_id}><code>{shortId(event.run_id, 16)}</code></dd></div>
        <div><dt>任务或 AgentRun / Task</dt><dd title={event.task_id}><code>{shortId(event.task_id, 20)}</code></dd></div>
        <div><dt>创建时间 / Created</dt><dd>{event.created_at ? new Date(event.created_at).toLocaleString("zh-CN", { hour12: false }) : "—"}</dd></div>
      </dl>
      {event.causation_id && (
        <button
          className="causation-link"
          disabled={!parent?.event.id}
          onClick={() => {
            if (!parent?.event.id) return;
            onSelectEvent(parent.event.id);
            document.getElementById(`event-${parent.event.id}`)?.scrollIntoView({ block: "center" });
          }}
        >
          <span><small>由此事件触发 / Caused by</small><code>{shortId(event.causation_id, 18)}</code></span>
          <ChevronRight size={16} />
        </button>
      )}
      <section className="payload-section">
        <div className="section-label"><span>持久化数据 / Durable payload</span><span>{Object.keys(event.payload ?? {}).length} 个字段 / fields</span></div>
        <pre>{JSON.stringify(event.payload ?? {}, null, 2)}</pre>
      </section>
    </>
  );
}

function ContextInspector({ snapshot, events }: { snapshot: Snapshot | null; events: EventRecord[] }) {
  const contexts = snapshot?.contexts ?? [];
  const capabilities = snapshot?.capability_manifests ?? [];
  const skillState = skillViewFromEvents(events);
  const activeByName = new Map(skillState.active.map((item) => [item.name, item]));
  return (
    <>
      <PanelHeading eyebrow="每轮重新编译 / Compiled each turn" title="上下文清单 / Context Manifests" value={contexts.length} />
      <div className="context-list">
        {contexts.length === 0 ? (
          <InspectorEmpty icon={Layers3} title="尚无上下文 / No context yet" detail="启动一次运行后将生成第一份上下文清单 / Start a run to compile the first manifest." />
        ) : contexts.map((context) => {
          const manifest = context.manifest as Record<string, unknown>;
          const retained = context.microcompact.retained_count ?? 0;
          const discarded = context.microcompact.discarded_count ?? 0;
          const offloaded = context.microcompact.offloaded_count ?? 0;
          const total = retained + discarded || 1;
          return (
            <section className="context-entry" key={context.agent_id}>
              <div className="entry-heading">
                <div><strong>{agentLabel(context.agent_id)}</strong><span>轮次 / epoch {context.context_epoch}</span></div>
                <code>{Number(manifest.token_estimate ?? 0)} 词元 / tok</code>
              </div>
              <div className="context-bar" aria-label={`保留 ${retained}，丢弃 ${discarded} / ${retained} retained, ${discarded} discarded`}>
                <span className="retained" style={{ width: `${(retained / total) * 100}%` }} />
                <span className="discarded" style={{ width: `${(discarded / total) * 100}%` }} />
              </div>
              <div className="metric-grid three">
                <Metric label="保留 / Retained" value={retained} tone="mint" />
                <Metric label="丢弃 / Discarded" value={discarded} tone="muted" />
                <Metric label="卸载 / Offloaded" value={offloaded} tone="amber" />
              </div>
              <div className="hash-line"><span>提示词哈希 / Prompt hash</span><code>{shortId(String(manifest.prompt_hash ?? ""), 20)}</code></div>
            </section>
          );
        })}
      </div>
      <div className="inspector-subsection">
        <PanelHeading
          eyebrow="目录常驻，正文按需 / Metadata first, body on demand"
          title="智能体技能 / Agent Skills"
          value={`${skillState.active.length}/${skillState.catalog.length}`}
        />
        {skillState.catalog.length === 0 ? (
          <InspectorEmpty
            icon={BookOpen}
            title="尚无技能目录 / No Skill catalog"
            detail="新运行会先展示名称和用途；只有显式激活后正文才进入受保护上下文 / New runs disclose metadata first and load the body only after activation."
          />
        ) : (
          <div className="skill-list">
            {skillState.catalog.map((skill) => {
              const active = activeByName.get(skill.name);
              return (
                <section className={`skill-entry ${active ? "active" : "stub"}`} key={skill.name}>
                  <div className="entry-heading">
                    <div>
                      <strong>{skill.name}</strong>
                      <span>{skillScopeLabel(skill.scope)} · {skill.sourceId}</span>
                    </div>
                    <span className={`skill-state ${active ? "active" : "stub"}`}>
                      {active ? "正文已激活 / Active" : "仅元数据 / Stub"}
                    </span>
                  </div>
                  <p>{skill.description}</p>
                  {active ? (
                    <div className="skill-activation-meta">
                      <span><strong>{active.bodyChars}</strong> 字符 / chars</span>
                      <span><strong>{active.resourceCount}</strong> 个资源 / resources</span>
                      <span>事件 / event <code>{shortId(active.eventId, 12)}</code></span>
                      <span>正文哈希 / body hash <code>{shortId(active.bodyHash, 14)}</code></span>
                    </div>
                  ) : (
                    <div className="skill-stub-note">正文尚未进入模型上下文 / Instruction body is not in model Context.</div>
                  )}
                  {active && active.allowedToolsHint.length > 0 && (
                    <div className="skill-tool-hints">
                      <span>工具提示，不是权限 / Tool hints, not authority</span>
                      <code>{active.allowedToolsHint.join(", ")}</code>
                    </div>
                  )}
                </section>
              );
            })}
            {skillState.diagnostics.map((item, index) => (
              <div className={`skill-diagnostic ${item.severity}`} key={`${item.code}:${item.sourceId}:${index}`}>
                <CircleAlert size={14} aria-hidden="true" />
                <span><strong>{item.code}</strong>{item.message}</span>
              </div>
            ))}
          </div>
        )}
        <div className="authority-note">
          <ShieldCheck size={15} aria-hidden="true" />
          <span>Skill 指导模型怎么做，不授予它能做什么；ToolPolicy 仍是执行权威 / Skills guide cognition; ToolPolicy owns execution authority.</span>
        </div>
        {skillState.manifestHash && (
          <div className="hash-line"><span>技能目录哈希 / Catalog hash</span><code>{shortId(skillState.manifestHash, 20)}</code></div>
        )}
      </div>
      <div className="inspector-subsection">
        <PanelHeading
          eyebrow="先鉴权，再披露 / Authorize before disclosure"
          title="能力披露 / Capability Disclosure"
          value={capabilities.length}
        />
        <div className="capability-list">
          {capabilities.length === 0 ? (
            <InspectorEmpty
              icon={ShieldCheck}
              title="尚无能力清单 / No capability manifest"
              detail="启动新版运行后，将记录每轮模型可见与不可见的工具 / A new run records which tools the model could and could not see."
            />
          ) : capabilities.map((capability) => {
            const manifest = capability.manifest as Record<string, unknown>;
            const authorized = stringList(manifest.authorized_names);
            const disclosed = stringList(manifest.disclosed_names);
            const withheld = stringList(manifest.withheld_names);
            const excluded = stringList(manifest.excluded_names);
            const recalled = capabilityRecallEntries(manifest.recall_sources);
            const identities = capabilityIdentityEntries(disclosed, manifest.kinds, manifest.providers);
            const strategy = capability.strategy === "inline_all"
              ? "小目录完整披露 / Inline all"
              : "大目录检索披露 / Search ranked";
            return (
              <section className="capability-entry" key={`${capability.agent_id}:${capability.turn_id}`}>
                <div className="entry-heading">
                  <div>
                    <strong>{agentLabel(capability.agent_id)}</strong>
                    <span>回合 / turn {shortId(capability.turn_id, 14)}</span>
                  </div>
                  <span className={`strategy-badge ${capability.strategy}`}>{strategy}</span>
                </div>
                <div className="metric-grid capability-metrics">
                  <Metric label="能力目录 / Catalog" value={capability.catalog_size} tone="muted" />
                  <Metric label="策略允许 / Authorized" value={authorized.length} tone="mint" />
                  <Metric label="模型可见 / Disclosed" value={capability.disclosed_count} tone="mint" />
                  <Metric label="本轮暂藏 / Withheld" value={capability.withheld_count} tone="amber" />
                  <Metric label="策略排除 / Excluded" value={capability.excluded_count} tone="muted" />
                </div>
                <div className="capability-group">
                  <span>本轮模型看见 / Model-visible</span>
                  <div className="capability-tags">
                    {identities.map((identity) => (
                      <span className={`capability-tag ${identity.kind}`} key={identity.name}>
                        <code>{identity.name}</code>
                        {(identity.kind !== "function" || identity.provider !== "local") && (
                          <small>{capabilityKindLabel(identity.kind)} · {identity.provider}</small>
                        )}
                      </span>
                    ))}
                  </div>
                </div>
                {recalled.length > 0 && (
                  <div className="capability-recall">
                    <span><Search size={14} aria-hidden="true" />搜索召回 / Search-recalled</span>
                    {recalled.map((entry) => (
                      <div key={entry.name}>
                        <code>{entry.name}</code>
                        <small>来自事件 / source <code>{shortId(entry.sourceEventId, 12)}</code></small>
                      </div>
                    ))}
                  </div>
                )}
                {(withheld.length > 0 || excluded.length > 0) && (
                  <div className="capability-hidden">
                    {withheld.length > 0 && <span>暂藏 / Withheld: <code>{withheld.join(", ")}</code></span>}
                    {excluded.length > 0 && <span>排除 / Excluded: <code>{excluded.join(", ")}</code></span>}
                  </div>
                )}
                <div className="authority-note">
                  <ShieldCheck size={15} aria-hidden="true" />
                  <span>可见不等于获准执行；ToolPolicy 仍在执行边界再次鉴权 / Visibility is not execution authority.</span>
                </div>
                <div className="hash-line">
                  <span>披露清单哈希 / Manifest hash</span>
                  <code>{shortId(String(manifest.manifest_hash ?? ""), 20)}</code>
                </div>
              </section>
            );
          })}
        </div>
      </div>
    </>
  );
}

function A2AInspector({ events }: { events: EventRecord[] }) {
  const agentRuns = events
    .filter((record) => record.event.type === "agent.run.created")
    .map((seed) => agentRunSummary(seed, events));
  const peerEvents = events.filter((record) => record.event.type.startsWith("a2a."));
  const allowed = peerEvents.find((record) => record.event.type === "a2a.policy.allowed");
  const request = peerEvents.find((record) => record.event.type === "a2a.peer.requested");
  const response = peerEvents.find((record) => record.event.type === "a2a.peer.responded");
  return (
    <>
      <PanelHeading eyebrow="受控的一跳通道 / Bounded peer channel" title="A2A 对账 / Reconciliation" value={peerEvents.length} />
      {agentRuns.length > 0 && (
        <AgentRunList runs={agentRuns} />
      )}
      {peerEvents.length === 0 ? (
        <InspectorEmpty icon={Network} title="尚无 A2A 通信 / No peer traffic" detail="等待持久消息进入受控对账通道 / Waiting for a durable peer message." />
      ) : (
        <>
          <div className="peer-route">
            <span className="peer-node builder">构建 / Builder</span>
            <span className="peer-line"><i /><Network size={16} /></span>
            <span className="peer-node scout">侦察 / Scout</span>
          </div>
          <div className="policy-summary">
            <ShieldCheck size={17} />
            <div>
              <strong>{allowed ? "策略已允许 / Policy allowed" : "策略待决 / Policy pending"}</strong>
              <span>深度 / depth {String(request?.event.payload?.depth ?? 1)} · 剩余预算 / budget left {String(allowed?.event.payload?.remaining_budget ?? "—")}</span>
            </div>
          </div>
          <div className="a2a-sequence">
            {peerEvents.map((record) => {
              const meta = eventMeta(record.event.type);
              return (
                <div key={record.cursor}>
                  <span className={`sequence-dot tone-${meta.tone}`} />
                  <div><strong>{meta.label}</strong><span>{record.event.source}</span></div>
                  <code>#{record.cursor}</code>
                </div>
              );
            })}
          </div>
          <div className="evidence-capsule">
            <Database size={16} />
            <div><strong>共享证据胶囊 / Shared capsule</strong><span>{(response?.event.payload?.evidence_refs as unknown[] | undefined)?.length ?? 0} 个证据引用 · 不共享私有上下文 / no private Context</span></div>
          </div>
        </>
      )}
    </>
  );
}

type AgentRunTone = "info" | "success" | "warning" | "danger";

interface AgentRunSummary {
  taskId: string;
  label: string;
  kind: string;
  status: string;
  tone: AgentRunTone;
  modelTurns: number;
  toolCalls: number;
  waits: number;
}

function AgentRunList({ runs }: { runs: AgentRunSummary[] }) {
  return (
    <section className="agent-run-section">
      <div className="section-label">
        <span>子 AgentLoop 执行链 / Child AgentLoop chain</span>
        <span>{runs.length} 个 AgentRun</span>
      </div>
      <div className="agent-run-list">
        {runs.map((run) => (
          <div className="agent-run-entry" key={run.taskId}>
            <span className={`sequence-dot tone-${run.tone}`} />
            <div className="agent-run-copy">
              <strong>{run.label}</strong>
              <span>
                {run.modelTurns} 模型轮次 · {run.toolCalls} 工具 · {run.waits} 等待 ·{" "}
                <code title={run.taskId}>{shortId(run.taskId, 15)}</code>
              </span>
            </div>
            <strong className={`agent-run-status tone-${run.tone}`}>{run.status}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

function agentRunSummary(seed: EventRecord, events: EventRecord[]): AgentRunSummary {
  const payload = seed.event.payload as Record<string, unknown>;
  const taskId = seed.event.task_id;
  const ownEvents = events.filter((record) => record.event.task_id === taskId);
  const eventTypes = ownEvents.map((record) => record.event.type);
  const kind = String(payload.agent_run_kind ?? "assignment");
  const agent = agentLabel(String(payload.agent_id ?? "agent"));
  const stage = stageLabel(String(payload.stage_id ?? ""));
  const label = kind === "peer" ? `${agent} · 对账响应 / Peer response` : `${agent} · ${stage}`;
  const lastWait = eventTypes.lastIndexOf("agent.waiting");
  const resumed = lastWait >= 0 && eventTypes.slice(lastWait + 1).includes("model.requested");

  let status = "执行中 / Running";
  let tone: AgentRunTone = "info";
  if (eventTypes.includes("agent.result.rejected")) {
    status = "被拒 / Rejected";
    tone = "danger";
  } else if (eventTypes.includes("agent.result.promoted")) {
    status = "已晋升 / Promoted";
    tone = "success";
  } else if (eventTypes.includes("agent.submitted")) {
    status = "已提交 / Submitted";
    tone = "warning";
  } else if (lastWait >= 0 && !resumed) {
    status = "等待 / Waiting";
    tone = "warning";
  }

  return {
    taskId,
    label,
    kind,
    status,
    tone,
    modelTurns: eventTypes.filter((type) => type === "model.requested").length,
    toolCalls: eventTypes.filter((type) => type === "tool.completed").length,
    waits: eventTypes.filter((type) => type === "agent.waiting").length,
  };
}

function stageLabel(stage: string): string {
  const labels: Record<string, string> = {
    evidence: "证据采集 / Evidence",
    artifact: "制品构建 / Artifact",
    review: "独立审查 / Review",
  };
  return labels[stage] ?? (stage || "任务执行 / Assignment");
}

function MemoryInspector({ snapshot, events }: { snapshot: Snapshot | null; events: EventRecord[] }) {
  const memories = snapshot?.memories ?? [];
  const jobs = snapshot?.dream_jobs ?? [];
  const signal = events.find((record) => record.event.type === "learning.signal.detected");
  return (
    <>
      <PanelHeading eyebrow="证据治理的学习 / Evidence-governed learning" title="异步蒸馏与记忆 / Dream & Memory" value={memories.length} />
      <div className="learning-pipeline">
        <PipelineStep label="信号" state={signal ? "done" : "pending"} />
        <PipelineStep label="蒸馏" state={jobs.some((job) => job.status === "completed") ? "done" : jobs.length ? "active" : "pending"} />
        <PipelineStep label="准入" state={memories.length ? "done" : "pending"} />
        <PipelineStep label="生效" state={memories.some((memory) => memory.status === "active") ? "done" : "pending"} />
      </div>
      {memories.length === 0 ? (
        <InspectorEmpty icon={BrainCircuit} title="尚无记忆候选 / No memory candidate" detail="CompletionGate 通过后才会运行 Dream / Dream runs after CompletionGate passes." />
      ) : memories.map((memory) => (
        <section className="memory-entry" key={memory.candidate_id}>
          <div className="entry-heading">
            <div><strong>{memory.slot ?? "记忆 / Memory"}</strong><span>{memory.scope ?? "—"}</span></div>
            <span className={`zone-badge ${memory.admission_zone ?? "yellow"}`}>{statusLabel(memory.admission_zone ?? "review")}</span>
          </div>
          <p>{contentLabel(memory.content)}</p>
          <div className="memory-meta">
            <span><Database size={13} />{memory.evidence_refs?.length ?? 0} 个引用 / refs</span>
            <span>置信度 / confidence {memory.confidence?.toFixed(2) ?? "—"}</span>
            <strong>{statusLabel(memory.status ?? "pending")}</strong>
          </div>
        </section>
      ))}
    </>
  );
}

function EvolutionInspector({ snapshot }: { snapshot: Snapshot | null }) {
  const evolutions = snapshot?.evolutions ?? [];
  return (
    <>
      <PanelHeading eyebrow="受控行为变更 / Controlled behavior change" title="进化门禁 / Evolution Gates" value={evolutions.length} />
      {evolutions.length === 0 ? (
        <InspectorEmpty icon={GitCompareArrows} title="尚无进化候选 / No candidate" detail="经过验证的 Dream 信号可以产生受控差异 / A verified Dream signal may produce a bounded diff." />
      ) : evolutions.map((item) => (
        <section className="evolution-entry" key={item.candidate_id}>
          <div className="version-line">
            <code>{item.base_version}</code><ChevronRight size={15} /><code>{item.proposed_version}</code>
          </div>
          <p>{contentLabel(item.rationale)}</p>
          <div className="gate-track">
            <Gate label="候选" state="passed" />
            <Gate label="回放" state={item.status === "candidate" ? "active" : "passed"} />
            <Gate label="影子" state={item.next_gate === "shadow" ? "active" : "pending"} />
            <Gate label="灰度" state="pending" />
            <Gate label="生效" state="pending" />
          </div>
          <div className="diff-block">
            {(item.diffs ?? []).map((diff, index) => (
              <div key={index}>
                <code>{String(diff.path)}</code>
                <span>{String(diff.before)} → {String(diff.after)}</span>
              </div>
            ))}
          </div>
          <div className="honesty-note"><CircleAlert size={14} />只展示已经真实通过的门禁，尚未声称完成 Git 晋升 / Stops at the last real gate; no Git promotion is claimed.</div>
        </section>
      ))}
    </>
  );
}

function ChaosInspector({
  busy,
  hasRun,
  onArmFault,
  onProbeDepth,
  onRebuild,
}: {
  busy: boolean;
  hasRun: boolean;
  onArmFault: InspectorPanelProps["onArmFault"];
  onProbeDepth: InspectorPanelProps["onProbeDepth"];
  onRebuild: InspectorPanelProps["onRebuild"];
}) {
  return (
    <>
      <PanelHeading eyebrow="确定性故障注入 / Deterministic fault injection" title="故障实验室 / Chaos Lab" />
      <div className="chaos-list">
        <button disabled={busy} onClick={() => void onArmFault("after_candidate_persisted")}>
          <Bug size={17} /><span><strong>候选落盘后崩溃 / Crash after Candidate</strong><small>模型响应已持久化，随后中断 Harness 步骤 / Model response is durable; Harness step interrupts.</small></span><Zap size={15} />
        </button>
        <button disabled={busy} onClick={() => void onArmFault("after_model_persisted")}>
          <Bug size={17} /><span><strong>模型响应落盘后崩溃 / Crash after Model</strong><small>重投后复用已持久响应，不再次调用模型 / Reuse the durable response without another model call.</small></span><Zap size={15} />
        </button>
        <button disabled={busy} onClick={() => void onArmFault("after_command_persisted")}>
          <Bug size={17} /><span><strong>命令校验后崩溃 / Crash after Command</strong><small>从正式 Command 恢复，不重新解析模型文本 / Recover the validated Command without reparsing.</small></span><Zap size={15} />
        </button>
        <button disabled={busy} onClick={() => void onArmFault("after_tool_effect")}>
          <Bug size={17} /><span><strong>工具效果后崩溃 / Crash after Tool effect</strong><small>由 OperationLedger 对账并补写 Observation / Reconcile from the ledger and persist the observation.</small></span><Zap size={15} />
        </button>
        <button disabled={busy} onClick={() => void onArmFault("before_mailbox_ack")}>
          <Bug size={17} /><span><strong>邮箱确认前崩溃 / Crash before Mailbox ack</strong><small>正式事实已存在，故意重新投递同一消息 / Formal facts exist; Delivery is intentionally redelivered.</small></span><Zap size={15} />
        </button>
        <button disabled={busy || !hasRun} onClick={() => void onProbeDepth()}>
          <Network size={17} /><span><strong>探测递归 A2A / Probe recursive A2A</strong><small>提交 depth=2，观察确定性拒绝 / Submit depth=2 and observe deterministic denial.</small></span><ShieldCheck size={15} />
        </button>
        <button disabled={busy} onClick={() => void onRebuild()}>
          <RefreshCw size={17} /><span><strong>重建投影视图 / Rebuild projections</strong><small>删除读取模型并重放 SQLite 事实 / Delete read models and replay SQLite facts.</small></span><Database size={15} />
        </button>
      </div>
      <div className="chaos-boundary">
        <TestTubeDiagonal size={16} />
        <span>故障只触发一次且仅限本地，不会调用外部云资源 / Faults are one-shot and local; no external cloud effects.</span>
      </div>
    </>
  );
}

function InspectorEmpty({ icon: Icon, title, detail }: { icon: LucideIcon; title: string; detail: string }) {
  return <div className="inspector-empty"><Icon size={24} /><strong>{title}</strong><span>{detail}</span></div>;
}

function Metric({ label, value, tone }: { label: string; value: number; tone: string }) {
  return <div className={`metric ${tone}`}><strong>{value}</strong><span>{label}</span></div>;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function skillScopeLabel(scope: string): string {
  if (scope === "agent") return "Agent 专属 / Agent";
  if (scope === "project") return "项目级 / Project";
  if (scope === "global") return "全局 / Global";
  return scope || "未知来源 / Unknown";
}

function capabilityKindLabel(kind: string): string {
  if (kind === "mcp") return "远端工具 / MCP";
  if (kind === "skill") return "技能 / Skill";
  return "函数 / Function";
}

function PipelineStep({ label, state }: { label: string; state: "pending" | "active" | "done" }) {
  return <div className={state}><span /><strong>{label}</strong></div>;
}

function Gate({ label, state }: { label: string; state: "pending" | "active" | "passed" }) {
  return <div className={state}><span /><strong>{label}</strong></div>;
}
