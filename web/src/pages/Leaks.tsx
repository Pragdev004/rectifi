import { Link } from "wouter";
import { useLeaks, type LeakReport } from "../api/alerts";
import { useMeta } from "../api/queries";
import { Loaded } from "../components/Loaded";
import { Money } from "../components/Money";
import { PageHeader } from "../components/PageHeader";
import { MonthStepper } from "../components/Steppers";
import { Waterfall } from "../components/Waterfall";
import { LinkButton } from "../components/forms";
import { EmptyState, Section, Skeleton } from "../components/ui";
import { monthLabel } from "../lib/dates";
import { formatInr, formatPercent } from "../lib/money";
import { useUrlState } from "../lib/urlState";
import styles from "./pages.module.css";
import leak from "./leaks.module.css";

const INTRO =
  "Each bar starts where the one above it stopped, so you can see which small habits take the most. " +
  "Fixed rules sorted every payment; no AI is involved and every figure is worked out on your laptop.";

function projectionLine(r: LeakReport): string | null {
  const p = r.projection;
  if (!p) return null;
  const pct = p.pct_of_income ? ` That is ${formatPercent(p.pct_of_income)} of your income.` : "";
  return `At this pace you will spend ${formatInr(p.projected, { paise: false })} by the end of the month (${formatInr(p.spent, { paise: false })} in ${p.day} days).${pct}`;
}

function LeaksBody({ data }: { data: LeakReport }) {
  const noSpend = data.segments.every((s) => s.kind !== "outflow" || s.count === 0); // counts of payments, not money
  if (noSpend) {
    return (
      <EmptyState title="No spending recorded for this month." actions={<LinkButton to="/add" variant="primary">Add an entry</LinkButton>}>
        Once payments are in, this page shows where they went.
      </EmptyState>
    );
  }
  const line = projectionLine(data);
  return (
    <>
      {data.income === null && (
        <p className={styles.muted} data-testid="leaks-no-income">
          Rectifi does not know your income for this month, so there is no {"“"}Left{"”"} bar. <Link href="/alerts?tab=delivery">Type your monthly income</Link> to add it.
        </p>
      )}
      <Section eyebrow={data.income ? `Where your ${formatInr(data.income, { paise: false })} went` : "Where your money went"}>
        <Waterfall segments={data.segments} />
        {line && <p className={leak.projection} data-testid="leaks-projection">{line}</p>}
      </Section>
      <Section eyebrow="Top merchants">
        {data.merchants.length === 0 ? (
          <p className={styles.muted}>No payment this month matched a known merchant. You can teach Rectifi a name under Alerts.</p>
        ) : (
          <table className={leak.table} data-testid="leaks-merchants">
            <thead>
              <tr><th scope="col">Merchant</th><th scope="col">Orders</th><th scope="col">Spent</th><th scope="col">Of income</th></tr>
            </thead>
            <tbody>
              {data.merchants.map((m) => (
                <tr key={m.merchant}>
                  <th scope="row">{m.merchant}<span className={leak.group}>{m.group}</span></th>
                  <td>{m.orders} {m.orders === 1 ? "order" : "orders"}</td>
                  <td><Money value={m.total} paise={false} /></td>
                  <td>{m.pct_of_income ? formatPercent(m.pct_of_income) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </>
  );
}

export default function LeaksPage() {
  const meta = useMeta();
  const [{ month }, setUrl] = useUrlState({ month: "" });
  const query = useLeaks(month || null);
  const shown = query.data?.month ?? (month || meta.data?.months[0] || null);
  return (
    <>
      <PageHeader
        name="Leaks"
        title={shown ? `Where your money went in ${monthLabel(shown)}` : "Where your money went"}
        sub={INTRO}
        controls={meta.data && shown ? <MonthStepper value={shown} months={meta.data.months} onChange={(m) => setUrl({ month: m })} /> : undefined}
      />
      <Loaded query={query} skeleton={<div className={styles.skelStack} aria-hidden="true"><Skeleton height={260} /><Skeleton height={120} /></div>}>
        {(data) => <LeaksBody data={data} />}
      </Loaded>
    </>
  );
}
