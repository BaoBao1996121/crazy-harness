import { describe, expect, it } from "vitest";

import { nextDialogFocusIndex } from "./CreateCampaignDialog";

describe("Create Campaign dialog focus trap", () => {
  it("wraps Tab and Shift+Tab at the dialog boundaries", () => {
    expect(nextDialogFocusIndex(3, 4, false)).toBe(0);
    expect(nextDialogFocusIndex(0, 4, true)).toBe(3);
    expect(nextDialogFocusIndex(1, 4, false)).toBeUndefined();
  });
});
