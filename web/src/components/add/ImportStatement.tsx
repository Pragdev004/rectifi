import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Undo2, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError, post, request } from "../../api/client";
import { refreshBooks, useMeta } from "../../api/queries";
import type { ImportedOutcome, ImportOutcome, Mapping, Preview, PreviewOutcome, UndoResult } from "../../api/types";
import { fmtShort } from "../../lib/dates";
import { Button, Checkbox, Field, FileDrop, LinkButton, Select, TextInput } from "../forms";
import { Money } from "../Money";
import { ReconSlip } from "../ReconSlip";
import { useToast } from "../Toast";
import { useIsDesktop } from "../../lib/media";
import { Callout, Disclosure, Eyebrow, Stamp } from "../ui";
import styles from "./add.module.css";

const MAX_BYTES = 10 * 1024 * 1024;
export const IMPORT_HELP =
  "Download a CSV, Excel or PDF statement from net banking. Rectifi finds the columns and, when they are clear, imports at once: " +
  "it skips rows it already has, merges entries you logged by hand, and uses the balance column to prove nothing is missing. " +
  "If anything is unclear it shows you the columns first instead of guessing. PDFs are read on this laptop only; " +
  "a PDF that is a scan or a photo can't be read.";

const HOW: Record<NonNullable<ImportedOutcome["how"]>, string> = {
  digits: "matched by the account digits in the statement",
  chosen: "chosen by you",
  most_recent: "the statement does not say which account it is, so this is your most recently used account",
  only: "your only account",
};

const ROLES = ["date", "narration", "debit", "credit", "amount", "type", "balance", "ref"] as const;

type Phase =
  | { kind: "idle" }
  | { kind: "uploading" }
  | { kind: "password"; message: string | null }
  | { kind: "error"; message: string }
  | { kind: "result"; outcome: ImportOutcome };

export function ImportStatement() {
  const meta = useMeta();
  const desktop = useIsDesktop();
  const client = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [manual, setManual] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [password, setPassword] = useState("");
  const [undone, setUndone] = useState<{ removed: number; added: number } | null>(null);
  const live = useRef(0); // ignores an answer that arrives after a newer upload

  async function upload(f: File, opts: { password?: string; manual: boolean }) {
    if (f.size > MAX_BYTES) {
      setPhase({ kind: "error", message: "That file is larger than 10 MB." });
      return;
    }
    const ticket = ++live.current;
    setUndone(null);
    setPhase({ kind: "uploading" });
    const form = new FormData();
    form.append("file", f);
    if (opts.password) form.append("password", opts.password);
    if (opts.manual) form.append("manual", "1");
    try {
      const outcome = await request<ImportOutcome>("POST", "/imports", { form });
      if (ticket !== live.current) return;
      setPhase({ kind: "result", outcome });
      if (outcome.outcome !== "preview") refreshBooks(client);
    } catch (e) {
      if (ticket !== live.current) return;
      if (e instanceof ApiError && (e.code === "pdf_password_needed" || e.code === "pdf_password_wrong")) {
        setPhase({ kind: "password", message: e.code === "pdf_password_wrong" ? e.message : null });
      } else {
        setPhase({ kind: "error", message: e instanceof Error ? e.message : "That file could not be read." });
      }
    }
  }

  function choose(f: File) {
    setFile(f);
    setPassword("");
    void upload(f, { manual });
  }

  function openPdf() {
    if (!file) return;
    const typed = password;
    setPassword("");                                   // the password leaves the page's state the moment it is sent
    void upload(file, { password: typed, manual });
  }

  // Unmounting forgets any typed password.
  useEffect(() => () => setPassword(""), []);

  const busy = phase.kind === "uploading";
  const noAccounts = (meta.data?.accounts.length ?? 0) === 0 && meta.data !== undefined;
  const settled = phase.kind === "result" && phase.outcome.outcome !== "preview";

  return (
    <div className={styles.panel}>
      {desktop ? <p className={styles.help}>{IMPORT_HELP}</p> : <Disclosure summary="How importing works"><p className={styles.help}>{IMPORT_HELP}</p></Disclosure>}
      {noAccounts && (
        <Callout tone="note" lead="No account yet." actions={<LinkButton to="/accounts" variant="secondary">Add an account</LinkButton>}>
          Add the bank account first so imported rows know where they belong.
        </Callout>
      )}
      <Checkbox
        label="Let me check the columns and choose the account myself (don't import automatically)"
        checked={manual}
        disabled={busy}
        onChange={(v) => {
          setManual(v);
          if (file && !settled && phase.kind !== "password") void upload(file, { manual: v });
        }}
      />
      <FileDrop file={file} onFile={choose} accept=".csv,.xlsx,.xls,.pdf" busy={busy} label="Statement file" />
      {busy && <p role="status" className={styles.help}>Reading statement…</p>}
      {phase.kind === "error" && <Callout tone="over" role="alert">{phase.message}</Callout>}
      {phase.kind === "password" && (
        <form
          onSubmit={(e) => { e.preventDefault(); openPdf(); }}
          className={styles.slip}
          aria-label="PDF password"
        >
          <Callout tone={phase.message ? "over" : "note"} lead={phase.message ? undefined : "This PDF is locked."} role={phase.message ? "alert" : undefined}>
            {phase.message ?? "Rectifi needs the password to read it. It is used once, on this laptop, and never saved."}
          </Callout>
          <Field label="PDF password" hint="If your bank's email gave a password for this PDF, enter it.">
            {(p) => <TextInput {...p} type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} />}
          </Field>
          <div className={styles.submitRow}>
            <Button type="submit" variant="primary" icon={<KeyRound size={16} strokeWidth={1.5} aria-hidden="true" />}>Open the PDF</Button>
          </div>
        </form>
      )}
      {phase.kind === "result" && phase.outcome.outcome === "preview" && file && (
        <PreviewPanel
          key={phase.outcome.upload_id}
          outcome={phase.outcome}
          onDone={(outcome) => { setPhase({ kind: "result", outcome }); refreshBooks(client); }}
        />
      )}
      {phase.kind === "result" && phase.outcome.outcome !== "preview" && (
        <ImportResult
          outcome={phase.outcome}
          undone={undone}
          onUndone={setUndone}
          onAgain={() => { if (file) void upload(file, { manual }); }}
        />
      )}
    </div>
  );
}

// ── the result of an import ──

function ImportResult({
  outcome, undone, onUndone, onAgain,
}: {
  outcome: ImportedOutcome;
  undone: { removed: number; added: number } | null;
  onUndone: (u: { removed: number; added: number }) => void;
  onAgain: () => void;
}) {
  const client = useQueryClient();
  const toast = useToast();
  const b = outcome.batch;
  const undo = useMutation({
    mutationFn: () => post<UndoResult>(`/imports/${b.id}/undo`),
    onSuccess: (r) => {
      toast(`Removed ${r.removed} ${r.removed === 1 ? "entry" : "entries"}.`);
      onUndone({ removed: r.removed, added: b.added });
      refreshBooks(client);
    },
    onError: (e) => toast(e instanceof Error ? e.message : "Could not undo that import.", "over"),
  });

  if (undone || b.undone_at) {
    const n = undone?.added ?? b.added;
    return (
      <div className={styles.slip} data-testid="import-undone">
        <Eyebrow>{b.filename ?? "Statement"}</Eyebrow>
        <p><strong>Import undone.</strong></p>
        <p className={styles.help}>
          The {n} {n === 1 ? "entry" : "entries"} this import added {n === 1 ? "was" : "were"} removed. Entries you made by hand, and rows that were already recorded, were not touched.
        </p>
        <div className={styles.actions}><Button variant="secondary" icon={<Upload size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={onAgain}>Import this file again</Button></div>
      </div>
    );
  }

  return (
    <div className={styles.slip} data-testid="import-slip">
      <Eyebrow>{b.filename ?? "Statement"}</Eyebrow>
      {outcome.outcome === "already_imported" && (
        <Callout tone="note" role="status">You already imported this file. Nothing new was added.</Callout>
      )}
      <dl className={styles.counts}>
        <div><dt>Added as new entries</dt><dd data-testid="import-added">{b.added}</dd></div>
        <div><dt>Merged with entries you made by hand</dt><dd>{b.merged}</dd></div>
        <div><dt>Already recorded (skipped)</dt><dd>{b.duplicates}</dd></div>
        <div><dt>Needs review (not imported)</dt><dd>{b.needs_review}</dd></div>
      </dl>
      <div className={styles.verdictLine}>
        <Stamp tone="ok">Imported</Stamp>
        <span>Went into <strong>{b.account ?? "your account"}</strong>{outcome.how ? ` (${HOW[outcome.how]})` : ""}.</span>
      </div>
      {b.needs_review > 0 && (
        <Callout tone="near" lead={`${b.needs_review} ${b.needs_review === 1 ? "row was" : "rows were"} NOT imported.`}>
          Rectifi can{"’"}t prove from the balance (or a Dr/Cr mark) whether that money went out or came in, and it does not guess. The balance check below shows them as a gap until you add them by hand under Quick entry, or use the CSV or Excel statement.
          {outcome.needs_review_lines.length > 0 && (
            <ul className={styles.reviewList}>{outcome.needs_review_lines.map((l) => <li key={l}>{l}</li>)}</ul>
          )}
        </Callout>
      )}
      {outcome.skipped.map((s) => <p key={s} className={styles.help}>{s}</p>)}
      {outcome.reconcile ? (
        <ReconSlip
          title="Balance check"
          recon={outcome.reconcile}
          popBalanced
          actions={b.account_id !== null ? <LinkButton to={`/ledger?account=${b.account_id}&period=all`} variant="secondary">Open this account in the Ledger</LinkButton> : undefined}
        />
      ) : outcome.reconcile_reason ? (
        <p className={styles.help}>{outcome.reconcile_reason}</p>
      ) : null}
      {b.added === 0 ? (
        <p className={styles.help}>This import added nothing new, so there is nothing to undo.</p>
      ) : (
        <div className={styles.actions}>
          <Button variant="secondary" loading={undo.isPending} loadingLabel="Undoing…" icon={<Undo2 size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => undo.mutate()}>
            Undo this import
          </Button>
        </div>
      )}
    </div>
  );
}

// ── the step-by-step import: columns, a preview, the account, and the button ──

function PreviewPanel({ outcome, onDone }: { outcome: PreviewOutcome; onDone: (o: ImportedOutcome) => void }) {
  const meta = useMeta();
  const accounts = meta.data?.accounts ?? [];
  const [mapping, setMapping] = useState<Mapping>(outcome.mapping);
  const [preview, setPreview] = useState<Preview | null>(outcome.preview);
  const [previewError, setPreviewError] = useState<string | null>(outcome.preview_error);
  const [accountId, setAccountId] = useState<string>(outcome.preview?.suggested_account_id ? String(outcome.preview.suggested_account_id) : "");
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const chosen = accountId || (accounts[0] ? String(accounts[0].id) : "");

  async function remap(next: Mapping) {
    setMapping(next);
    setUpdating(true);
    try {
      const r = await post<{ preview: Preview }>("/imports/preview", { upload_id: outcome.upload_id, mapping: next });
      setPreview(r.preview);
      setPreviewError(null);
    } catch (e) {
      setPreview(null);
      setPreviewError(e instanceof Error ? e.message : "Those column choices don't work.");
    } finally {
      setUpdating(false);
    }
  }

  const commit = useMutation({
    mutationFn: () => post<ImportedOutcome>("/imports/commit", { upload_id: outcome.upload_id, mapping, account_id: Number(chosen) }),
    onSuccess: onDone,
    onError: (e) => setError(e instanceof Error ? e.message : "That import did not work."),
  });

  const rows = preview?.totals.count ?? 0;
  return (
    <div className={styles.slip} data-testid="import-preview">
      <Eyebrow>{outcome.filename}</Eyebrow>
      {outcome.reasons.length > 0 && <Callout tone="near" role="status">{outcome.reasons.join(" ")} Check the columns below, then import.</Callout>}
      {outcome.account_reason && <Callout tone="note" role="status">{outcome.account_reason}</Callout>}
      <div>
        <Eyebrow>Columns Rectifi detected</Eyebrow>
        <div className={styles.mapGrid} style={{ marginTop: 8 }}>
          {ROLES.map((role) => (
            <Field key={role} label={role.charAt(0).toUpperCase() + role.slice(1)}>
              {(p) => (
                <Select {...p} value={mapping[role] ?? ""} onChange={(e) => void remap({ ...mapping, [role]: e.target.value || null })}>
                  <option value="">{"—"}</option>
                  {outcome.columns.map((c) => <option key={c} value={c}>{c}</option>)}
                </Select>
              )}
            </Field>
          ))}
        </div>
      </div>
      {updating && <p role="status" className={styles.help}>Updating the preview…</p>}
      {previewError && <Callout tone="over" role="alert">{previewError}</Callout>}
      {preview && preview.review_rows.length > 0 && (
        <Callout tone="near" lead={`${preview.review_rows.length} ${preview.review_rows.length === 1 ? "row needs" : "rows need"} your review and will NOT be imported.`}>
          Rectifi can{"’"}t prove from the balance (or a Dr/Cr mark) whether that money went out or came in, and it does not guess. Add them by hand under Quick entry, or use the CSV or Excel statement.
          <div className={styles.tableWrap}>
            <table className={styles.tbl}>
              <thead><tr><th scope="col">Date</th><th scope="col">Narration</th><th scope="col" className={styles.r}>Amount</th><th scope="col">Why</th></tr></thead>
              <tbody>{preview.review_rows.map((r, i) => (
                <tr key={i}><td>{fmtShort(r.date)}</td><td>{r.narration}</td><td className={styles.r}><Money value={r.amount} /></td><td>{r.why}</td></tr>
              ))}</tbody>
            </table>
          </div>
        </Callout>
      )}
      {preview && preview.rows.length > 0 && (
        <>
          <dl className={styles.totals}>
            <div><dt><Eyebrow as="span">Rows</Eyebrow></dt><dd className="figures">{preview.totals.count}</dd></div>
            <div><dt><Eyebrow as="span">Period</Eyebrow></dt><dd className="figures">{preview.totals.first && preview.totals.last ? `${fmtShort(preview.totals.first)} – ${fmtShort(preview.totals.last)}` : "—"}</dd></div>
            <div><dt><Eyebrow as="span">Money out</Eyebrow></dt><dd><Money value={preview.totals.money_out} paise={false} /></dd></div>
            <div><dt><Eyebrow as="span">Money in</Eyebrow></dt><dd><Money value={preview.totals.money_in} paise={false} /></dd></div>
          </dl>
          <div className={styles.tableWrap}>
            <table className={styles.tbl}>
              <caption className="sr-only">The first rows of the statement</caption>
              <thead><tr><th scope="col">Date</th><th scope="col">Narration</th><th scope="col" className={styles.r}>Out</th><th scope="col" className={styles.r}>In</th><th scope="col" className={styles.r}>Balance</th></tr></thead>
              <tbody>{preview.rows.map((r, i) => (
                <tr key={i}>
                  <td>{fmtShort(r.date)}</td><td>{r.narration}</td>
                  <td className={styles.r}>{r.direction === "debit" ? <Money value={r.amount} /> : ""}</td>
                  <td className={styles.r}>{r.direction === "credit" ? <Money value={r.amount} /> : ""}</td>
                  <td className={styles.r}>{r.balance ? <Money value={r.balance} /> : ""}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          {preview.problems.map((p) => <p key={p} className={styles.help}>{p}</p>)}
        </>
      )}
      {preview && preview.rows.length === 0 && !previewError && preview.review_rows.length === 0 && (
        <Callout tone="near">No transactions found with these column choices.</Callout>
      )}
      {accounts.length > 0 && rows > 0 && (
        <>
          <Field label="Import into account">
            {(p) => (
              <Select {...p} value={chosen} onChange={(e) => setAccountId(e.target.value)}>
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </Select>
            )}
          </Field>
          {error && <Callout tone="over" role="alert">{error}</Callout>}
          <div className={styles.submitRow}>
            <Button variant="primary" loading={commit.isPending} loadingLabel="Importing…" onClick={() => { setError(null); commit.mutate(); }}>
              Import {rows} {rows === 1 ? "row" : "rows"}
            </Button>
          </div>
        </>
      )}
      {accounts.length === 0 && meta.data && <p className={styles.help}>Add an account first, then come back to import.</p>}
    </div>
  );
}
