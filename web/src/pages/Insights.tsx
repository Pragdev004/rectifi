import { Hourglass } from "lucide-react";
import { useAskStatus, useInsights } from "../api/queries";
import type { Insight, InsightsList } from "../api/types";
import { LinkButton } from "../components/forms";
import { InsightRow } from "../components/InsightRow";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { Callout, EmptyState, Section, Skeleton } from "../components/ui";
import styles from "./pages.module.css";

const INTRO =
  "What Rectifi noticed in your own numbers, and what to do about it. Fixed rules worked out every figure: " +
  "no AI decides a number, and nothing here changes your data unless you press a button.";

function Groups({ data, explainOk }: { data: InsightsList; explainOk: boolean }) {
  const rest = (i: Insight) => !i.is_data_quality;
  const groups: { heading: string; items: Insight[] }[] = [
    { heading: "Fix these first", items: data.items.filter((i) => i.is_data_quality) },
    { heading: "Act now", items: data.items.filter((i) => rest(i) && i.severity === "act") },
    { heading: "Keep an eye on", items: data.items.filter((i) => rest(i) && i.severity === "watch") },
    { heading: "Good to know", items: data.items.filter((i) => rest(i) && i.severity === "info") },
  ];
  return (
    <>
      {groups.filter((g) => g.items.length > 0).map((g) => (
        <Section key={g.heading} eyebrow={g.heading}>
          {g.heading === "Fix these first" && <p className={styles.muted}>They make every other number less trustworthy.</p>}
          <div data-testid={`group-${g.heading}`}>
            {g.items.map((i) => <InsightRow key={i.key} insight={i} explainOk={explainOk} />)}
          </div>
        </Section>
      ))}
    </>
  );
}

function InsightsBody({ data }: { data: InsightsList }) {
  const ask = useAskStatus();
  const explainOk = ask.data?.ok === true;
  if (data.span.count === 0) {
    return (
      <EmptyState title="Insights need about a month of data." actions={<LinkButton to="/add" variant="primary">Go to Add</LinkButton>}>
        Import last month{"’"}s bank statement to get started: it fills in a whole month at once. You can also add what you spend on the Add page.
      </EmptyState>
    );
  }
  const days = data.span.days;
  return (
    <>
      {days < 28 && (
        <Callout tone="near" icon={Hourglass} lead="Not much history yet." className={styles.spaced}>
          Rectifi has {days} {days === 1 ? "day" : "days"} of data. Spikes, subscriptions and the savings trend need about a month, so those checks stay quiet for now. Import last month{"’"}s statement to unlock them.
        </Callout>
      )}
      {data.items.length === 0 ? (
        <EmptyState title="Nothing to flag right now.">
          No budget is heading over, nothing looks unusual, and your records look complete. Check back after a few more days of entries.
        </EmptyState>
      ) : (
        <Groups data={data} explainOk={explainOk} />
      )}
      {!explainOk && ask.data && data.items.length > 0 && (
        <p className={styles.muted} style={{ marginTop: "var(--s-6)" }}>
          {"“"}Explain this{"”"} appears when the Ask model is on (see the Ask page). Everything above works without it.
        </p>
      )}
    </>
  );
}

export default function InsightsPage() {
  const query = useInsights();
  return (
    <>
      <PageHeader name="Insights" title="Insights" sub={INTRO} />
      <Loaded query={query} skeleton={<div className={styles.skelStack} aria-hidden="true"><Skeleton height={90} /><Skeleton height={90} /><Skeleton height={90} /></div>}>
        {(data) => <InsightsBody data={data} />}
      </Loaded>
    </>
  );
}
