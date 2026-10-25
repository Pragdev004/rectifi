import type { Review } from "../api/types";
import { fmtShort } from "../lib/dates";
import { formatInr, formatPercent } from "../lib/money";
import { Money } from "./Money";
import { Stamp } from "./ui";
import styles from "./ReconSlip.module.css";

/** This week's money out set against the same days a week earlier, as a small sum. Only the server decides up, down or same. */
export function CompareSlip({ review: r }: { review: Review }) {
  const prev = `${fmtShort(r.prev_start)} \u2013 ${fmtShort(r.prev_end)}`;
  const comparable = r.prev_spending !== "0.00";
  const pct = r.spending_change_pct ? formatPercent(r.spending_change_pct.replace(/^-/, "")) : "";
  const size = formatInr(r.spending_change.replace(/^-/, ""));
  let verdict;
  if (!comparable) {
    verdict = <><p><Stamp tone="near">Nothing to compare</Stamp></p><p className={styles.sentence}>No spending was recorded for {prev}.</p></>;
  } else if (r.trend === "flat") {
    verdict = <><p><Stamp tone="ok">Same</Stamp></p><p className={styles.sentence}>Spending is the same as {prev}.</p></>;
  } else {
    const up = r.trend === "up";
    verdict = (
      <>
        <p><Stamp tone={up ? "over" : "ok"}>{up ? "Up" : "Down"} {pct}</Stamp></p>
        <p className={styles.sentence}>{up ? "More" : "Less"} than {prev} by {size}.</p>
      </>
    );
  }
  return (
    <div className={styles.slip} data-testid="compare-slip" data-trend={r.trend}>
      <dl>
        <div className={styles.line}><dt>Money out {"\u00b7"} {fmtShort(r.week_start)} {"\u2013"} {fmtShort(r.through)}</dt><dd><Money value={r.spending} /></dd></div>
        <div className={styles.line}><dt>Money out {"\u00b7"} {prev}</dt><dd><Money value={r.prev_spending} /></dd></div>
        {comparable && (
          <div className={`${styles.line} ${styles.final}`}><dt>= change</dt><dd><Money value={r.spending_change} sign /></dd></div>
        )}
      </dl>
      <div className={styles.verdict}>{verdict}</div>
    </div>
  );
}
