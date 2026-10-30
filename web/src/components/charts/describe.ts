import type { Tone } from "../ui";
import type { DrawEvent, TraceRow } from "../../api/charts";

type Progress = Extract<DrawEvent, { type: "progress" }>;

/** One live line for the working state. The mark (✓ or ✗) and the words carry the meaning, never colour alone. */
export function describeProgress(e: Pick<Progress, "step" | "verdict" | "detail" | "attempt">): { text: string; state: "working" | "ok" | "bad" | "wait" } {
  const { step, verdict, detail } = e;
  const reason = detail ? `: ${detail}` : "";
  if (step === "prepare") return { text: detail || "Getting your entries ready", state: "ok" };
  if (step === "write") return verdict === "working" ? { text: "Writing code…", state: "working" } : { text: "Writing code", state: "ok" };
  if (step === "repair") return verdict === "working" ? { text: `Fixing the code (try ${e.attempt})…`, state: "working" } : { text: `Wrote a new version (try ${e.attempt})`, state: "ok" };
  if (step === "validate") {
    if (verdict === "working") return { text: "Checking the code…", state: "working" };
    return verdict === "PASSED" ? { text: "Checking the code", state: "ok" } : { text: `The code was refused${reason}; fixing`, state: "bad" };
  }
  if (step === "review") {
    if (verdict === "waiting") return { text: "Waiting for you to read the code", state: "wait" };
    return { text: detail || "Reviewed", state: verdict === "CANCELLED" ? "bad" : "ok" };
  }
  if (step === "run") {
    if (verdict === "working") return { text: "Running…", state: "working" };
    if (verdict === "RAN") return { text: "Running", state: "ok" };
    return { text: verdict === "TIMEOUT" ? `It took too long${reason}; fixing` : `It crashed${reason}; fixing`, state: "bad" };
  }
  if (step === "check") {
    if (verdict === "working") return { text: "Checking the figure…", state: "working" };
    return verdict === "PASSED" ? { text: "Checking the figure", state: "ok" } : { text: `The figure didn't pass${reason}; fixing`, state: "bad" };
  }
  if (step === "give_up") return { text: detail || "Gave up", state: "bad" };
  if (step === "done") return { text: "Saved", state: "ok" };
  return { text: detail || step, state: "ok" };
}

/** The stamp word and tone for a row of "How it got here". */
export function stampFor(row: Pick<TraceRow, "step" | "verdict">): { word: string; tone: Tone } | null {
  const v = row.verdict;
  if (v === "VALIDATION FAILED") return { word: "Validation failed", tone: "over" };
  if (v === "RAN") return { word: "Ran", tone: "neutral" };
  if (v === "ERROR") return { word: "Error", tone: "over" };
  if (v === "TIMEOUT") return { word: "Too slow", tone: "over" };
  if (v === "CHECK FAILED") return { word: "Check failed", tone: "over" };
  if (v === "PASSED") return row.step === "done" ? null : { word: "Passed", tone: "ok" };
  if (v === "APPROVED") return { word: "Approved", tone: "ok" };
  if (v === "EDITED") return { word: "Edited", tone: "near" };
  if (v === "CANCELLED" || v === "FAILED") return { word: v === "FAILED" ? "Gave up" : "Cancelled", tone: "near" };
  return null;
}

/** Rows worth showing in the "How it got here" list (the step-by-step lines already shown while drawing are not repeated as 'working'). */
export function meaningfulTrace(trace: TraceRow[]): TraceRow[] {
  return trace.filter((r) => r.verdict !== "working" && r.step !== "prepare" && !(r.step === "write" && r.verdict === "ok") && !(r.step === "repair" && r.verdict === "ok") && r.step !== "done");
}

export const PERIODS = [
  { value: "last-3-months", label: "Last 3 months" },
  { value: "this-month", label: "This month" },
  { value: "last-month", label: "Last month" },
  { value: "last-30-days", label: "Last 30 days" },
  { value: "last-12-months", label: "Last 12 months" },
  { value: "all", label: "All time" },
] as const;

export const EXAMPLES = [
  { label: "Monthly food spend, bar", request: "Monthly food spend as a bar chart", period: "last-3-months" },
  { label: "Last month by category", request: "Last month's spending by category as bars", period: "last-month" },
  { label: "Daily spending vs budget pace", request: "Daily spending this month against my budget", period: "this-month" },
] as const;
