import { Link } from "wouter";
import { useToday } from "../api/alerts";
import { formatInr } from "../lib/money";
import { Money } from "./Money";
import { Disclosure, Eyebrow, Skeleton } from "./ui";
import styles from "./TodayStrip.module.css";

const SOURCE_WORDS = { plan: "your spending plan", budgets: "this month's budgets added up", income: "your monthly income" } as const;

/** "Safe to spend today", "Left today" and today's small spends: the day's budget at a glance. Every figure comes from the server. */
export function TodayStrip() {
  const query = useToday();
  if (query.isPending) return <div className={styles.strip} aria-hidden="true"><Skeleton height={72} /></div>;
  if (query.isError || !query.data) return null; // the Overview is fine without it
  const { safe, small_spends: small, unread } = query.data;
  const over = safe?.over_today ?? false;

  return (
    <section className={styles.strip} aria-label="Today" data-testid="today-strip">
      {safe ? (
        <>
          <div className={styles.figures}>
            <div className={styles.cell}>
              <Eyebrow as="span">Safe to spend today</Eyebrow>
              <p className={styles.big} data-testid="today-safe"><Money value={safe.daily} paise={false} /></p>
              <p className={styles.note}>{safe.days_left === 1 ? "the last day of the month" : `a day, for the next ${safe.days_left} days`}</p>
            </div>
            <div className={styles.cell}>
              <Eyebrow as="span">{over ? "Over today by" : "Left today"}</Eyebrow>
              <p className={styles.big} data-testid="today-left">
                <Money value={over ? safe.left_today.replace("-", "") : safe.left_today} paise={false} tone={over ? "over" : undefined} />
              </p>
              <p className={styles.note}>{formatInr(safe.spent_today, { paise: false })} spent so far today</p>
            </div>
          </div>
          {small.length > 0 && (
            <ul className={styles.chips} aria-label="Small payments today">
              {small.map((s) => (
                <li key={s.txn_id} className={styles.chip}>{s.label} <Money value={s.amount} paise={false} /></li>
              ))}
            </ul>
          )}
          <Disclosure summary="How this is worked out">
            <p className={styles.formula} data-testid="today-formula">
              {formatInr(safe.spendable, { paise: false })} ({SOURCE_WORDS[safe.source]}) {"−"} {formatInr(safe.spent_before_today, { paise: false })} spent before today {"−"}{" "}
              {formatInr(safe.committed, { paise: false })} of bills still due, shared over {safe.days_left} {safe.days_left === 1 ? "day" : "days"} (today included), rounded down.
            </p>
            {safe.committed_items.length > 0 && (
              <p className={styles.note}>
                Bills still due: {safe.committed_items.map((i) => `${i.name} ${formatInr(i.amount, { paise: false })} (around the ${i.day}th)`).join(", ")}. Rectifi only knows the ones it has seen repeat every month.
              </p>
            )}
          </Disclosure>
        </>
      ) : (
        <div className={styles.empty}>
          <Eyebrow as="span">Safe to spend today</Eyebrow>
          <p className={styles.emptyText}>Set a budget or your monthly income to see this.</p>
          <p className={styles.links}>
            <Link href="/budgets">Set a budget</Link> {"·"} <Link href="/alerts?tab=delivery">Enter your monthly income</Link>
          </p>
        </div>
      )}
      <p className={styles.foot}>
        <Link href="/alerts" className={styles.alertLink}>{unread > 0 ? `${unread} new ${unread === 1 ? "alert" : "alerts"}` : "Leak alerts"}</Link>
        {" "}{"·"}{" "}
        <Link href="/leaks">Where your money went</Link>
      </p>
    </section>
  );
}
