import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import App from "./App";

vi.mock("./hooks/useControlPlane", () => ({
  useControlPlane: () => ({
    runId: undefined,
    snapshot: null,
    events: [],
    selected: null,
    followLive: false,
    streamState: "offline",
    busy: false,
    notice: null,
    setNotice: vi.fn(),
    selectRun: vi.fn(),
    createRun: vi.fn(),
    cancelRun: vi.fn(),
    armFault: vi.fn(),
    probeDepth: vi.fn(),
    rebuildProjections: vi.fn(),
    selectEvent: vi.fn(),
    resumeLive: vi.fn(),
  }),
}));

vi.mock("./hooks/usePairedEval", () => ({
  usePairedEval: () => ({
    evalId: undefined,
    report: null,
    selectedArm: "single",
    busy: false,
    loading: false,
    notice: null,
    setNotice: vi.fn(),
    createEval: vi.fn(),
    openEval: vi.fn(),
    selectArm: vi.fn(),
    clearEval: vi.fn(),
  }),
}));

vi.mock("./hooks/useEvalCampaign", () => ({
  useEvalCampaign: () => ({
    campaignId: "campaign_visible",
    report: null,
    busy: false,
    loading: true,
    notice: null,
    setNotice: vi.fn(),
    createCampaign: vi.fn(),
    clearCampaign: vi.fn(),
  }),
}));

vi.mock("./hooks/useCheckpoints", () => ({
  useCheckpoints: () => ({
    checkpoints: [],
    selected: null,
    label: "",
    loading: false,
    busy: false,
    notice: null,
    setLabel: vi.fn(),
    setNotice: vi.fn(),
    selectCheckpoint: vi.fn(),
    createCheckpoint: vi.fn(),
    restoreCheckpoint: vi.fn(),
    refresh: vi.fn(),
  }),
}));

describe("Control Room campaign integration", () => {
  it("renders a persisted Campaign as a first-class control surface", () => {
    const html = renderToStaticMarkup(<App />);

    expect(html).toContain("恢复多轮评测");
    expect(html).toContain("campaign_visible");
  });
});
