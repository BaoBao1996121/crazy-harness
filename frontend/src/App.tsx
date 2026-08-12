import { X } from "lucide-react";
import { useState } from "react";

import { AgentRail } from "./components/AgentRail";
import { AgentRunControlBand } from "./components/AgentRunControlBand";
import { CampaignBand } from "./components/CampaignBand";
import { CheckpointBand } from "./components/CheckpointBand";
import { CreateCampaignDialog } from "./components/CreateCampaignDialog";
import { CreateEngineeringLoopDialog } from "./components/CreateEngineeringLoopDialog";
import { CreateEvalDialog } from "./components/CreateEvalDialog";
import { CreateRunDialog } from "./components/CreateRunDialog";
import { EvalComparisonBand } from "./components/EvalComparisonBand";
import { EngineeringLoopBand } from "./components/EngineeringLoopBand";
import { InspectorPanel, type InspectorTab } from "./components/InspectorPanel";
import { Timeline } from "./components/Timeline";
import { TopBar } from "./components/TopBar";
import { useControlPlane } from "./hooks/useControlPlane";
import { useAgentRunControls } from "./hooks/useAgentRunControls";
import { useCheckpoints } from "./hooks/useCheckpoints";
import { useEvalCampaign } from "./hooks/useEvalCampaign";
import { useEngineeringLoop } from "./hooks/useEngineeringLoop";
import { usePairedEval } from "./hooks/usePairedEval";

export default function App() {
  const control = useControlPlane();
  const pairedEval = usePairedEval({
    activeRunId: control.runId,
    onSelectRun: control.selectRun,
  });
  const campaign = useEvalCampaign();
  const engineering = useEngineeringLoop();
  const [checkpointOpen, setCheckpointOpen] = useState(false);
  const [agentControlOpen, setAgentControlOpen] = useState(false);
  const executionMode = control.events.find(
    (record) => record.event.type === "run.created",
  )?.event.payload?.execution_mode;
  const isSingleAgentRun = executionMode === "single";
  const checkpoints = useCheckpoints({
    runId: control.runId,
    enabled: checkpointOpen,
    onSelectRun: control.selectRun,
  });
  const agentControls = useAgentRunControls({
    runId: control.runId,
    enabled: agentControlOpen && isSingleAgentRun,
    refreshToken: control.events.at(-1)?.event.id,
    onSelectRun: control.selectRun,
  });
  const [showAll, setShowAll] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [evalDialogOpen, setEvalDialogOpen] = useState(false);
  const [campaignDialogOpen, setCampaignDialogOpen] = useState(false);
  const [engineeringDialogOpen, setEngineeringDialogOpen] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("event");

  const openChaos = () => {
    setInspectorTab("chaos");
    window.setTimeout(() => document.querySelector(".inspector-panel")?.scrollIntoView({ block: "start" }), 0);
  };

  return (
    <div className={`control-room ${pairedEval.evalId || campaign.campaignId || engineering.loopId ? "has-eval" : ""} ${(checkpointOpen || (agentControlOpen && isSingleAgentRun)) && control.runId ? "has-checkpoint" : ""}`}>
      <TopBar
        snapshot={control.snapshot}
        runtime={engineering.loopId ? engineering.runtime : null}
        streamState={engineering.loopId ? engineering.streamState : control.streamState}
        eventCount={control.events.length}
        busy={control.busy || pairedEval.busy || campaign.busy || engineering.busy || checkpoints.busy || agentControls.busy}
        agentControlAvailable={isSingleAgentRun}
        onNewRun={() => setDialogOpen(true)}
        onNewEval={() => setEvalDialogOpen(true)}
        onNewCampaign={() => setCampaignDialogOpen(true)}
        onNewEngineeringLoop={() => setEngineeringDialogOpen(true)}
        onCheckpoints={() => {
          setAgentControlOpen(false);
          setCheckpointOpen(true);
        }}
        onAgentControl={() => {
          setCheckpointOpen(false);
          setAgentControlOpen(true);
        }}
        onCancel={() => void control.cancelRun()}
        onChaos={openChaos}
      />
      {(engineering.loopId || campaign.campaignId || pairedEval.evalId) && (
        <div className="eval-stack">
          {engineering.loopId && (
            <EngineeringLoopBand
              loopId={engineering.loopId}
              report={engineering.report}
              loading={engineering.loading}
              busy={engineering.busy}
              onAdvance={() => void engineering.advance()}
              onDrain={() => void engineering.drain()}
              onPause={() => void engineering.pause()}
              onResume={() => void engineering.resume()}
              onCancel={() => void engineering.cancel()}
              onSelectRun={control.selectRun}
              onClose={() => engineering.clearLoop()}
            />
          )}
          {campaign.campaignId && (
            <CampaignBand
              campaignId={campaign.campaignId}
              report={campaign.report}
              loading={campaign.loading}
              busy={campaign.busy}
              onOpenTrial={pairedEval.openEval}
              onCancel={() => void campaign.cancelCampaign()}
              onClose={() => campaign.clearCampaign()}
            />
          )}
          {pairedEval.evalId && (
            <EvalComparisonBand
              evalId={pairedEval.evalId}
              report={pairedEval.report}
              loading={pairedEval.loading}
              selectedArm={pairedEval.selectedArm}
              onSelectArm={pairedEval.selectArm}
              onClose={() => pairedEval.clearEval()}
            />
          )}
        </div>
      )}
      {agentControlOpen && isSingleAgentRun && control.runId && (
        <AgentRunControlBand
          runId={control.runId}
          session={agentControls.session}
          branch={agentControls.branch}
          nudge={agentControls.nudge}
          forkLabel={agentControls.forkLabel}
          loading={agentControls.loading}
          busy={agentControls.busy}
          onNudgeChange={agentControls.setNudge}
          onForkLabelChange={agentControls.setForkLabel}
          onPause={() => void agentControls.pause()}
          onResume={() => void agentControls.resume()}
          onSendNudge={() => void agentControls.sendNudge()}
          onFork={() => void agentControls.fork()}
          onSelectRun={control.selectRun}
          onClose={() => setAgentControlOpen(false)}
        />
      )}
      {checkpointOpen && control.runId && (
        <CheckpointBand
          runId={control.runId}
          checkpoints={checkpoints.checkpoints}
          selected={checkpoints.selected}
          label={checkpoints.label}
          loading={checkpoints.loading}
          busy={checkpoints.busy}
          onLabelChange={checkpoints.setLabel}
          onCreate={() => void checkpoints.createCheckpoint()}
          onSelect={checkpoints.selectCheckpoint}
          onRestore={(checkpointId) => void checkpoints.restoreCheckpoint(checkpointId)}
          onClose={() => setCheckpointOpen(false)}
        />
      )}
      <div className="workspace">
        <AgentRail snapshot={control.snapshot} />
        <Timeline
          events={control.events}
          selectedId={control.selected?.event.id ?? null}
          showAll={showAll}
          followLive={control.followLive}
          onShowAll={setShowAll}
          onSelect={(eventId) => {
            control.selectEvent(eventId);
            setInspectorTab("event");
          }}
          onResumeLive={control.resumeLive}
        />
        <InspectorPanel
          activeTab={inspectorTab}
          selected={control.selected}
          events={control.events}
          snapshot={control.snapshot}
          busy={control.busy}
          onTabChange={setInspectorTab}
          onSelectEvent={control.selectEvent}
          onArmFault={control.armFault}
          onProbeDepth={control.probeDepth}
          onRebuild={control.rebuildProjections}
        />
      </div>
      <CreateRunDialog
        open={dialogOpen}
        busy={control.busy}
        deepseekConfigured={control.snapshot?.runtime.deepseek_configured ?? false}
        onClose={() => setDialogOpen(false)}
        onSubmit={async (request) => {
          const created = await control.createRun(request);
          pairedEval.clearEval();
          campaign.clearCampaign();
          engineering.clearLoop();
          return created;
        }}
      />
      <CreateEvalDialog
        open={evalDialogOpen}
        busy={pairedEval.busy}
        deepseekConfigured={control.snapshot?.runtime.deepseek_configured ?? false}
        onClose={() => setEvalDialogOpen(false)}
        onSubmit={async (request) => {
          const created = await pairedEval.createEval(request);
          if (created) {
            campaign.clearCampaign();
            engineering.clearLoop();
          }
          return created;
        }}
      />
      <CreateCampaignDialog
        open={campaignDialogOpen}
        busy={campaign.busy}
        deepseekConfigured={control.snapshot?.runtime.deepseek_configured ?? false}
        onClose={() => setCampaignDialogOpen(false)}
        onSubmit={async (request) => {
          const created = await campaign.createCampaign(request);
          if (created) {
            pairedEval.clearEval();
            engineering.clearLoop();
          }
          return created;
        }}
      />
      <CreateEngineeringLoopDialog
        open={engineeringDialogOpen}
        busy={engineering.busy}
        deepseekConfigured={control.snapshot?.runtime.deepseek_configured ?? false}
        onClose={() => setEngineeringDialogOpen(false)}
        onSubmit={async (request) => {
          const created = await engineering.createLoop(request);
          if (created) {
            pairedEval.clearEval();
            campaign.clearCampaign();
          }
          return created;
        }}
      />
      {(engineering.notice || agentControls.notice || checkpoints.notice || campaign.notice || pairedEval.notice || control.notice) && (
        <div className="notice" role="status">
          <span>{engineering.notice || agentControls.notice || checkpoints.notice || campaign.notice || pairedEval.notice || control.notice}</span>
          <button
            className="icon-only"
            onClick={() => {
              campaign.setNotice(null);
              pairedEval.setNotice(null);
              engineering.setNotice(null);
              agentControls.setNotice(null);
              checkpoints.setNotice(null);
              control.setNotice(null);
            }}
            title="关闭提示 / Dismiss"
          >
            <X size={15} />
          </button>
        </div>
      )}
    </div>
  );
}
