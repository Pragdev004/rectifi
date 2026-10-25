import type { Txn } from "../api/types";
import { dayHeader, fmtShort } from "../lib/dates";
import { Money } from "./Money";
import styles from "./LedgerList.module.css";

export const payeeOf = (t: Txn) => t.payee || t.description || "(no payee)";

export function TxnAmount({ t, paise = true }: { t: Txn; paise?: boolean }) {
  const credit = t.direction === "credit";
  return (
    <span className={styles.amount}>
      <Money value={t.amount} paise={paise} sign={credit} tone={credit ? "credit" : undefined} />
      {credit && <span className={styles.in}>IN</span>}
    </span>
  );
}

function Row({ t, showDate, onOpen }: { t: Txn; showDate?: boolean; onOpen?: (t: Txn) => void }) {
  const inner = (
    <>
      <span className={styles.payee}>{payeeOf(t)}</span>
      <TxnAmount t={t} />
      <span className={styles.meta}>
        {showDate && <>{fmtShort(t.date)} {"· "}</>}
        {t.category ?? "Uncategorised"} {"· "}
        {t.account ?? <span className={styles.unlinked}>Not linked</span>}
      </span>
    </>
  );
  return (
    <li className={styles.row} data-testid="ledger-row" data-txn-id={t.id}>
      {onOpen ? (
        <button type="button" className={styles.rowInner} onClick={() => onOpen(t)} aria-label={`${payeeOf(t)}, open details`}>{inner}</button>
      ) : (
        <div className={styles.rowInner}>{inner}</div>
      )}
    </li>
  );
}

/** A phone-friendly list: payee and amount, then category and account. With `grouped`, entries sit under sticky day headers. */
export function LedgerList({ items, grouped, showDate, onOpen }: { items: Txn[]; grouped?: boolean; showDate?: boolean; onOpen?: (t: Txn) => void }) {
  if (!grouped) {
    return <ul className={styles.list}>{items.map((t) => <Row key={t.id} t={t} showDate={showDate} onOpen={onOpen} />)}</ul>;
  }
  const days: { day: string; rows: Txn[] }[] = [];
  for (const t of items) {
    const last = days[days.length - 1];
    if (last && last.day === t.date) last.rows.push(t);
    else days.push({ day: t.date, rows: [t] });
  }
  return (
    <div className={styles.list}>
      {days.map((d) => (
        <section key={d.day} aria-label={dayHeader(d.day)}>
          <h3 className={styles.dayHead}>{dayHeader(d.day)}</h3>
          <ul>{d.rows.map((t) => <Row key={t.id} t={t} onOpen={onOpen} />)}</ul>
        </section>
      ))}
    </div>
  );
}
