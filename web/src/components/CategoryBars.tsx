import type { CategoryShare } from "../api/types";
import { formatPercent } from "../lib/money";
import { Money } from "./Money";
import styles from "./CategoryBars.module.css";

const share = (p: string | null) => (p ? Number.parseFloat(p) : 0);

/** Where the money went. Each bar is drawn relative to the biggest category, so small ones stay readable. */
export function CategoryBars({ items, noun = "money out" }: { items: CategoryShare[]; noun?: string }) {
  const max = Math.max(...items.map((i) => share(i.share)), 0.0001);
  return (
    <ul className={styles.list}>
      {items.map((c) => (
        <li key={c.category}>
          <div className={styles.head}>
            <span className={styles.cat}>{c.category}</span>
            <Money value={c.amount} paise={false} />
          </div>
          <div className={styles.track} role="img" aria-label={`${c.category}: ${formatPercent(c.share)} of ${noun}`}>
            <div className={styles.fill} style={{ width: `${Math.max(2, (share(c.share) / max) * 100)}%` }} />
          </div>
          <p className={styles.sub}>{formatPercent(c.share)} of {noun} {"· "}{c.count} {c.count === 1 ? "entry" : "entries"}</p>
        </li>
      ))}
    </ul>
  );
}
