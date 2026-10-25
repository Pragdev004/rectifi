import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { useState } from "react";
import { ApiError, post, put } from "../api/client";
import { keys, refreshBooks, useBudgets, useMeta } from "../api/queries";
import type { BudgetRowData, BudgetStatus } from "../api/types";
import { BudgetLegend, BudgetRow } from "../components/BudgetRow";
import { AmountInput, Button, Field } from "../components/forms";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { MonthStepper } from "../components/Steppers";
import { useToast } from "../components/Toast";
import { EmptyState, Section, Skeleton } from "../components/ui";
import { monthLabel, monthName } from "../lib/dates";
import { formatInr, formatPercent } from "../lib/money";
import { useUrlState } from "../lib/urlState";
import styles from "./pages.module.css";
import b from "./budgets.module.css";

const plain = (limit: string) => limit.replace(/\.00$/, "");

/** Set, change or remove one limit. The server answers with the month's whole status, so the bars are always the real ones. */
function useSetLimit(month: string, onStatus: (s: BudgetStatus) => void) {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: (v: { category: string; limit: string | null }) => put<BudgetStatus>("/budgets", { month, category: v.category, limit: v.limit }),
    onSuccess: (status, v) => {
      onStatus(status);
      toast(v.limit === null ? `Limit removed for ${v.category}.` : `Budget saved for ${v.category}.`);
      refreshBooks(client);
    },
  });
}

function LimitEditor({ row, month, animate, onSaved }: { row: BudgetRowData; month: string; animate: () => void; onSaved: (s: BudgetStatus) => void }) {
  const [value, setValue] = useState(plain(row.limit));
  const [error, setError] = useState<string | null>(null);
  const set = useSetLimit(month, (s) => { animate(); onSaved(s); });
  const dirty = value.trim() !== plain(row.limit);
  const run = (limit: string | null) => { setError(null); set.mutate({ category: row.category, limit }, { onError: (e) => setError(e instanceof Error ? e.message : "That was not saved.") }); };
  return (
    <form className={b.editor} onSubmit={(e) => { e.preventDefault(); if (dirty) run(value.trim()); }} aria-label={`Limit for ${row.category}`} noValidate>
      <Field label={`Limit for ${row.category}`} hideLabel error={error}>
        {(p) => <AmountInput {...p} value={value} onChange={(e) => setValue(e.target.value)} />}
      </Field>
      {dirty && (
        <>
          <Button type="submit" variant="primary" loading={set.isPending} loadingLabel="Saving…">Save</Button>
          <Button variant="tertiary" onClick={() => { setValue(plain(row.limit)); setError(null); }}>Cancel</Button>
        </>
      )}
      {!dirty && <Button variant="tertiary" loading={set.isPending} loadingLabel="Removing…" onClick={() => run(null)}>Remove limit</Button>}
    </form>
  );
}

function NewLimit({ category, month, onSaved }: { category: string; month: string; onSaved: (s: BudgetStatus) => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const set = useSetLimit(month, (s) => { setValue(""); onSaved(s); });
  return (
    <form
      className={b.newRow}
      onSubmit={(e) => { e.preventDefault(); setError(null); set.mutate({ category, limit: value.trim() }, { onError: (err) => setError(err instanceof Error ? err.message : "That was not saved.") }); }}
      aria-label={`Set a limit for ${category}`}
      noValidate
    >
      <span className={b.newName}>{category}</span>
      <Field label={`Limit for ${category}`} hideLabel error={error}>
        {(p) => <AmountInput {...p} value={value} placeholder="Set a limit" onChange={(e) => setValue(e.target.value)} />}
      </Field>
      <Button type="submit" variant="secondary" loading={set.isPending} loadingLabel="Saving…" aria-label={`Save the limit for ${category}`}>Set a limit</Button>
    </form>
  );
}

export default function BudgetsPage() {
  const meta = useMeta();
  const client = useQueryClient();
  const toast = useToast();
  const [{ month }, setUrl] = useUrlState({ month: "" });
  const query = useBudgets(month || null);
  const shown = query.data?.status.month ?? (month || meta.data?.months[0] || null);
  const [animated, setAnimated] = useState<string | null>(null);

  const copy = useMutation({
    mutationFn: (v: { from: string; to: string }) => post<{ copied: number }>("/budgets/copy", v),
    onSuccess: (r, v) => {
      toast(r.copied ? `Copied ${r.copied} ${r.copied === 1 ? "budget" : "budgets"}.` : `No budgets in ${monthName(v.from)} to copy.`, r.copied ? "ok" : "note");
      refreshBooks(client);
    },
    onError: (e) => toast(e instanceof ApiError ? e.message : "Could not copy the budgets.", "over"),
  });

  return (
    <>
      <PageHeader
        name="Budgets"
        title={shown ? `Budgets · ${monthLabel(shown)}` : "Budgets"}
        controls={meta.data && shown ? <MonthStepper value={shown} months={meta.data.months} onChange={(m) => setUrl({ month: m })} /> : undefined}
      />
      <Loaded query={query} skeleton={<div className={styles.skelStack} aria-hidden="true"><Skeleton height={20} width="60%" /><Skeleton height={90} /><Skeleton height={90} /></div>}>
        {(data) => {
          const st = data.status;
          const budgeted = new Set(st.categories.map((c) => c.category));
          const rest = data.expense_categories.filter((c) => !budgeted.has(c));
          const refresh = () => void client.invalidateQueries({ queryKey: keys.budgets(month || null) });
          return (
            <>
              {st.categories.length > 0 ? (
                <Section
                  eyebrow={`${monthName(st.month)} \u00b7 ${formatPercent(st.pace)} of the month gone \u00b7 ${formatInr(st.total_spent, { paise: false })} of ${formatInr(st.total_limit, { paise: false })} used`}
                >
                  <BudgetLegend day={st.pace_day} days={st.days_in_month} />
                  {st.categories.map((row) => (
                    <BudgetRow key={row.category} row={row} pace={st.pace} monthName={monthName(st.month)} animate={animated === row.category}>
                      <LimitEditor row={row} month={st.month} animate={() => setAnimated(row.category)} onSaved={refresh} />
                    </BudgetRow>
                  ))}
                </Section>
              ) : (
                <EmptyState
                  title="No budgets for this month yet."
                  actions={
                    <Button variant="secondary" icon={<Copy size={16} strokeWidth={1.5} aria-hidden="true" />} loading={copy.isPending} loadingLabel="Copying…" onClick={() => copy.mutate({ from: data.previous_month, to: st.month })}>
                      Copy budgets from {monthName(data.previous_month)}
                    </Button>
                  }
                >
                  Set limits below. Start with the 3{"–"}4 categories where your money actually goes.
                </EmptyState>
              )}
              {rest.length > 0 && (
                <Section eyebrow="No budget yet">
                  <div className={b.newList}>
                    {rest.map((c) => <NewLimit key={c} category={c} month={st.month} onSaved={refresh} />)}
                  </div>
                </Section>
              )}
            </>
          );
        }}
      </Loaded>
    </>
  );
}

