import type { LeakSegment } from "../api/alerts";
import { cx } from "../lib/cx";
import { formatInr } from "../lib/money";
import { Money } from "./Money";
import styles from "./Waterfall.module.css";

// The server works out every amount, start and end (Decimal). This file only draws them. The one number conversion is `pos`:
// it turns a money string into a pixel position on a 0..1000 line. It never adds, subtracts or compares amounts.
const pos = (money: string, scale: number): number => (scale > 0 ? (Number.parseFloat(money) / scale) * 1000 : 0);

/** The widest figure on the page (a start or an end), only used to set the length of the line. */
function widest(segments: LeakSegment[]): number {
  let top = 0;
  for (const s of segments) top = Math.max(top, Math.abs(Number.parseFloat(s.start)), Math.abs(Number.parseFloat(s.end)));
  return top;
}

function sentence(s: LeakSegment): string {
  if (s.kind === "income") return `${s.label}: ${formatInr(s.amount, { paise: false })} came in.`;
  if (s.kind === "left") return s.amount.startsWith("-") ? `${s.label}: spending went ${formatInr(s.amount.replace("-", ""), { paise: false })} past income.` : `${s.label}: ${formatInr(s.amount, { paise: false })} stayed.`;
  const count = s.count === 1 ? "1 payment" : `${s.count} payments`;
  return `${s.label}: ${formatInr(s.amount, { paise: false })} in ${count}.`;
}

/** Where the month's money went, as rows. Each bar starts where the one above it ended, like a waterfall on its side. */
export function Waterfall({ segments }: { segments: LeakSegment[] }) {
  const scale = widest(segments);
  return (
    <ol className={styles.list} data-testid="waterfall" aria-label="Where the month's money went">
      {segments.map((s) => {
        const a = pos(s.start, scale);
        const b = pos(s.end, scale);
        const x = Math.min(a, b);
        const width = Math.max(Math.abs(b - a), s.kind === "outflow" && s.amount === "0.00" ? 0 : 3);
        return (
          <li key={s.key} className={styles.row} data-key={s.key} data-testid={`wf-${s.key}`}>
            <span className={styles.label}>{s.label}</span>
            <svg className={styles.svg} viewBox="0 0 1000 18" preserveAspectRatio="none" aria-hidden="true" focusable="false">
              <rect className={styles.track} x="0" y="6" width="1000" height="6" />
              {width > 0 && <rect className={cx(styles.bar, styles[s.key])} x={x.toFixed(1)} y="1" width={width.toFixed(1)} height="16" />}
            </svg>
            <span className={styles.amount}>
              <Money value={s.amount} paise={false} tone={s.kind === "left" && s.amount.startsWith("-") ? "over" : undefined} />
              {s.kind === "outflow" && s.count > 0 && <span className={styles.count}>{s.count === 1 ? "1 payment" : `${s.count} payments`}</span>}
            </span>
            <span className="sr-only">{sentence(s)}</span>
          </li>
        );
      })}
    </ol>
  );
}
