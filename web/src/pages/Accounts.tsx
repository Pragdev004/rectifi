import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
import { ApiError, post } from "../api/client";
import { keys, refreshBooks, useAccounts, useMeta } from "../api/queries";
import type { AccountItem, BalanceResult } from "../api/types";
import { AmountInput, Button, Field, LinkButton, TextInput } from "../components/forms";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { ReconSlip } from "../components/ReconSlip";
import { useToast } from "../components/Toast";
import { Callout, EmptyState, Eyebrow, Skeleton } from "../components/ui";
import { fmtDay } from "../lib/dates";
import styles from "./pages.module.css";
import acct from "./accounts.module.css";

function AccountSection({ a, today }: { a: AccountItem; today: string }) {
  const client = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [when, setWhen] = useState(today);
  const [balance, setBalance] = useState("");
  const [errors, setErrors] = useState<{ as_of?: string; balance?: string; general?: string }>({});
  const [pop, setPop] = useState(false);

  const save = useMutation({
    mutationFn: () => post<BalanceResult>(`/accounts/${a.id}/balances`, { as_of: when, balance: balance.trim() }),
    onMutate: () => setErrors({}),
    onSuccess: (r) => {
      setOpen(false);
      setBalance("");
      setPop(Boolean(r.reconcile?.balanced) && !a.reconcile?.balanced);
      toast("Balance saved.");
      refreshBooks(client);
      void client.invalidateQueries({ queryKey: keys.accounts });
    },
    onError: (e) => {
      if (e instanceof ApiError && (e.field === "as_of" || e.field === "balance")) setErrors({ [e.field]: e.message });
      else setErrors({ general: e instanceof Error ? e.message : "That balance was not saved." });
    },
  });

  return (
    <section className={acct.account} aria-labelledby={`acct-${a.id}`} data-testid={`account-${a.id}`}>
      <h2 id={`acct-${a.id}`} className={acct.name}>
        {a.name}{a.last4 && <span className={acct.digits}> {"··"}{a.last4}</span>}
      </h2>
      <p className={acct.meta}>
        {a.transactions} {a.transactions === 1 ? "entry" : "entries"} {"·"}{" "}
        {a.last_snapshot ? `last bank balance ${fmtDay(a.last_snapshot)}` : "no bank balance recorded yet"}
      </p>
      {a.reconcile ? (
        <ReconSlip
          recon={a.reconcile}
          popBalanced={pop}
          actions={
            <>
              <LinkButton to={`/ledger?account=${a.id}&period=all`} variant="secondary">Open this account in the Ledger</LinkButton>
              <LinkButton to="/add?tab=import" variant="tertiary">Import a statement</LinkButton>
            </>
          }
        />
      ) : (
        <p className={acct.reason}>{a.reason}</p>
      )}
      {open ? (
        <form className={acct.balanceForm} onSubmit={(e) => { e.preventDefault(); save.mutate(); }} aria-label={`Record a balance for ${a.name}`} noValidate>
          <Field label="End of day" error={errors.as_of}>
            {(p) => <TextInput {...p} type="date" value={when} max={today} onChange={(e) => setWhen(e.target.value)} />}
          </Field>
          <Field label={"Balance (₹)"} error={errors.balance} hint="Your bank's balance at the end of that day. A minus sign is fine for an overdraft.">
            {(p) => <AmountInput {...p} value={balance} placeholder="12000" autoFocus onChange={(e) => setBalance(e.target.value)} />}
          </Field>
          {errors.general && <Callout tone="over" role="alert">{errors.general}</Callout>}
          <div className={acct.formActions}>
            <Button type="submit" variant="primary" loading={save.isPending} loadingLabel="Saving…">Save balance</Button>
            <Button variant="tertiary" onClick={() => { setOpen(false); setErrors({}); }}>Cancel</Button>
          </div>
        </form>
      ) : (
        <div><Button variant="secondary" onClick={() => { setOpen(true); setWhen(today); }}>Record a balance</Button></div>
      )}
    </section>
  );
}

function AddAccount() {
  const client = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [last4, setLast4] = useState("");
  const [error, setError] = useState<string | null>(null);
  const add = useMutation({
    mutationFn: () => post<{ account: { id: number; name: string } }>("/accounts", { name: name.trim(), last4: last4.trim() || null }),
    onMutate: () => setError(null),
    onSuccess: (r) => {
      toast(`Account ${r.account.name} added.`);
      setName("");
      setLast4("");
      refreshBooks(client);
    },
    onError: (e) => setError(e instanceof Error ? e.message : "That account was not added."),
  });
  return (
    <form className={acct.addForm} onSubmit={(e) => { e.preventDefault(); add.mutate(); }} aria-label="Add an account" noValidate>
      <Eyebrow as="h2">Add an account</Eyebrow>
      <Field label="Name" error={error}>
        {(p) => <TextInput {...p} value={name} placeholder="SBI Savings" maxLength={80} onChange={(e) => setName(e.target.value)} />}
      </Field>
      <Field label="Last 4 digits" hint="Matches bank SMS and statements.">
        {(p) => <TextInput {...p} inputMode="numeric" pattern="[0-9]*" maxLength={4} value={last4} placeholder="1234" onChange={(e) => setLast4(e.target.value.replace(/\D/g, ""))} />}
      </Field>
      <div><Button type="submit" variant="primary" icon={<Plus size={16} strokeWidth={2} aria-hidden="true" />} loading={add.isPending} loadingLabel="Adding…">Add account</Button></div>
    </form>
  );
}

export default function AccountsPage() {
  const query = useAccounts();
  const meta = useMeta();
  const today = meta.data?.today ?? new Date().toISOString().slice(0, 10);
  return (
    <>
      <PageHeader
        name="Accounts"
        title="Accounts"
        sub="Reconciliation needs the bank's balance on two dates. A statement import records them automatically; you can also type them in from your passbook or banking app."
      />
      <Loaded query={query} skeleton={<div className={styles.skelStack} aria-hidden="true"><Skeleton height={200} /><Skeleton height={80} /></div>}>
        {(data) => (
          <div className={acct.layout}>
            <div className={acct.list}>
              {data.items.length === 0 ? (
                <EmptyState title="No accounts yet.">Add the bank account you spend from. Rectifi uses it to match SMS and statements.</EmptyState>
              ) : (
                data.items.map((a) => <AccountSection key={a.id} a={a} today={today} />)
              )}
            </div>
            <AddAccount />
          </div>
        )}
      </Loaded>
    </>
  );
}
