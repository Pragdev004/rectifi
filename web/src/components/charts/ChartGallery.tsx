import { RefreshCw } from "lucide-react";
import type { ChartSummary } from "../../api/charts";
import { Button } from "../forms";
import { EmptyState, Stamp } from "../ui";
import styles from "./charts.module.css";

function updated(c: ChartSummary): string {
  const raw = c.refreshed_at ?? c.created_at;
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return "";
  return `updated ${d.toLocaleDateString("en-IN", { day: "numeric", month: "short" })}`;
}

/** Saved charts as a ruled list (not cards): thumbnail, title, the request in muted text, a PINNED stamp and the actions. */
export function ChartGallery({ items, busyId, onOpen, onRerun }: { items: ChartSummary[]; busyId: number | null; onOpen: (id: number) => void; onRerun: (id: number) => void }) {
  if (items.length === 0) return <EmptyState title="No saved charts yet.">Draw one above and pin it.</EmptyState>;
  return (
    <ul className={styles.gallery} data-testid="chart-gallery">
      {items.map((c) => (
        <li key={c.id} className={styles.gRow}>
          {c.thumb ? <img className={styles.thumb} src={`data:image/png;base64,${c.thumb}`} alt="" /> : <div className={styles.thumb} aria-hidden="true" />}
          <div className={styles.gText}>
            <p className={styles.gTitle}>{c.title}</p>
            <p className={styles.gPrompt}>{c.prompt}</p>
            <p className={styles.gMeta}>{c.pinned && <Stamp tone="accent">Pinned</Stamp>}<span>{updated(c)}</span></p>
          </div>
          <div className={styles.gActions}>
            <Button variant="secondary" onClick={() => onOpen(c.id)}>Open</Button>
            <Button variant="tertiary" icon={<RefreshCw size={16} strokeWidth={1.5} aria-hidden="true" />} loading={busyId === c.id} loadingLabel="Drawing…" onClick={() => onRerun(c.id)}>Run again</Button>
          </div>
        </li>
      ))}
    </ul>
  );
}
