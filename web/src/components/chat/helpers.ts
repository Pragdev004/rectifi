// Small pure pieces of the chat window, kept apart so they can be tested without a browser.
import type { Alternative, ChatEvent, ChatItem, Interrupt, Thread, TraceEvent } from "../../api/chat";

export type Group = "Today" | "This week" | "Earlier";
const DAY = 24 * 60 * 60 * 1000;

function startOfDay(t: number): number {
  const d = new Date(t);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/** Sidebar groups by when the conversation last changed. Keeps the server's order (newest first) inside each group. */
export function groupThreads(threads: Thread[], now: number = Date.now()): { group: Group; threads: Thread[] }[] {
  const today = startOfDay(now);
  const buckets: Record<Group, Thread[]> = { Today: [], "This week": [], Earlier: [] };
  for (const t of threads) {
    const at = new Date(t.updated_at).getTime();
    if (Number.isNaN(at) || at < today - 6 * DAY) buckets.Earlier.push(t);
    else if (at >= today) buckets.Today.push(t);
    else buckets["This week"].push(t);
  }
  return (Object.keys(buckets) as Group[]).filter((g) => buckets[g].length > 0).map((g) => ({ group: g, threads: buckets[g] }));
}

const HM = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
const DM = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short" });

/** "14:02" for today, "24 Oct, 14:02" for any other day; "" when there is no time. */
export function msgTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = HM.format(d);
  if (startOfDay(d.getTime()) === startOfDay(now)) return time;
  return `${DM.format(d).replace(/Sept\b/, "Sep")}, ${time}`;
}

export const ROUTE_STAMP: Record<string, string> = {
  agent: "Did it",
  knowledge: "Looked it up",
  chart: "Chart",
  smalltalk: "Chat",
  remember: "Memory",
};

/** What the page is showing while a turn runs. */
export interface Live {
  startedAt: number;
  text: string;
  textId: string | null;
  steps: { label: string; error: boolean }[];
  trace: TraceEvent[];
  interrupt: Interrupt | null;
  stopping: boolean;
}

export const freshLive = (now: number = Date.now()): Live => ({ startedAt: now, text: "", textId: null, steps: [], trace: [], interrupt: null, stopping: false });

/** One streamed line to the new "live" state. `done` and `error` are handled by the page (they replace the transcript). */
export function applyEvent(live: Live, event: ChatEvent): Live {
  switch (event.type) {
    case "token":
      // a new model call (a different message id) starts a new answer: the previous text was a step, not the reply
      return event.id !== live.textId ? { ...live, textId: event.id, text: event.text } : { ...live, text: live.text + event.text };
    case "step":
      return { ...live, steps: [...live.steps, { label: event.tool, error: event.error }] };
    case "trace":
      return { ...live, trace: [...live.trace, event.event] };
    case "interrupt":
      return { ...live, interrupt: { id: event.id, kind: event.kind, payload: event.payload } };
    default:
      return live;
  }
}

/** The plain sentence for the line under "Rectifi is working". */
export function workingLine(live: Live): string {
  if (live.stopping) return "Stopping…";
  const last = live.trace[live.trace.length - 1];
  if (live.text) return "Writing the answer…";
  if (!last) return "Starting…";
  if (last.step === "route") return `Sending this to: ${ROUTE_STAMP[last.verdict] ?? last.verdict}`;
  return `${last.step}: ${last.verdict}`;
}

/** Which answer is on screen for the last reply: the newest, or an earlier one chosen with the arrows. */
export function shownAlternative(alts: Alternative[], offset: number): { index: number; total: number; alt: Alternative | null } {
  if (alts.length < 2) return { index: 0, total: alts.length, alt: null };
  const index = Math.min(Math.max(alts.length - 1 - offset, 0), alts.length - 1);
  return { index, total: alts.length, alt: alts[index] ?? null };
}

export function lastAssistantIndex(items: ChatItem[]): number {
  for (let i = items.length - 1; i >= 0; i--) if (items[i]?.role === "assistant") return i;
  return -1;
}

export const MAX_TEXT = 4000;
