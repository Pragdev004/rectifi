import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil } from "lucide-react";
import { useEffect, useState } from "react";
import { put } from "../api/client";
import { keys, useMeta, useReview } from "../api/queries";
import type { Review } from "../api/types";
import { Button, Field, LinkButton, TextInput } from "../components/forms";
import { Hero } from "../components/Hero";
import { InsightRow } from "../components/InsightRow";
import { LedgerList } from "../components/LedgerList";
import { Loaded } from "../components/Loaded";
import { Money } from "../components/Money";
import { PageHeader } from "../components/PageHeader";
import { CategoryBars } from "../components/CategoryBars";
import { WeekStepper } from "../components/Steppers";
import { EmptyState, Eyebrow, Section, Skeleton, Stamp } from "../components/ui";
import { fmtShort, weekdayName } from "../lib/dates";
import { formatInr } from "../lib/money";
import { useUrlState } from "../lib/urlState";
import { CompareSlip } from "../components/CompareSlip";
import styles from "./pages.module.css";
import r from "./review.module.css";

const COMMITMENT_MAX = 280;
const COUNT_FROM = 240;

function throughLabel(through: string): string {
  return `${weekdayName(through)} ${fmtShort(through)}`;
}

function Attention({ data }: { data: Review }) {
  const rows: { key: string; stamp: React.ReactNode; text: React.ReactNode; action?: React.ReactNode }[] = [];
  if (data.uncategorised_count) {
    const n = data.uncategorised_count;
    rows.push({
      key: "other",
      stamp: <Stamp tone="near">Check</Stamp>,
      text: <>{n} {n === 1 ? "entry is" : "entries are"} still in {"\u201c"}Miscellaneous{"\u201d"}. Give {n === 1 ? "it" : "them"} a real category in the Ledger.</>,
      action: <LinkButton to="/ledger?category=Miscellaneous&period=all" variant="tertiary">Fix in Ledger</LinkButton>,
    });
  }
  for (const b of data.budgets_needing_attention) {
    const over = b.state === "over";
    rows.push({
      key: `budget-${b.category}`,
      stamp: <Stamp tone={over ? "over" : "near"}>{over ? "Over budget" : "Near limit"}</Stamp>,
      text: <><strong>{b.category}</strong>: <Money value={b.spent} paise={false} /> of <Money value={b.limit} paise={false} /> used this month ({b.percent}%).</>,
      action: <LinkButton to={`/budgets${data.budget_month ? `?month=${data.budget_month}` : ""}`} variant="tertiary">Review your Budgets</LinkButton>,
    });
  }
  if (data.unlinked_count) {
    const n = data.unlinked_count;
    rows.push({
      key: "unlinked",
      stamp: <Stamp tone="near">Check</Stamp>,
      text: <>{n === 1 ? "1 entry this week isn\u2019t" : `${n} entries this week aren\u2019t`} linked to an account, so no balance check covers {n === 1 ? "it" : "them"}.</>,
      action: <LinkButton to="/ledger?account=none&period=all" variant="tertiary">See unlinked entries</LinkButton>,
    });
  }
  for (const a of data.accounts_needing_attention) {
    rows.push({
      key: `acct-${a.id}`,
      stamp: <Stamp tone="near">Check</Stamp>,
      text: <><strong>{a.name}</strong>{a.reasons.map((x) => <span key={x} className={r.reason}>{x}</span>)}</>,
      action: <LinkButton to="/accounts" variant="tertiary">Check your Accounts</LinkButton>,
    });
  }
  if (rows.length === 0 && data.all_clear) {
    rows.push({
      key: "clear",
      stamp: <Stamp tone="ok">All clear</Stamp>,
      text: <>No budget is near its limit, nothing is uncategorised, every entry is linked to an account, and every account checks out.</>,
    });
  }
  if (rows.length === 0) return null;
  return (
    <Section eyebrow="Needs attention">
      <ul className={r.attention} data-testid="needs-attention">
        {rows.map((row) => (
          <li key={row.key} className={r.attRow}>
            <div className={r.attStamp}>{row.stamp}</div>
            <div className={r.attText}>{row.text}</div>
            {row.action && <div className={r.attAction}>{row.action}</div>}
          </li>
        ))}
      </ul>
    </Section>
  );
}

function Commitment({ week, last, saved }: { week: string; last: string | null; saved: string | null }) {
  const client = useQueryClient();
  const [text, setText] = useState(saved ?? "");
  const [editing, setEditing] = useState(!saved);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { setText(saved ?? ""); setEditing(!saved); setError(null); }, [week, saved]);

  const save = useMutation({
    mutationFn: () => put<{ week: string; text: string }>("/review/commitment", { week, text: text.trim() }),
    onMutate: () => setError(null),
    onSuccess: () => {
      setEditing(false);
      void client.invalidateQueries({ queryKey: keys.review(week) });
      void client.invalidateQueries({ queryKey: ["review"] });
    },
    onError: (e) => setError(e instanceof Error ? e.message : "That was not saved."),
  });
  const n = text.length;

  return (
    <Section eyebrow="One commitment">
      {last && (
        <div className={r.lastWeek} data-testid="last-commitment">
          <Eyebrow>What you committed to last week</Eyebrow>
          <p className={r.promise}>{last}</p>
          <p className={r.muted}>Did you keep it? Look at the numbers above, then pick your next one.</p>
        </div>
      )}
      {!editing && saved ? (
        <div className={r.saved} data-testid="commitment-saved">
          <Eyebrow>Your commitment for next week</Eyebrow>
          <p className={r.promise}>{saved}</p>
          <div><Button variant="secondary" icon={<Pencil size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => setEditing(true)}>Edit</Button></div>
        </div>
      ) : (
        <form className={r.form} onSubmit={(e) => { e.preventDefault(); save.mutate(); }} noValidate aria-label="Your commitment">
          <Field
            label="Your commitment for next week"
            hint="One small, specific promise. Next week this page will show it back to you."
            error={error}
          >
            {(p) => <TextInput {...p} value={text} placeholder="No more than 3 delivery orders" onChange={(e) => setText(e.target.value)} />}
          </Field>
          {n >= COUNT_FROM && (
            <p className={n > COMMITMENT_MAX ? r.countOver : r.count} role="status" aria-live="polite" data-testid="commitment-count">
              {n} of {COMMITMENT_MAX} characters{n > COMMITMENT_MAX ? ` \u2014 ${n - COMMITMENT_MAX} too many` : ""}
            </p>
          )}
          <div className={r.formActions}>
            <Button type="submit" variant="primary" icon={<Check size={16} strokeWidth={2} aria-hidden="true" />} loading={save.isPending} loadingLabel="Saving\u2026" disabled={n === 0}>
              Save commitment
            </Button>
            {saved && <Button variant="tertiary" onClick={() => { setText(saved); setEditing(false); setError(null); }}>Cancel</Button>}
          </div>
        </form>
      )}
    </Section>
  );
}

function ReviewBody({ data }: { data: Review }) {
  const rate = data.savings_rate;
  const short = data.saved.startsWith("-");
  const empty = data.count === 0;
  return (
    <>
      {empty ? (
        <EmptyState title="Nothing recorded this week yet." actions={<LinkButton to="/add" variant="primary">Add an entry</LinkButton>}>
          Add what you spent on the Add page, or import your statement. Then come back on Sunday.
        </EmptyState>
      ) : (
        <>
          <Hero
            label="This week at a glance"
            eyebrow="Spent this week"
            numeral={<span data-testid="review-spent">{formatInr(data.spending, { paise: false })}</span>}
            caption={`money out \u00b7 ${fmtShort(data.week_start)} to ${fmtShort(data.through)}${rate === null ? " \u00b7 no income this week, so no savings rate" : ""}`}
            cells={[
              { label: "Money in", value: <Money value={data.income} paise={false} /> },
              { label: "Saved", value: <Money value={data.saved} paise={false} tone={short ? "over" : undefined} />, note: short ? "short" : undefined },
              { label: "Savings rate", value: rate !== null ? <span className={rate.startsWith("-") ? r.neg : undefined}>{rate}%</span> : "\u2014" },
            ]}
          />
          <div className={styles.twoCol}>
            <div className={styles.col}>
              <Section eyebrow={data.is_current ? "Compared with the same days last week" : "Compared with the week before"}>
                <CompareSlip review={data} />
              </Section>
              {data.top_categories.length > 0 && (
                <Section eyebrow="Where it went"><CategoryBars items={data.top_categories} /></Section>
              )}
              {data.biggest_debits.length > 0 && (
                <Section eyebrow="Biggest payments"><LedgerList items={data.biggest_debits} showDate /></Section>
              )}
            </div>
            <div className={styles.col}>
              <Attention data={data} />
            </div>
          </div>
        </>
      )}
      {data.act_insights.length > 0 && (
        <Section eyebrow="Insights worth acting on">
          <div>{data.act_insights.map((i) => <InsightRow key={i.key} insight={i} compact />)}</div>
          <LinkButton to="/insights" variant="tertiary">See all insights</LinkButton>
        </Section>
      )}
      {data.insights_error && <p className={styles.muted} style={{ marginTop: "var(--s-5)" }}>Suggestions could not be worked out this time. The rest of the page is fine.</p>}
      <Commitment week={data.week_start} last={data.last_commitment} saved={data.commitment} />
    </>
  );
}

export default function ReviewPage() {
  const meta = useMeta();
  const [{ week }, setUrl] = useUrlState({ week: "" });
  const query = useReview(week || null);
  const shown = query.data?.week_start ?? (week || meta.data?.weeks[0] || null);
  const data = query.data;
  return (
    <>
      <PageHeader
        name="Review"
        title="Weekly review"
        sub={data ? `counted through ${throughLabel(data.through)}${data.is_current ? " \u00b7 the week isn\u2019t over yet" : ""}` : undefined}
        controls={meta.data && shown ? <WeekStepper value={shown} weeks={meta.data.weeks} onChange={(w) => setUrl({ week: w })} /> : undefined}
      />
      <Loaded query={query} skeleton={<div className={styles.skelStack} aria-hidden="true"><Skeleton height={120} /><Skeleton height={90} /><Skeleton height={90} /></div>}>
        {(d) => <ReviewBody data={d} />}
      </Loaded>
    </>
  );
}
