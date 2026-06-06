import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { formatDisplayTime, itemDisplayTimestamp } from "../src/timeDisplay.js";

describe("intelligence card display timestamp", () => {
  it("falls back to fetched_at when published_at is missing", () => {
    const timestamp = itemDisplayTimestamp({
      published_at: null,
      fetched_at: "2026-06-05T08:58:08.901928+00:00"
    });

    assert.equal(timestamp, "2026-06-05T08:58:08.901928+00:00");
  });

  it("uses published_at before fetched_at when both exist", () => {
    const timestamp = itemDisplayTimestamp({
      published_at: "2026-05-27T10:00:00+08:00",
      fetched_at: "2026-06-05T08:58:08.901928+00:00"
    });

    assert.equal(timestamp, "2026-05-27T10:00:00+08:00");
  });

  it("does not throw when LLM key facts contain natural-language dates", () => {
    assert.equal(formatDisplayTime("2026年6月（CVPR 2026会议期间）"), "2026年6月（CVPR 2026会议期间）");
  });

  it("uses unknown time only for empty timestamps", () => {
    assert.equal(formatDisplayTime(""), "未知时间");
  });

  it("formats valid ISO timestamps for display", () => {
    assert.match(formatDisplayTime("2026-06-05T08:58:08.901928+00:00"), /^06\/05/);
  });
});
