import type { ReactNode } from "react";
import { Eyebrow } from "./ui";
import styles from "./Hero.module.css";

export interface HeroCellData {
  label: string;
  value: ReactNode;
  /** Shown under the figure, in words, e.g. "short" for a negative Saved. */
  note?: string;
}

/** The big number and three ruled cells, like the totals strip of a passbook page. */
export function Hero({
  eyebrow, numeral, caption, cells, label,
}: {
  eyebrow: string;
  numeral: ReactNode;
  caption: ReactNode;
  cells: HeroCellData[];
  label: string;
}) {
  return (
    <section className={styles.hero} aria-label={label}>
      <div>
        <Eyebrow>{eyebrow}</Eyebrow>
        <p className={styles.numeral}>{numeral}</p>
        <p className={styles.caption}>{caption}</p>
      </div>
      <dl className={styles.cells}>
        {cells.map((c) => (
          <div key={c.label} className={styles.cell}>
            <dt><Eyebrow as="span">{c.label}</Eyebrow></dt>
            <dd>
              {c.value}
              {c.note && <span className={styles.short}>{c.note}</span>}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
