import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { applyToSimilar, jobIsActive, startSorting, useBatchRows, type BatchSummary, type ClassifyJob, type Show, type SortedTxn } from "../../api/classify";
import { patch } from "../../api/client";
import { refreshBooks, useMeta } from "../../api/queries";
import type { EditResult } from "../../api/types";
import { fmtShort } from "../../lib/dates";
import { Button, Select, SegmentedControl } from "../forms";
import { categoriesFor } from "../ledger/useEditTxn";
import { payeeOf, TxnAmount } from "../LedgerList";
import { SourceStamp } from "../SourceStamp";
import { Callout, Eyebrow } from "../ui";
import styles from "./ImportCategories.module.css";

const FILTERS: { value: Show; label: string }[] = [
  { value: "all", label: "All" },
  { value: "misc", label: "Miscellaneous" },
  { value: "model", label: "Decided by the model" },
  { value: "suggested", label: "Suggestions" },
];

/** "312 entries · 241 by rules · 38 like your past choices · 20 by the local model · 13 in Miscellaneous". Zero parts are left out. */
export function summaryLine(s: BatchSummary): string {
  const by = s.by_source;
  const rules = (by.rule ?? 0) + (by.heuristic ?? 0) + (by.learned ?? 0);
  const parts: [number, string][] = [
    [s.total, s.total === 1 ? "entry" : "entries"],
    [rules, "by rules"],
    [by.user ?? 0, "chosen by you"],
    [by.retrieval ?? 0, "like your past choices"],
    [by.llm ?? 0, "by the local model"],
    [s.miscellaneous, "in Miscellaneous"],
  ];
  return parts.filter(([n]) => n > 0).map(([n, w]) => `${n} ${w}`).join(" · ");
}

export function jobLine(job: ClassifyJob | null, waiting: number): { text: string; tone: "note" | "near" | "ok" } | null {
  if (!job) return null;
  if (job.status === "queued" || job.status === "running") {
    return { text: `Sorting the rest with the local model… ${job.done} of ${job.total}`, tone: "note" };
  }
  if (job.status === "no_model") {
    return { text: `Model is off. ${waiting} ${waiting === 1 ? "entry waits" : "entries wait"} in Miscellaneous.`, tone: "near" };
  }
  if (job.status === "failed") return { text: job.error ?? "The model could not sort these.", tone: "near" };
  if (job.status === "cancelled") return null;
  return { text: "The local model has looked at the rest.", tone: "ok" };
}

function RowItem({ t, batchId }: { t: SortedTxn; batchId: number }) {
  const meta = useMeta();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [taught, setTaught] = useState<{ rule: string | null; category: string } | null>(null);
  const [spread, setSpread] = useState<number | null>(null);
  const name = payeeOf(t);

  async function choose(category: string) {
    setBusy(true);
    setError(null);
    try {
      const r = await patch<EditResult>(`/transactions/${t.id}`, { category });
      setTaught({ rule: r.learned_rule, category: r.transaction.category ?? category });
      setSpread(null);
      await client.invalidateQueries({ queryKey: ["transactions", "import-rows", batchId] });
      refreshBooks(client);
    } catch (e) {
      setError(e instanceof Error ? e.message : "That change was not saved.");
    } finally {
      setBusy(false);
    }
  }

  async function spreadIt() {
    setBusy(true);
    try {
      const r = await applyToSimilar(t.id);
      setSpread(r.changed);
      await client.invalidateQueries({ queryKey: ["transactions", "import-rows", batchId] });
      refreshBooks(client);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not apply that.");
    } finally {
      setBusy(false);
    }
  }

  const options = meta.data ? categoriesFor(t, meta.data.categories) : [];
  const suggestion = t.suggested_category && t.category_source === "fallback" ? t : null;
  return (
    <li className={styles.row} data-testid="import-row" data-txn-id={t.id}>
      <span className={styles.date}>{fmtShort(t.date)}</span>
      <span className={styles.payee}>
        <strong>{name}</strong>
        {t.description && t.description !== t.payee && <small>{t.description}</small>}
      </span>
      <span className={styles.amount}><TxnAmount t={t} /></span>
      <span className={styles.cat}>
        <Select
          aria-label={`Category for ${name}, ${fmtShort(t.date)}`}
          value={t.category ?? ""}
          disabled={busy || !meta.data}
          onChange={(e) => void choose(e.target.value)}
        >
          {t.category === null && <option value="" disabled>Uncategorised</option>}
          {options.map((c) => <option key={c.id} value={c.name}>{c.name}</option>)}
        </Select>
      </span>
      <span className={styles.source}>
        <SourceStamp source={t.category_source} confidence={t.category_confidence} />
      </span>
      <div className={styles.under}>
        {suggestion && (
          <p className={styles.suggest}>
            Maybe {suggestion.suggested_category} ({suggestion.suggested_confidence}%) {"· "}
            <button type="button" className={styles.link} disabled={busy} onClick={() => void choose(suggestion.suggested_category ?? "")}>
              Accept<span className="sr-only"> {suggestion.suggested_category} for {name}</span>
            </button>
          </p>
        )}
        {t.category_reason && (
          <details className={styles.why}>
            <summary>Why</summary>
            <p>{t.category_reason}</p>
          </details>
        )}
        {taught && (
          <p className={styles.saved} role="status">
            Saved.{taught.rule ? ` Rectifi will use ${taught.category} for “${taught.rule}” from now on.` : ""}{" "}
            {spread === null ? (
              <button type="button" className={styles.link} disabled={busy} onClick={() => void spreadIt()}>Apply to similar entries</button>
            ) : (
              <span>{spread === 0 ? "No other entries needed changing." : `Changed ${spread} similar ${spread === 1 ? "entry" : "entries"}.`}</span>
            )}
          </p>
        )}
        {error && <p className={styles.error} role="alert">{error}</p>}
      </div>
    </li>
  );
}

/** Under the import slip: how every new entry got its category, a way to fix any of them, and the model's progress. */
export function ImportCategories({ batchId }: { batchId: number }) {
  const client = useQueryClient();
  const [show, setShow] = useState<Show>("all");
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const rows = useBatchRows(batchId, show);
  const data = rows.data;

  async function sortNow() {
    setStarting(true);
    setStartError(null);
    try {
      await startSorting(batchId);
      await client.invalidateQueries({ queryKey: ["transactions", "import-rows", batchId] });
    } catch (e) {
      setStartError(e instanceof Error ? e.message : "Could not start sorting.");
    } finally {
      setStarting(false);
    }
  }

  if (rows.isError) return <Callout tone="near" role="alert">Could not load the categories for this import.</Callout>;
  if (!data) return <p className={styles.help} role="status">Loading categories…</p>;
  if (data.summary.total === 0) return null;
  const job = data.job;
  const line = jobLine(job, data.summary.miscellaneous);
  const canSort = job?.status === "no_model" || job?.status === "failed";

  return (
    <section className={styles.section} aria-labelledby="import-cats" data-testid="import-categories">
      <Eyebrow as="h3" className={styles.eyebrow}><span id="import-cats">Categories</span></Eyebrow>
      <p className={styles.summary} data-testid="import-summary">{summaryLine(data.summary)}</p>
      {line && (
        <div className={styles.progress} role="status" aria-live="polite" data-tone={line.tone}>
          <span>{line.text}</span>
          {jobIsActive(job) && job && job.total > 0 && <progress max={job.total} value={job.done} aria-label="Entries sorted by the model" />}
          {canSort && (
            <Button variant="secondary" loading={starting} loadingLabel="Starting…" onClick={() => void sortNow()}>
              Sort them when the model is on
            </Button>
          )}
        </div>
      )}
      {startError && <p className={styles.error} role="alert">{startError}</p>}
      <SegmentedControl legend="Show entries" name="import-show" options={FILTERS} value={show} onChange={setShow} />
      {data.items.length === 0 ? (
        <p className={styles.help}>Nothing here.</p>
      ) : (
        <ul className={styles.list}>{data.items.map((t) => <RowItem key={t.id} t={t} batchId={batchId} />)}</ul>
      )}
    </section>
  );
}
