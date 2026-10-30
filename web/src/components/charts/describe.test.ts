import { describe, expect, it } from "vitest";
import { describeProgress, meaningfulTrace, stampFor } from "./describe";

describe("describeProgress", () => {
  it("says what is happening in words, with the reason when a step fails", () => {
    expect(describeProgress({ step: "write", verdict: "working", detail: "", attempt: 1 })).toEqual({ text: "Writing code…", state: "working" });
    expect(describeProgress({ step: "validate", verdict: "PASSED", detail: "", attempt: 1 }).state).toBe("ok");
    const bad = describeProgress({ step: "check", verdict: "CHECK FAILED", detail: "the title says ₹4,200, which isn't in your data", attempt: 1 });
    expect(bad.state).toBe("bad");
    expect(bad.text).toContain("₹4,200");
    expect(bad.text).toContain("fixing");
  });
  it("tells a crash from a timeout", () => {
    expect(describeProgress({ step: "run", verdict: "ERROR", detail: "KeyError", attempt: 1 }).text).toContain("crashed");
    expect(describeProgress({ step: "run", verdict: "TIMEOUT", detail: "", attempt: 1 }).text).toContain("too long");
  });
});

describe("stamps and the trace list", () => {
  it("gives every failure a word", () => {
    for (const verdict of ["VALIDATION FAILED", "ERROR", "TIMEOUT", "CHECK FAILED"]) {
      expect(stampFor({ step: "x", verdict })?.tone).toBe("over");
    }
    expect(stampFor({ step: "check", verdict: "PASSED" })).toEqual({ word: "Passed", tone: "ok" });
    expect(stampFor({ step: "write", verdict: "ok" })).toBeNull();
  });
  it("hides the bookkeeping rows", () => {
    const rows = [
      { attempt: 0, step: "prepare", verdict: "ok", detail: "" },
      { attempt: 1, step: "write", verdict: "ok", detail: "" },
      { attempt: 1, step: "validate", verdict: "VALIDATION FAILED", detail: "x" },
      { attempt: 2, step: "repair", verdict: "ok", detail: "" },
      { attempt: 2, step: "run", verdict: "RAN", detail: "" },
      { attempt: 2, step: "done", verdict: "PASSED", detail: "" },
    ];
    expect(meaningfulTrace(rows).map((r) => r.verdict)).toEqual(["VALIDATION FAILED", "RAN"]);
  });
});
