import { describe, expect, it } from "vitest";
import type { Alternative, Thread } from "../../api/chat";
import { applyEvent, freshLive, groupThreads, lastAssistantIndex, msgTime, shownAlternative, workingLine } from "./helpers";

const NOW = new Date(2026, 9, 25, 15, 0, 0).getTime(); // 25 Oct 2026, 15:00 local
const at = (daysAgo: number, hour = 9) => new Date(2026, 9, 25 - daysAgo, hour, 5, 0).toISOString();
const thread = (id: string, updated_at: string): Thread => ({ thread_id: id, title: id, preview: null, created_at: updated_at, updated_at });

describe("groupThreads", () => {
  it("splits into today, this week and earlier, keeping the order it was given", () => {
    const groups = groupThreads([thread("a", at(0)), thread("b", at(0, 8)), thread("c", at(1)), thread("d", at(6)), thread("e", at(7)), thread("f", at(40))], NOW);
    expect(groups.map((g) => g.group)).toEqual(["Today", "This week", "Earlier"]);
    expect(groups[0]?.threads.map((x) => x.thread_id)).toEqual(["a", "b"]);
    expect(groups[1]?.threads.map((x) => x.thread_id)).toEqual(["c", "d"]);
    expect(groups[2]?.threads.map((x) => x.thread_id)).toEqual(["e", "f"]);
  });
  it("leaves out groups with nothing in them", () => {
    expect(groupThreads([thread("a", at(0))], NOW).map((g) => g.group)).toEqual(["Today"]);
    expect(groupThreads([], NOW)).toEqual([]);
  });
});

describe("msgTime", () => {
  it("shows only the time for today and the day for other days", () => {
    expect(msgTime(at(0, 14), NOW)).toBe("14:05");
    expect(msgTime(at(1, 9), NOW)).toBe("24 Oct, 09:05");
  });
  it("is empty without a usable time", () => {
    expect(msgTime(null, NOW)).toBe("");
    expect(msgTime("not a date", NOW)).toBe("");
  });
});

describe("applyEvent", () => {
  it("adds streamed words to the same reply and starts over for a new model call", () => {
    let live = freshLive(1);
    live = applyEvent(live, { type: "token", id: "m1", text: "Saved " });
    live = applyEvent(live, { type: "token", id: "m1", text: "it." });
    expect(live.text).toBe("Saved it.");
    live = applyEvent(live, { type: "token", id: "m2", text: "Done." });
    expect(live.text).toBe("Done.");
  });
  it("keeps tool steps, trace lines and a pending question", () => {
    let live = freshLive(1);
    live = applyEvent(live, { type: "step", tool: "log_transaction", error: false });
    live = applyEvent(live, { type: "trace", event: { step: "route", verdict: "agent", detail: "" } });
    live = applyEvent(live, { type: "interrupt", id: "i1", kind: "memory", payload: { text: "x" } });
    expect(live.steps).toEqual([{ label: "log_transaction", error: false }]);
    expect(live.trace).toHaveLength(1);
    expect(live.interrupt).toEqual({ id: "i1", kind: "memory", payload: { text: "x" } });
  });
  it("ignores lines it does not draw", () => {
    const live = freshLive(1);
    expect(applyEvent(live, { type: "tick" })).toBe(live);
    expect(applyEvent(live, { type: "node", node: "route" })).toBe(live);
  });
});

describe("workingLine", () => {
  it("says what is happening in plain words", () => {
    const live = freshLive(1);
    expect(workingLine(live)).toBe("Starting…");
    expect(workingLine(applyEvent(live, { type: "trace", event: { step: "route", verdict: "knowledge", detail: "" } }))).toBe("Sending this to: Looked it up");
    expect(workingLine({ ...live, text: "Hi" })).toBe("Writing the answer…");
    expect(workingLine({ ...live, stopping: true })).toBe("Stopping…");
  });
});

describe("earlier answers", () => {
  const alts: Alternative[] = ["a", "b", "c"].map((id) => ({ id, text: id, ts: null, route: null }));
  it("offset 0 is the newest; larger offsets walk back", () => {
    expect(shownAlternative(alts, 0).alt?.id).toBe("c");
    expect(shownAlternative(alts, 1)).toMatchObject({ index: 1, total: 3 });
    expect(shownAlternative(alts, 99).alt?.id).toBe("a");
  });
  it("needs at least two answers", () => {
    expect(shownAlternative(alts.slice(0, 1), 0).alt).toBeNull();
  });
  it("finds the last reply", () => {
    expect(lastAssistantIndex([{ id: null, role: "user", text: "x", ts: null }])).toBe(-1);
  });
});
