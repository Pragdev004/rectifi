import { ArrowRight, Plus, Upload } from "lucide-react";
import { Link } from "wouter";
import type { Overview } from "../api/types";
import { useMeta, useOverview } from "../api/queries";
import { BudgetLegend, BudgetRow } from "../components/BudgetRow";
import { CategoryBars } from "../components/CategoryBars";
import { LinkButton } from "../components/forms";
import { Hero } from "../components/Hero";
import { InsightRow } from "../components/InsightRow";
import { LedgerList } from "../components/LedgerList";
import { Loaded } from "../components/Loaded";
import { Money } from "../components/Money";
import { PageHeader } from "../components/PageHeader";
import { PaceChart } from "../components/PaceChart";
import { ReconSlip } from "../components/ReconSlip";
import { TodayStrip } from "../components/TodayStrip";
import { MonthStepper } from "../components/Steppers";
import { EmptyState, Section, Skeleton } from "../components/ui";
import { monthLabel, monthName } from "../lib/dates";
import { useIsDesktop } from "../lib/media";
import { useUrlState } from "../lib/urlState";
import styles from "./pages.module.css";

function OverviewSkeleton() {
  return (
    <div className={styles.skelStack} aria-hidden="true">
      <Skeleton height={120} />
      <Skeleton height={20} width="40%" />
      <Skeleton height={64} />
      <Skeleton height={64} />
      <Skeleton height={64} />
    </div>
  );
}

function savingsCaption(rate: string | null): string {
  if (rate === null) return "no income recorded yet — add your stipend or salary to see a savings rate";
  return rate.startsWith("-") ? "spending is above income this month" : "of your income saved this month";
}

function OverviewBody({ data }: { data: Overview }) {
  const desktop = useIsDesktop();
  const { summary, budgets, pace, recent, proofs } = data;
  const rate = summary.savings_rate;
  const short = summary.saved.startsWith("-");

  if (summary.count === 0) {
    return (
      <EmptyState
        title="Nothing recorded for this month yet."
        actions={
          <>
            <LinkButton to="/add" variant="primary" icon={<Plus size={16} strokeWidth={2} aria-hidden="true" />}>Add an entry</LinkButton>
            <LinkButton to="/add?tab=import" icon={<Upload size={16} strokeWidth={1.5} aria-hidden="true" />}>Import a statement</LinkButton>
          </>
        }
      >
        Start in Add: type an expense, paste a bank SMS, or import your bank statement (CSV/Excel). Or open Ask and say {"“"}spent 250 on swiggy{"”"}.
      </EmptyState>
    );
  }

  const hero = (
    <Hero
      label="This month at a glance"
      eyebrow="Savings rate"
      numeral={
        rate !== null ? (
          <span data-testid="hero-rate">{rate}<small>%</small></span>
        ) : (
          <span data-testid="hero-rate">{"—"}</span>
        )
      }
      caption={savingsCaption(rate)}
      cells={[
        { label: "Money in", value: <Money value={summary.income} paise={false} data-testid="hero-income" /> },
        { label: "Money out", value: <Money value={summary.spending} paise={false} data-testid="hero-spending" /> },
        { label: "Saved", value: <Money value={summary.saved} paise={false} tone={short ? "over" : undefined} data-testid="hero-saved" />, note: short ? "short" : undefined },
      ]}
    />
  );

  const worth = data.insights_error ? (
    <p className={styles.muted} style={{ marginTop: "var(--s-5)" }}>Suggestions could not be worked out this time. The rest of the page is fine.</p>
  ) : data.top_insights && data.top_insights.length > 0 ? (
    <Section eyebrow="Worth a look">
      <div>{data.top_insights.map((i) => <InsightRow key={i.key} insight={i} compact />)}</div>
      <Link href="/insights" className={styles.moreLink}>All insights <ArrowRight size={14} strokeWidth={1.5} aria-hidden="true" style={{ verticalAlign: "-2px" }} /></Link>
    </Section>
  ) : null;

  const budgetsSection = (
    <Section eyebrow="Budgets">
      {budgets.categories.length > 0 ? (
        <>
          <BudgetLegend day={budgets.pace_day} days={budgets.days_in_month} />
          {budgets.categories.map((b) => (
            <BudgetRow key={b.category} row={b} pace={budgets.pace} monthName={monthName(budgets.month)} />
          ))}
        </>
      ) : (
        <EmptyState title="No budgets for this month.">
          Set limits on the Budgets page {"—"} that{"’"}s what turns tracking into a coach.{" "}
          <Link href={`/budgets?month=${data.month}`}>Set budgets</Link>
        </EmptyState>
      )}
    </Section>
  );

  const where = (
    <Section eyebrow="Where it went">
      {summary.by_category.length > 0 ? (
        <CategoryBars items={summary.by_category} />
      ) : (
        <p className={styles.muted}>No spending this month. Only money coming in so far; spending shows up here as soon as you record some.</p>
      )}
    </Section>
  );

  const paceSection =
    budgets.total_limit !== "0.00" && pace.days.length > 0 ? (
      <Section eyebrow="Spending vs budget pace">
        <PaceChart pace={pace} daysInMonth={budgets.days_in_month} />
      </Section>
    ) : null;

  const latest = (
    <Section eyebrow="Latest entries">
      <LedgerList items={recent} showDate />
      <Link href={`/ledger?period=${data.month}`} className={styles.moreLink}>Open ledger <ArrowRight size={14} strokeWidth={1.5} aria-hidden="true" style={{ verticalAlign: "-2px" }} /></Link>
    </Section>
  );

  const provable = proofs.filter((p) => p.reconcile);
  const proof = (
    <Section eyebrow="Proof of completeness">
      {provable.length > 0 ? (
        provable.map((p) => (
          <div key={p.account.id}>
            <p className={styles.proofName}>{p.account.name}{p.account.last4 && <span className="figures" style={{ color: "var(--c-muted)", fontWeight: 400 }}> {"··"}{p.account.last4}</span>}</p>
            {p.reconcile && (
              <ReconSlip
                recon={p.reconcile}
                actions={
                  <>
                    <LinkButton to={`/ledger?account=${p.account.id}&period=all`} variant="secondary">Open this account in the Ledger</LinkButton>
                    <LinkButton to="/add?tab=import" variant="tertiary">Import a statement</LinkButton>
                  </>
                }
              />
            )}
          </div>
        ))
      ) : (
        <EmptyState
          title="Not proven complete yet."
          actions={
            <>
              <LinkButton to="/accounts" variant="secondary">Record a balance</LinkButton>
              <LinkButton to="/add?tab=import" variant="secondary">Import a statement</LinkButton>
            </>
          }
        >
          Import a bank statement with a balance column (or record two balances on Accounts) and Rectifi will prove {"—"} with arithmetic {"—"} that no transaction is missing.
        </EmptyState>
      )}
    </Section>
  );

  if (desktop) {
    return (
      <>
        {hero}
        {worth}
        <div className={styles.twoCol}>
          <div className={styles.col}>{budgetsSection}{where}{paceSection}</div>
          <div className={styles.col}>{latest}{proof}</div>
        </div>
      </>
    );
  }
  return <>{hero}{worth}{budgetsSection}{proof}{latest}{where}{paceSection}</>;
}

export default function OverviewPage() {
  const meta = useMeta();
  const [{ month }, setUrl] = useUrlState({ month: "" });
  const query = useOverview(month || null);
  const shown = query.data?.month ?? (month || meta.data?.months[0] || null);
  return (
    <>
      <PageHeader
        name="Overview"
        title={shown ? monthLabel(shown) : "Overview"}
        controls={meta.data && shown ? <MonthStepper value={shown} months={meta.data.months} onChange={(m) => setUrl({ month: m })} /> : undefined}
      />
      <TodayStrip />
      <Loaded query={query} skeleton={<OverviewSkeleton />}>{(data) => <OverviewBody data={data} />}</Loaded>
    </>
  );
}
