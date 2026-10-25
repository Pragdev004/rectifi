import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError, post } from "../../api/client";
import { keys, refreshBooks, useMeta } from "../../api/queries";
import type { AddResult, InboxPrefill } from "../../api/types";
import { shiftDay } from "../../lib/dates";
import { useIsDesktop } from "../../lib/media";
import { AmountInput, Button, Field, LinkButton, SegmentedControl, Select, TextInput } from "../forms";
import { Callout } from "../ui";
import { Receipt, type ReceiptState } from "./Receipt";
import styles from "./add.module.css";

type Direction = "debit" | "credit";

export function QuickEntry({ prefill, onSavedFromInbox }: { prefill: InboxPrefill | null; onSavedFromInbox: () => void }) {
  const desktop = useIsDesktop();
  const meta = useMeta();
  const client = useQueryClient();
  const today = meta.data?.today ?? "";
  const accounts = meta.data?.accounts ?? [];
  const defaultAccount = meta.data?.default_account_id ?? null;

  const [amount, setAmount] = useState("");
  const [direction, setDirection] = useState<Direction>("debit");
  const [date, setDate] = useState("");
  const [payee, setPayee] = useState("");
  const [category, setCategory] = useState("");
  const [account, setAccount] = useState<string | null>(null); // null = not touched yet: use the default
  const [note, setNote] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [general, setGeneral] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<ReceiptState | null>(null);
  const amountRef = useRef<HTMLInputElement>(null);

  // A text the phone could not read: start from what the fixed rules did read, and let the person check it.
  useEffect(() => {
    if (!prefill) return;
    if (prefill.amount) setAmount(prefill.amount.replace(/\.00$/, ""));
    if (prefill.direction) setDirection(prefill.direction);
    if (prefill.txn_date) setDate(prefill.txn_date);
    setPayee(prefill.payee ?? "");
    if (prefill.account_id !== null) setAccount(String(prefill.account_id));
  }, [prefill]);

  const effectiveDate = date || today;
  const effectiveAccount = account ?? (defaultAccount !== null ? String(defaultAccount) : "none");
  const kind = direction === "credit" ? "income" : "expense";
  const options = (meta.data?.categories ?? []).filter((c) => c.kind === kind || c.kind === "transfer");

  const save = useMutation({
    mutationFn: () =>
      post<AddResult>("/transactions", {
        amount: amount.trim(),
        direction,
        date: effectiveDate,
        payee: payee.trim() || null,
        category: category || null,
        account_id: effectiveAccount === "none" ? null : Number(effectiveAccount),
        note: note.trim() || null,
        ...(prefill ? { inbox_id: prefill.inbox_id } : {}),
      }),
    onMutate: () => { setErrors({}); setGeneral(null); },
    onSuccess: (r) => {
      setReceipt({ status: r.status, txn: r.transaction, at: Date.now() });
      setAmount(""); setPayee(""); setNote(""); setCategory(""); setDirection("debit"); setDate("");
      refreshBooks(client);
      void client.invalidateQueries({ queryKey: keys.phoneInbox });
      if (prefill) onSavedFromInbox();
      if (desktop) amountRef.current?.focus();
    },
    onError: (e) => {
      if (e instanceof ApiError && e.field) setErrors({ [e.field === "account_id" ? "account" : e.field]: e.message });
      else setGeneral(e instanceof Error ? e.message : "That was not saved.");
    },
  });

  return (
    <form
      className={styles.panel}
      onSubmit={(e) => { e.preventDefault(); save.mutate(); }}
      noValidate
      aria-label="Quick entry"
    >
      {receipt && <Receipt receipt={receipt} />}
      {accounts.length === 0 && meta.data && (
        <Callout tone="note" lead="No account yet." actions={<LinkButton to="/accounts" variant="secondary">Add an account</LinkButton>}>
          Entries with no account aren{"’"}t covered by the balance check. Add the bank account you spend from first.
        </Callout>
      )}
      <Field label={"Amount (₹)"} error={errors.amount}>
        {(p) => <AmountInput {...p} big ref={amountRef} value={amount} placeholder="250" autoFocus={desktop && !prefill} onChange={(e) => setAmount(e.target.value)} />}
      </Field>
      <div className={`${styles.grid2} ${styles.typeRow}`}>
        <SegmentedControl
          legend="Type"
          name="quick-direction"
          full
          options={[{ value: "debit", label: "Money out" }, { value: "credit", label: "Money in" }]}
          value={direction}
          onChange={(v) => { setDirection(v); setCategory(""); }}
        />
        <div className={styles.dateRow}>
          <Field label="Date" error={errors.date}>
            {(p) => <TextInput {...p} type="date" value={effectiveDate} max={today} onChange={(e) => setDate(e.target.value)} />}
          </Field>
          <div className={styles.quick}>
            <Button variant="secondary" onClick={() => setDate(today)} aria-label="Set the date to today">Today</Button>
            <Button variant="secondary" onClick={() => setDate(shiftDay(today, -1))} aria-label="Set the date to yesterday">Yesterday</Button>
          </div>
        </div>
      </div>
      <div className={styles.grid2}>
        <Field label="Paid to / received from" error={errors.payee}>
          {(p) => <TextInput {...p} value={payee} placeholder="Swiggy" maxLength={120} onChange={(e) => setPayee(e.target.value)} />}
        </Field>
        <Field label="Category" error={errors.category}>
          {(p) => (
            <Select {...p} value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="">Auto (from payee)</option>
              {options.map((c) => <option key={c.id} value={c.name}>{c.name}</option>)}
            </Select>
          )}
        </Field>
        <Field label="Account" error={errors.account} hint={"Entries with no account aren’t covered by the balance check."}>
          {(p) => (
            <Select {...p} value={effectiveAccount} onChange={(e) => setAccount(e.target.value)}>
              {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              <option value="none">No account</option>
            </Select>
          )}
        </Field>
        <Field label="Note (optional)" error={errors.note}>
          {(p) => <TextInput {...p} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />}
        </Field>
      </div>
      {general && <Callout tone="over" role="alert">{general}</Callout>}
      <div className={styles.submitRow}>
        <Button type="submit" variant="primary" loading={save.isPending} loadingLabel="Saving…">Save entry</Button>
      </div>
    </form>
  );
}
