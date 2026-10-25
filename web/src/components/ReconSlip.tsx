import type { ReactNode } from "react";
import type { Recon } from "../api/types";
import { fmtDay } from "../lib/dates";
import { formatInr } from "../lib/money";
import { Money } from "./Money";
import { Stamp } from "./ui";
import styles from "./ReconSlip.module.css";

/** The proof: opening + credits − debits = expected closing, set against what the bank says. A single ink rule above the total,
 *  a double rule under the final figure, as in an accountant's columnar paper. */
export function ReconSlip({ recon, actions, popBalanced, title }: { recon: Recon; actions?: ReactNode; popBalanced?: boolean; title?: string }) {
  const gapText = recon.gap.startsWith("-") ? recon.gap.slice(1) : recon.gap;
  return (
    <div className={styles.slip} data-testid="recon-slip" data-balanced={recon.balanced}>
      {title && <p className={styles.head}><strong>{title}</strong></p>}
      <dl>
        <div className={styles.line}><dt>Opening balance {"·"} {fmtDay(recon.opening_date)}</dt><dd><Money value={recon.opening} /></dd></div>
        <div className={styles.line}><dt>+ credits Rectifi recorded</dt><dd><Money value={recon.credits} /></dd></div>
        <div className={styles.line}><dt>{"−"} debits Rectifi recorded</dt><dd><Money value={recon.debits} /></dd></div>
        <div className={`${styles.line} ${styles.total}`}><dt>= expected closing</dt><dd><Money value={recon.expected_closing} /></dd></div>
        <div className={`${styles.line} ${styles.final}`}><dt>Bank's closing {"·"} {fmtDay(recon.closing_date)}</dt><dd><Money value={recon.closing} /></dd></div>
      </dl>
      <p className={styles.count}>Adds up {recon.count} {recon.count === 1 ? "entry" : "entries"} between the two bank balances.</p>
      <div className={styles.verdict}>
        {recon.balanced ? (
          <>
            <p><Stamp tone="ok" pop={popBalanced}>Balanced</Stamp></p>
            <p className={styles.sentence}>Every rupee is accounted for.</p>
          </>
        ) : (
          <>
            <p><Stamp tone="over">Gap {formatInr(gapText)}</Stamp></p>
            <p className={styles.sentence}>{recon.explanation}</p>
          </>
        )}
        {actions && <div className={styles.actions}>{actions}</div>}
      </div>
    </div>
  );
}
