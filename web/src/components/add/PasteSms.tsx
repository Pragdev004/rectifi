import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { post } from "../../api/client";
import { refreshBooks, useMeta } from "../../api/queries";
import type { AddResult, ParsedSms } from "../../api/types";
import { fmtDay } from "../../lib/dates";
import { Button, Field, TextArea } from "../forms";
import { Money } from "../Money";
import { Callout } from "../ui";
import { Receipt, type ReceiptState } from "./Receipt";
import styles from "./add.module.css";

const SAMPLE = "Rs.250.00 debited from A/c XX1234 on 21-10-26 to VPA swiggy@icici UPI Ref No 429183746521";

export function PasteSms() {
  const client = useQueryClient();
  const meta = useMeta();
  const [text, setText] = useState("");
  const [debounced, setDebounced] = useState("");
  const [receipt, setReceipt] = useState<ReceiptState | null>(null);

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(text.trim()), 300);
    return () => window.clearTimeout(t);
  }, [text]);

  const preview = useQuery({
    queryKey: ["sms-preview", debounced],
    queryFn: () => post<{ parsed: ParsedSms | null }>("/sms/preview", { text: debounced }),
    enabled: debounced.length > 0,
    staleTime: Infinity,
    retry: false,
  });

  const save = useMutation({
    mutationFn: () => post<AddResult>("/sms/save", { text: text.trim() }),
    onSuccess: (r) => {
      setReceipt({ status: r.status, txn: r.transaction, at: Date.now() });
      setText("");
      setDebounced("");
      refreshBooks(client);
    },
  });

  const parsed = debounced && debounced === text.trim() ? preview.data?.parsed : undefined;
  const today = meta.data?.today;

  return (
    <div className={styles.panel}>
      {receipt && <Receipt receipt={receipt} />}
      <Field label="Bank SMS" hint="Paste the text of a debit or credit message. Nothing is saved until you press the button.">
        {(p) => <TextArea {...p} rows={4} value={text} maxLength={4000} placeholder={SAMPLE} onChange={(e) => { setText(e.target.value); save.reset(); }} />}
      </Field>
      {preview.isError && <Callout tone="over" role="alert">{preview.error instanceof Error ? preview.error.message : "Could not read that."}</Callout>}
      {debounced && preview.data && preview.data.parsed === null && debounced === text.trim() && (
        <Callout tone="near" role="status">
          That doesn{"’"}t look like a transaction SMS {"—"} no amount, or no debited/credited wording.
        </Callout>
      )}
      {parsed && (
        <div className={styles.slip} data-testid="sms-slip">
          <dl className={styles.facts}>
            <dt>Amount</dt><dd className="fig"><Money value={parsed.amount} /></dd>
            <dt>Type</dt><dd>{parsed.direction === "debit" ? "Money out" : "Money in"}</dd>
            <dt>Date</dt><dd>{parsed.txn_date ? fmtDay(parsed.txn_date) : today ? `${fmtDay(today)} (today)` : "today"}</dd>
            <dt>Payee</dt><dd>{parsed.payee ?? "—"}</dd>
            <dt>UPI ref</dt><dd className="fig">{parsed.upi_ref ?? "—"}</dd>
            <dt>Account</dt>
            <dd>{parsed.account ? <>{parsed.account.name}{parsed.account.last4 ? ` ··${parsed.account.last4}` : ""}</> : "No account matched"}</dd>
          </dl>
          {parsed.warnings.map((w) => <p key={w} className={styles.help}>{w}</p>)}
          {save.isError && <Callout tone="over" role="alert">{save.error instanceof Error ? save.error.message : "That was not saved."}</Callout>}
          <div className={styles.submitRow}>
            <Button variant="primary" loading={save.isPending} loadingLabel="Saving…" onClick={() => save.mutate()}>Save from SMS</Button>
          </div>
        </div>
      )}
    </div>
  );
}
