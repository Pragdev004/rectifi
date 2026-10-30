import { Check, Loader, Pause, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { DrawEvent } from "../../api/charts";
import { cx } from "../../lib/cx";
import { describeProgress } from "./describe";
import styles from "./charts.module.css";

type Progress = Extract<DrawEvent, { type: "progress" }>;

/** The live lines while a chart is being drawn. A polite live region, so a screen reader hears each step as it lands. */
export function ChartProgress({ events, startedAt }: { events: Progress[]; startedAt: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  // Keep the last event per step+attempt, so "Writing code…" turns into "Writing code ✓" in place.
  const lines: { key: string; e: Progress }[] = [];
  for (const e of events) {
    const key = `${e.step}-${e.attempt}`;
    const at = lines.findIndex((l) => l.key === key);
    if (at >= 0) lines[at] = { key, e };
    else lines.push({ key, e });
  }
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  return (
    <div role="status" aria-live="polite" data-testid="chart-progress">
      <ul className={styles.progress}>
        {lines.map(({ key, e }) => {
          const d = describeProgress(e);
          return (
            <li key={key} className={cx(d.state === "working" && styles.working, d.state === "bad" && styles.bad)}>
              <span className={styles.mark} aria-hidden="true">
                {d.state === "ok" && <Check size={16} strokeWidth={1.5} />}
                {d.state === "bad" && <X size={16} strokeWidth={1.5} />}
                {d.state === "working" && <Loader size={16} strokeWidth={1.5} />}
                {d.state === "wait" && <Pause size={16} strokeWidth={1.5} />}
              </span>
              <span>{d.text}</span>
            </li>
          );
        })}
      </ul>
      <p className={styles.elapsed}>{seconds}s</p>
    </div>
  );
}
