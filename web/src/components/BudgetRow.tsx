import type { ReactNode } from "react";
import type { BudgetRowData } from "../api/types";
import { cx } from "../lib/cx";
import { formatPercent, formatInr } from "../lib/money";
import { Money } from "./Money";
import { Stamp } from "./ui";
import styles from "./BudgetRow.module.css";

// Percentages arrive as text ("78.0"). Turning them into a number here only decides how wide to draw a bar.
const draw = (p: string | null | undefined) => Math.max(0, Math.min(100, p ? Number.parseFloat(p) : 0));

export function BudgetBar({ row, pace, monthName, animate }: { row: BudgetRowData; pace: string | null; monthName?: string; animate?: boolean }) {
  const label = `${row.category}: ${formatPercent(row.percent)} of budget used${pace ? `, ${formatPercent(pace)} of ${monthName ?? "the month"} gone` : ""}`;
  return (
    <div className={cx(styles.bar, animate && styles.animate)} role="img" aria-label={label}>
      <div className={cx(styles.fill, row.state === "near" && styles.near, row.state === "over" && styles.over)} style={{ width: `${draw(row.percent)}%` }} />
      {pace && <div className={styles.tick} style={{ left: `${draw(pace)}%` }} />}
    </div>
  );
}

export function BudgetRow({
  row, pace, monthName, animate, children,
}: {
  row: BudgetRowData;
  pace: string | null;
  monthName?: string;
  animate?: boolean;
  children?: ReactNode;
}) {
  const over = row.state === "over";
  return (
    <div className={styles.row} data-testid={`budget-${row.category}`}>
      <div className={styles.top}>
        <span className={styles.name}>
          {row.category}
          {row.state === "over" && <Stamp tone="over">Over</Stamp>}
          {row.state === "near" && <Stamp tone="near">Near</Stamp>}
        </span>
        <span className={styles.nums}>
          <strong><Money value={row.spent} paise={false} /></strong> of <Money value={row.limit} paise={false} />
        </span>
      </div>
      <BudgetBar row={row} pace={pace} monthName={monthName} animate={animate} />
      <p className={styles.meta}>
        {formatPercent(row.percent)} used {"· "}
        {over ? (
          <span className={styles.overText}>over by {formatInr(row.remaining.replace("-", ""), { paise: false })}</span>
        ) : (
          <>{formatInr(row.remaining, { paise: false })} left</>
        )}
      </p>
      {children}
    </div>
  );
}

export function BudgetLegend({ day, days }: { day: number; days: number }) {
  if (day <= 0) return <p className={styles.legend}>This month has not started yet.</p>;
  return (
    <p className={styles.legend}>
      <span className={styles.tickKey} aria-hidden="true" />marks day {day} of {days}
    </p>
  );
}
