import { Pin, PinOff, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import type { ChartDetail, TraceRow } from "../../api/charts";
import { Button } from "../forms";
import { ConfirmDialog } from "../Dialog";
import { Disclosure, Eyebrow, Stamp } from "../ui";
import { ChartPicture } from "./ChartPicture";
import { meaningfulTrace, stampFor } from "./describe";
import styles from "./charts.module.css";

const CHECK_WORDS: Record<string, string> = { has_data: "has data", labelled: "labelled", figures_match: "figures match Rectifi's totals", enough_points: "enough points", no_unasked_pie: "no unasked pie", glyphs: "every symbol drawn", image_size: "normal image size" };

function caption(c: ChartDetail): string {
  const range = c.period_start && c.period_end ? `, ${c.period_start} to ${c.period_end}` : "";
  const checks = c.checks.filter((k) => k.ok).map((k) => CHECK_WORDS[k.name] ?? k.name).join(" · ");
  return `Drawn by chart code from ${c.entries} ${c.entries === 1 ? "entry" : "entries"}${range}. Checked: ${checks || "nothing yet"}`;
}

function CodeBlock({ code }: { code: string }) {
  const lines = code.replace(/\n$/, "").split("\n");
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(code); setCopied(true); window.setTimeout(() => setCopied(false), 2000); } catch { /* clipboard blocked: the code is selectable */ }
  };
  return (
    <>
      <div className={styles.code} data-testid="chart-code">
        <ol aria-hidden="true">{lines.map((_, i) => <li key={i}>{i + 1}</li>)}</ol>
        <pre><code>{code}</code></pre>
      </div>
      <div className={styles.copyRow}><Button variant="tertiary" onClick={() => void copy()} aria-label="Copy chart code"><span aria-live="polite">{copied ? "Copied" : "Copy code"}</span></Button></div>
    </>
  );
}

function HowItGotHere({ trace, attempts, onRetry, retryBusy }: { trace: TraceRow[]; attempts: number; onRetry?: (attempt: number) => void; retryBusy?: boolean }) {
  const rows = meaningfulTrace(trace);
  if (rows.length === 0) return null;
  return (
    <Disclosure summary={`How it got here · ${attempts} ${attempts === 1 ? "attempt" : "attempts"}`}>
      <ol className={styles.trace} data-testid="chart-trace">
        {rows.map((r, i) => {
          const stamp = stampFor(r);
          return (
            <li key={i}>
              <div className={styles.traceHead}>
                <span className={styles.attempt}>Try {r.attempt || attempts}</span>
                {stamp && <Stamp tone={stamp.tone}>{stamp.word}</Stamp>}
              </div>
              {r.detail && <p className={styles.reason}>{r.detail}</p>}
              {r.traceback && <details><summary>Where it stopped</summary><pre className={styles.tb}>{r.traceback}</pre></details>}
              {onRetry && r.attempt >= 1 && stamp?.tone === "over" && (
                <div><Button variant="tertiary" disabled={retryBusy} onClick={() => onRetry(r.attempt)}>Try again from here</Button></div>
              )}
            </li>
          );
        })}
      </ol>
    </Disclosure>
  );
}

/** A finished chart: the picture, what was checked, and the actions. */
export function ChartSlip({ chart, trace, busy, onPin, onRerun, onDelete, onRetry }: {
  chart: ChartDetail;
  trace?: TraceRow[];
  busy?: boolean;
  onPin: (pinned: boolean) => void;
  onRerun: () => void;
  onDelete: () => void;
  onRetry?: (attempt: number) => void;
}) {
  const [confirm, setConfirm] = useState(false);
  return (
    <article className={styles.slip} aria-labelledby={`chart-${chart.id}`} data-testid="chart-slip">
      <header>
        <Eyebrow>{chart.pinned ? "Saved chart · Pinned" : "Your chart"}</Eyebrow>
        <h2 id={`chart-${chart.id}`}>{chart.title}</h2>
      </header>
      <ChartPicture light={chart.image_light} dark={chart.image_dark} alt={chart.alt} caption={caption(chart)} />
      <div className={styles.actions}>
        <Button variant={chart.pinned ? "secondary" : "primary"} icon={chart.pinned ? <PinOff size={16} strokeWidth={1.5} aria-hidden="true" /> : <Pin size={16} strokeWidth={1.5} aria-hidden="true" />}
          disabled={busy} onClick={() => onPin(!chart.pinned)}>{chart.pinned ? "Unpin" : "Pin to gallery"}</Button>
        <Button variant="secondary" icon={<RefreshCw size={16} strokeWidth={1.5} aria-hidden="true" />} loading={busy} loadingLabel="Drawing…" onClick={onRerun}>Run again with latest data</Button>
        <Button variant="dangerQuiet" icon={<Trash2 size={16} strokeWidth={1.5} aria-hidden="true" />} disabled={busy} onClick={() => setConfirm(true)}>Delete</Button>
      </div>
      <Disclosure summary="Show code"><CodeBlock code={chart.code} /></Disclosure>
      <HowItGotHere trace={trace ?? chart.trace} attempts={chart.attempts} onRetry={onRetry} />
      <ConfirmDialog open={confirm} title="Delete this chart?" confirmLabel="Delete" onClose={() => setConfirm(false)} onConfirm={() => { setConfirm(false); onDelete(); }}>
        <p>"{chart.title}" will be removed from your saved charts. Your entries are not touched.</p>
      </ConfirmDialog>
    </article>
  );
}

export { HowItGotHere };
