import { X } from "lucide-react";
import { useState } from "react";

import { AgentRail } from "./components/AgentRail";
import { CampaignBand } from "./components/CampaignBand";
import { CheckpointBand } from "./components/CheckpointBand";
import { CreateCampaignDialog } from "./components/CreateCampaignDialog";
import { CreateEvalDialog } from "./components/CreateEvalDialog";
import { CreateRunDialog } from "./components/CreateRunDialog";
import { EvalComparisonBand } from "./components/EvalComparisonBand";
import { InspectorPanel, type InspectorTab } from "./components/InspectorPanel";
import { Timeline } from "./components/Timeline";
import { TopBar } from "./components/TopBar";
import { useControlPlane } from "./hooks/useControlPlane";
import { useCheckpoints } from "./hooks/useCheckpoints";
import { useEvalCampaign } from "./hooks/useEvalCampaign";
import { usePairedEval } from "./hooks/usePairedEval";

export default function App() {
  const control = useControlPlane();
  const pairedEval = usePairedEval({
    activeRunId: control.runId,
    onSelectRun: control.selectRun,
  });
  const campaign = useEvalCampaign();
  const [checkpointOpen, setCheckpointOpen] = useState(false);
  const checkpoints = useCheckpoints({
    runId: control.runId,
    enabled: checkpointOpen,
    onSelectRun: control.selectRun,
  });
  const [showAll, setShowAll] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [evalDialogOpen, setEvalDialogOpen] = useState(false);
  const [campaignDialogOpen, setCampaignDialogOpen] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("event");

  const openChaos = () => {
    setInspectorTab("chaos");
    window.setTimeout(() => document.querySelector(".inspector-panel")?.scrollIntoView({ block: "start" }), 0);
  };

  return (
    <div className={`control-room ${pairedEval.evalId || campaign.campaignId ? "has-eval" : ""} ${checkpointOpen && control.runId ? "has-checkpoint" : ""}`}>
      <TopBar
        snapshot={control.snapshot}
        streamState={control.streamState}
        eventCount={control.events.length}
        busy={control.busy || pairedEval.busy || campaign.busy || checkpoints.busy}
        onNewRun={() => setDialogOpen(true)}
        onNewEval={() => setEvalDialogOpen(true)}
        onNewCampaign={() => setCampaignDialogOpen(true)}
        onCheckpoints={() => setCheckpointOpen(true)}
        onCancel={() => void control.cancelRun()}
        onChaos={openChaos}
      />
      {(campaign.campaignId || pairedEval.evalId) && (
        <div className="eval-stack">
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
          if (created) campaign.clearCampaign();
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
          if (created) pairedEval.clearEval();
          return created;
        }}
      />
      {(checkpoints.notice || campaign.notice || pairedEval.notice || control.notice) && (
        <div className="notice" role="status">
          <span>{checkpoints.notice || campaign.notice || pairedEval.notice || control.notice}</span>
          <button
            className="icon-only"
            onClick={() => {
              campaign.setNotice(null);
              pairedEval.setNotice(null);
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
