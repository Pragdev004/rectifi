import { formatInr, splitMoney, NO_VALUE } from "../lib/money";
import { cx } from "../lib/cx";
import styles from "./Money.module.css";

/** An amount from the server, shown in the figures face. Whole rupees by default in the hero and charts; paise in ledgers and slips.
 *  In ledgers the paise are drawn smaller and lighter so the whole rupees scan first. */
export function Money({
  value, paise = true, sign = false, tone, className, ...rest
}: {
  value: string | null | undefined;
  paise?: boolean;
  sign?: boolean;
  tone?: "credit" | "over" | "muted";
  className?: string;
  "data-testid"?: string;
}) {
  const parts = value ? splitMoney(value) : null;
  const toneClass = tone ? styles[tone] : undefined;
  if (!value || !parts) {
    return <span className={cx(styles.money, "figures", toneClass, className)} {...rest}>{formatInr(value ?? null, { paise, sign })}</span>;
  }
  const whole = formatInr(value, { paise: false, sign });
  return (
    <span className={cx(styles.money, "figures", toneClass, className)} {...rest}>
      {whole}
      {paise && <span className={styles.paise}>.{parts.paise}</span>}
    </span>
  );
}

export { NO_VALUE };
