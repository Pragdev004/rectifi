import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { del } from "../../api/client";
import { refreshBooks } from "../../api/queries";
import type { AccountRef, Category, Txn } from "../../api/types";
import { fmtDay } from "../../lib/dates";
import { Money } from "../Money";
import { ConfirmDialog, Dialog } from "../Dialog";
import { Button, Field, Select, TextInput } from "../forms";
import { payeeOf } from "../LedgerList";
import { useToast } from "../Toast";
import { InlineStatus } from "../ui";
import { categoriesFor, STATEMENT_NOTE, useEditTxn, type EditBody } from "./useEditTxn";
import styles from "./Ledger.module.css";

/** All of one entry, with the same edits as the table row, plus Delete. A bottom sheet on a phone. */
export function EntrySheet({ txn, categories, accounts, onClose }: { txn: Txn | null; categories: Category[]; accounts: AccountRef[]; onClose: () => void }) {
  const edit = useEditTxn();
  const client = useQueryClient();
  const toast = useToast();
  const [note, setNote] = useState("");
  const [state, setState] = useState<{ field: string; kind: "saving" | "saved" | "error"; message?: string } | null>(null);
  const [confirm, setConfirm] = useState(false);

  useEffect(() => { setNote(txn?.note ?? ""); setState(null); setConfirm(false); }, [txn?.id, txn?.note]);

  const remove = useMutation({
    mutationFn: () => del<{ deleted: boolean }>(`/transactions/${txn?.id}`),
    onSuccess: () => {
      toast("Entry deleted.");
      refreshBooks(client);
      setConfirm(false);
      onClose();
    },
    onError: (e) => { setConfirm(false); setState({ field: "delete", kind: "error", message: e instanceof Error ? e.message : "Could not delete that entry." }); },
  });

  async function run(field: string, body: EditBody) {
    if (!txn) return;
    setState({ field, kind: "saving" });
    try {
      await edit(txn, body);
      setState({ field, kind: "saved" });
    } catch (e) {
      setState({ field, kind: "error", message: e instanceof Error ? e.message : "That change was not saved." });
    }
  }

  const statement = txn?.source === "statement";
  const status = (field: string) => {
    if (state?.field !== field) return null;
    if (state.kind === "saving") return <InlineStatus kind="saving">Saving…</InlineStatus>;
    if (state.kind === "saved") return <InlineStatus kind="saved">Saved</InlineStatus>;
    return null;
  };
  const err = (field: string) => (state?.field === field && state.kind === "error" ? state.message ?? null : null);
  const name = txn ? payeeOf(txn) : "";

  return (
    <>
      <Dialog open={txn !== null} onClose={onClose} title={name || "Entry"} sheet>
        {txn && (
          <div className={styles.sheetGrid}>
            <div className={styles.sheetTitle}>
              <span className={styles.sheetAmount}>
                <Money value={txn.amount} sign={txn.direction === "credit"} tone={txn.direction === "credit" ? "credit" : undefined} />
                {txn.direction === "credit" ? " IN" : ""}
              </span>
              <dl className={styles.facts}>
                <dt>Date</dt><dd>{fmtDay(txn.date)}</dd>
                <dt>Source</dt><dd>{txn.source}</dd>
                {txn.description && txn.description !== txn.payee && (<><dt>Details</dt><dd>{txn.description}</dd></>)}
              </dl>
            </div>
            <Field label="Category" error={err("category")} hint={status("category")}>
              {(p) => (
                <Select {...p} value={txn.category ?? ""} onChange={(e) => void run("category", { category: e.target.value })}>
                  {txn.category === null && <option value="" disabled>Uncategorised</option>}
                  {categoriesFor(txn, categories).map((c) => <option key={c.id} value={c.name}>{c.name}</option>)}
                </Select>
              )}
            </Field>
            <Field label="Account" error={err("account")} hint={statement ? STATEMENT_NOTE : status("account")}>
              {(p) => (
                <Select
                  {...p}
                  value={txn.account_id === null ? "" : String(txn.account_id)}
                  disabled={statement}
                  onChange={(e) => void run("account", { account_id: e.target.value === "" ? null : Number(e.target.value) })}
                >
                  <option value="">Not linked</option>
                  {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                </Select>
              )}
            </Field>
            <Field label="Note" error={err("note")} hint={status("note")}>
              {(p) => (
                <TextInput
                  {...p}
                  value={note}
                  maxLength={500}
                  onChange={(e) => setNote(e.target.value)}
                  onBlur={() => { if (note.trim() !== (txn.note ?? "")) void run("note", { note: note.trim() || null }); }}
                  onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }}
                />
              )}
            </Field>
            {err("delete") && <InlineStatus kind="error">{err("delete")}</InlineStatus>}
            <div className={styles.sheetFoot}>
              <Button variant="dangerQuiet" onClick={() => setConfirm(true)}>Delete entry</Button>
              <Button variant="secondary" onClick={onClose}>Done</Button>
            </div>
          </div>
        )}
      </Dialog>
      <ConfirmDialog
        open={confirm && txn !== null}
        title="Delete this entry?"
        confirmLabel="Delete"
        busyLabel="Deleting…"
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
        onClose={() => setConfirm(false)}
      >
        {txn && <p><Money value={txn.amount} /> {"·"} {name} {"·"} {fmtDay(txn.date)}. This can{"’"}t be undone.</p>}
      </ConfirmDialog>
    </>
  );
}
