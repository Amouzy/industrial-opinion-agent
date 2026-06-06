import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { feedInspectionStatus, isFeedAutoInspecting } from "../src/feedInspection.js";

describe("feed inspection cruise state", () => {
  it("gives manual click pause priority over hover and auto inspection", () => {
    const state = {
      canInspect: true,
      isHovered: false,
      isManuallyPaused: true
    };

    assert.equal(isFeedAutoInspecting(state), false);
    assert.equal(feedInspectionStatus(state), "点击暂停循环");
  });

  it("falls back to hover pause after manual pause is released", () => {
    const state = {
      canInspect: true,
      isHovered: true,
      isManuallyPaused: false
    };

    assert.equal(isFeedAutoInspecting(state), false);
    assert.equal(feedInspectionStatus(state), "悬停暂停巡检");
  });

  it("auto cruises only when the list can inspect and has no pause condition", () => {
    const state = {
      canInspect: true,
      isHovered: false,
      isManuallyPaused: false
    };

    assert.equal(isFeedAutoInspecting(state), true);
    assert.equal(feedInspectionStatus(state), "自动巡航中");
  });
});
