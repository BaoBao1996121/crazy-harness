import { X } from "lucide-react";
import { useState } from "react";

import { AgentRail } from "./components/AgentRail";
import { CreateRunDialog } from "./components/CreateRunDialog";
import { InspectorPanel, type InspectorTab } from "./components/InspectorPanel";
import { Timeline } from "./components/Timeline";
import { TopBar } from "./components/TopBar";
import { useControlPlane } from "./hooks/useControlPlane";

export default function App() {
  const control = useControlPlane();
  const [showAll, setShowAll] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("event");

  const openChaos = () => {
    setInspectorTab("chaos");
    window.setTimeout(() => document.querySelector(".inspector-panel")?.scrollIntoView({ block: "start" }), 0);
  };

  return (
    <div className="control-room">
      <TopBar
        snapshot={control.snapshot}
        streamState={control.streamState}
        eventCount={control.events.length}
        busy={control.busy}
        onNewRun={() => setDialogOpen(true)}
        onChaos={openChaos}
      />
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
        onSubmit={control.createRun}
      />
      {control.notice && (
        <div className="notice" role="status">
          <span>{control.notice}</span>
          <button className="icon-only" onClick={() => control.setNotice(null)} title="关闭提示 / Dismiss">
            <X size={15} />
          </button>
        </div>
      )}
    </div>
  );
}
