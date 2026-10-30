import { ChevronLeft, ChevronRight, RotateCcw, TriangleAlert } from "lucide-react";
import type { ChatItem } from "../../api/chat";
import { Markdown } from "../../lib/markdown";
import { formatInr } from "../../lib/money";
import { LinkButton, Button, IconButton } from "../forms";
import { Citations } from "../rag/Citations";
import { ReflectionTrace } from "../rag/ReflectionTrace";
import { Disclosure, Eyebrow, Stamp } from "../ui";
import { ROUTE_STAMP, msgTime, type Live, workingLine } from "./helpers";
import { useElapsed } from "./useElapsed";
import c from "./chat.module.css";

type Assistant = Extract<ChatItem, { role: "assistant" }>;

function Steps({ steps }: { steps: Assistant["steps"] }) {
  if (steps.length === 0) return null;
  return (
    <Disclosure summary={`What Rectifi did · ${steps.length} ${steps.length === 1 ? "step" : "steps"}`}>
      <ul className={c.steps} data-testid="ask-steps">
        {steps.map((s, i) => (
          <li key={i}>
            <p className={c.stepHead}><code>{s.tool}</code>{s.error && <Stamp tone="over">Refused</Stamp>}</p>
            <pre className={c.stepOut}>{s.output}</pre>
          </li>
        ))}
      </ul>
    </Disclosure>
  );
}

export function UserTurn({ item }: { item: Extract<ChatItem, { role: "user" }> }) {
  return (
    <div className={c.turn} data-role="user">
      <div className={c.meta}><Eyebrow>You</Eyebrow><time className={c.time} dateTime={item.ts ?? undefined}>{msgTime(item.ts)}</time></div>
      <p className={c.user}>{item.text}</p>
    </div>
  );
}

export function AssistantTurn({
  item, last, busy, earlier, alternatives, onAnswerAgain, onAlternative,
}: {
  item: Assistant;
  last: boolean;
  busy: boolean;
  /** Showing an earlier answer instead of this item's own text. */
  earlier?: { text: string; ts: string | null; index: number; total: number } | null;
  alternatives: number;
  onAnswerAgain: () => void;
  onAlternative: (delta: number) => void;
}) {
  const text = earlier ? earlier.text : item.text;
  const ts = earlier ? earlier.ts : item.ts;
  return (
    <div className={c.turn} data-role="assistant" data-route={item.route ?? undefined}>
      <div className={c.meta}>
        <Eyebrow>Rectifi</Eyebrow>
        {item.route && <Stamp>{ROUTE_STAMP[item.route] ?? item.route}</Stamp>}
        {item.stopped && !earlier && <Stamp tone="near">Stopped</Stamp>}
        {earlier && <Stamp tone="accent">Earlier answer</Stamp>}
        <time className={c.time} dateTime={ts ?? undefined}>{msgTime(ts)}</time>
      </div>
      <Markdown text={text} className={earlier ? `${c.md} ${c.earlier}` : c.md} />
      {!earlier && item.unverified.length > 0 && (
        <p className={c.unverified} data-testid="not-verified">
          <TriangleAlert size={16} strokeWidth={1.5} aria-hidden="true" />
          <span>
            <strong>Not verified: {item.unverified.map((v) => formatInr(v)).join(", ")}.</strong>{" "}
            I can{"’"}t match this to anything you typed or to a number Rectifi worked out, so check it before you rely on it.
          </span>
        </p>
      )}
      {!earlier && item.artifact && (
        <div className={c.slip} data-testid="chart-slip">
          <p><Stamp>Chart</Stamp> Saved as chart {item.artifact.chart_id}.</p>
          <LinkButton to={`/charts?chart=${item.artifact.chart_id}`}>Open in Charts</LinkButton>
        </div>
      )}
      {!earlier && <Citations citations={item.citations} idPrefix={`source-${item.id ?? "m"}`} />}
      {!earlier && <ReflectionTrace trace={[...item.trace, ...item.checks.filter((e) => e.step !== "turn").map((e) => ({ ...e, step: e.step.toUpperCase() }))]} />}
      {!earlier && <Steps steps={item.steps} />}
      {last && (
        <div className={c.afterReply}>
          <Button variant="tertiary" disabled={busy} icon={<RotateCcw size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={onAnswerAgain}>Answer again</Button>
          {alternatives > 1 && (
            <span className={c.alts} role="group" aria-label="Earlier answers">
              <IconButton label="Older answer" disabled={earlier ? earlier.index === 0 : false} onClick={() => onAlternative(1)}><ChevronLeft size={18} strokeWidth={1.5} aria-hidden="true" /></IconButton>
              <span>{earlier ? `Earlier answer (${earlier.index + 1} of ${earlier.total})` : `Latest answer (${alternatives} of ${alternatives})`}</span>
              <IconButton label="Newer answer" disabled={!earlier} onClick={() => onAlternative(-1)}><ChevronRight size={18} strokeWidth={1.5} aria-hidden="true" /></IconButton>
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/** The reply being written right now: the streamed words in place, or what Rectifi is doing until the first word. */
export function LiveTurn({ live }: { live: Live }) {
  const secs = useElapsed(live.startedAt);
  return (
    <div className={c.turn} data-role="assistant-live" data-testid="ask-working">
      <div className={c.meta}><Eyebrow>Rectifi</Eyebrow></div>
      {live.text && <Markdown text={live.text} className={c.md} />}
      <div className={c.working} role="status" aria-live="polite">
        <p>{workingLine(live)}{secs >= 5 && <span className={c.elapsed}> {secs} s</span>}</p>
        {live.steps.length > 0 && <ul>{live.steps.map((s, i) => <li key={i}>{s.error ? "✗" : "✓"} <code>{s.label}</code></li>)}</ul>}
      </div>
    </div>
  );
}
