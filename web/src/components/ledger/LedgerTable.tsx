import { Ellipsis } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { AccountRef, Category, Txn } from "../../api/types";
import { fmtDay, fmtShort } from "../../lib/dates";
import { cx } from "../../lib/cx";
import { payeeOf, TxnAmount } from "../LedgerList";
import { IconButton, Select, TextInput } from "../forms";
import { categoriesFor, STATEMENT_NOTE, useEditTxn, type EditBody } from "./useEditTxn";
import styles from "./Ledger.module.css";

type Field = "category" | "account" | "note";

function Row({ t, categories, accounts, onOpen }: { t: Txn; categories: Category[]; accounts: AccountRef[]; onOpen: (t: Txn) => void }) {
  const edit = useEditTxn();
  const [busy, setBusy] = useState<Field | null>(null);
  const [saved, setSaved] = useState<Field | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState(false);
  const [note, setNote] = useState(t.note ?? "");
  const timers = useRef<number[]>([]);
  useEffect(() => setNote(t.note ?? ""), [t.note]);
  useEffect(() => () => timers.current.forEach(window.clearTimeout), []);

  async function run(field: Field, body: EditBody) {
    setBusy(field);
    setError(null);
    try {
      await edit(t, body);
      setSaved(field);
      setFlash(true);
      timers.current.push(window.setTimeout(() => setSaved(null), 2000), window.setTimeout(() => setFlash(false), 650));
    } catch (e) {
      setError(e instanceof Error ? e.message : "That change was not saved.");
      if (field === "note") setNote(t.note ?? "");
    } finally {
      setBusy(null);
    }
  }

  const statement = t.source === "statement";
  const status = (field: Field) =>
    busy === field ? <span className={styles.status}>Saving…</span> : saved === field ? <span className={cx(styles.status, styles.saved)}>Saved</span> : null;
  const name = payeeOf(t);

  return (
    <>
      <tr className={cx(flash && styles.flash)} data-testid="ledger-row" data-txn-id={t.id}>
        <td className={styles.date}>{fmtShort(t.date)}<small>{t.date.slice(0, 4)}</small></td>
        <td className={styles.payee}>
          <strong>{name}</strong>
          {t.description && t.description !== t.payee && <small>{t.description}</small>}
        </td>
        <td className={styles.cat} aria-busy={busy === "category" || undefined}>
          <Select
            aria-label={`Category for ${name}, ${fmtDay(t.date)}`}
            value={t.category ?? ""}
            disabled={busy === "category"}
            onChange={(e) => void run("category", { category: e.target.value })}
          >
            {t.category === null && <option value="" disabled>Uncategorised</option>}
            {categoriesFor(t, categories).map((c) => <option key={c.id} value={c.name}>{c.name}</option>)}
          </Select>
          {status("category")}
        </td>
        <td className={styles.acct} aria-busy={busy === "account" || undefined}>
          <Select
            aria-label={`Account for ${name}, ${fmtDay(t.date)}`}
            value={t.account_id === null ? "" : String(t.account_id)}
            disabled={busy === "account" || statement}
            title={statement ? STATEMENT_NOTE : undefined}
            onChange={(e) => void run("account", { account_id: e.target.value === "" ? null : Number(e.target.value) })}
          >
            <option value="">Not linked</option>
            {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </Select>
          {status("account")}
        </td>
        <td className={styles.amountCell}><TxnAmount t={t} /></td>
        <td className={styles.src}>{t.source}</td>
        <td className={styles.noteCell} aria-busy={busy === "note" || undefined}>
          <TextInput
            aria-label={`Note for ${name}, ${fmtDay(t.date)}`}
            value={note}
            maxLength={500}
            placeholder="Add a note"
            onChange={(e) => setNote(e.target.value)}
            onBlur={() => { if (note.trim() !== (t.note ?? "")) void run("note", { note: note.trim() || null }); }}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
              if (e.key === "Escape") { setNote(t.note ?? ""); e.currentTarget.blur(); }
            }}
          />
          {status("note")}
        </td>
        <td className={styles.actions}>
          <IconButton label={`More about ${name}, ${fmtDay(t.date)}`} onClick={() => onOpen(t)}><Ellipsis size={20} strokeWidth={1.5} aria-hidden="true" /></IconButton>
        </td>
      </tr>
      {error && (
        <tr className={styles.errorRow}><td colSpan={8} role="alert">{error}</td></tr>
      )}
    </>
  );
}

export function LedgerTable({ items, categories, accounts, onOpen }: { items: Txn[]; categories: Category[]; accounts: AccountRef[]; onOpen: (t: Txn) => void }) {
  return (
    <div className={styles.wrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col">Payee</th>
            <th scope="col">Category</th>
            <th scope="col">Account</th>
            <th scope="col" className={styles.num}>Amount</th>
            <th scope="col">Source</th>
            <th scope="col">Note</th>
            <th scope="col"><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {items.map((t) => <Row key={t.id} t={t} categories={categories} accounts={accounts} onOpen={onOpen} />)}
        </tbody>
      </table>
    </div>
  );
}
