import { Bot, Boxes, Hammer, Mail, Network, Radar, ShieldCheck } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { Snapshot } from "../api/client";
import { agentLabel, capabilityLabel, contentLabel, statusLabel } from "../lib/i18n";
import { leaseSummary } from "../lib/leases";

const AGENT_ICON: Record<string, LucideIcon> = {
  coordinator: Network,
  scout: Radar,
  "scout-backup": Radar,
  builder: Hammer,
  reviewer: ShieldCheck,
  generalist: Bot,
};

interface AgentRailProps {
  snapshot: Snapshot | null;
}

export function AgentRail({ snapshot }: AgentRailProps) {
  const agents = snapshot?.agents ?? [];
  const assignments = snapshot?.assignments ?? [];
  const leases = snapshot?.leases ?? [];
  return (
    <aside className="agent-rail">
      <div className="rail-heading">
        <div>
          <span className="eyebrow">常驻身份 / Resident identities</span>
          <h2>智能体团队 / Agent Team</h2>
        </div>
        <span className="rail-count">{agents.length}</span>
      </div>

      <div className="agent-list">
        {agents.map((agent) => {
          const Icon = AGENT_ICON[agent.agent_id] ?? Boxes;
          const assignment = assignments.find(
            (item) => item.agent_id === agent.agent_id
              && !["succeeded", "failed", "completed", "expired"].includes(item.status),
          );
          const lease = assignment
            ? leases.find((item) => item.assignment_id === assignment.assignment_id)
            : undefined;
          const leaseCopy = leaseSummary(lease);
          return (
            <div className={`agent-row agent-${agent.agent_id}`} key={agent.agent_id}>
              <div className="agent-icon" aria-hidden="true"><Icon size={18} /></div>
              <div className="agent-copy">
                <div className="agent-name-line">
                  <strong>{agentLabel(agent.agent_id)}</strong>
                  <span className={`status-dot ${agent.status}`} title={statusLabel(agent.status)} />
                </div>
                <span>{statusLabel(agent.status)}</span>
                <small>{assignment ? contentLabel(assignment.goal) : capabilityLabel(agent.capabilities?.[0])}</small>
              </div>
                {leaseCopy ? (
                  <small className={`lease-copy ${lease?.status}`}>{leaseCopy}</small>
                ) : null}
              <div className="mailbox-count" title="待处理的持久邮箱投递 / Pending durable mailbox deliveries">
                <Mail size={13} aria-hidden="true" />
                <span>{agent.mailbox_pending}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="rail-section">
        <div className="rail-section-title">
          <span>任务委派 / Assignments</span>
          <span>{assignments.length}</span>
        </div>
        <div className="assignment-list">
          {assignments.length === 0 ? (
            <div className="rail-empty">暂无运行 / No active run</div>
          ) : assignments.map((assignment) => {
            const lease = leases.find((item) => item.assignment_id === assignment.assignment_id);
            const leaseCopy = leaseSummary(lease);
            return (
              <div className="assignment-row" key={assignment.assignment_id}>
                <span className={`assignment-state ${assignment.status}`} />
                <div>
                  <strong>{agentLabel(assignment.agent_id)}</strong>
                  <span title={leaseCopy ?? undefined}>{leaseCopy ?? statusLabel(assignment.status)}</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="fact-source">
        <DatabaseGlyph />
        <div>
          <span>事实源 / Source of truth</span>
          <strong>SQLite / WAL</strong>
        </div>
      </div>
    </aside>
  );
}

function DatabaseGlyph() {
  return <span className="database-glyph" aria-hidden="true"><span /><span /><span /></span>;
}
