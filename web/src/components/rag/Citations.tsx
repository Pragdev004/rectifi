import { Stamp, type Tone } from "../ui";
import type { Citation } from "../../api/knowledge";
import styles from "./rag.module.css";

const TONE: Record<string, Tone> = { guide: "accent", note: "near", txn: "ok", upload: "neutral", web: "over", tool: "ok" };

/** The small raised [1] after a claim. It jumps to the matching source below the answer. */
export function CitationMark({ n, idPrefix = "source" }: { n: number; idPrefix?: string }) {
  return (
    <sup className={styles.mark}>
      <a href={`#${idPrefix}-${n}`} aria-label={`Source ${n}`}>[{n}]</a>
    </sup>
  );
}

/** "Sources": one ruled row per cited passage, with where it came from (GUIDE, YOUR NOTE, YOUR ENTRY ...). */
export function Citations({ citations, idPrefix = "source" }: { citations: Citation[]; idPrefix?: string }) {
  if (citations.length === 0) return null;
  return (
    <section className={styles.sources} aria-label="Sources" data-testid="citations">
      <p className={styles.eyebrow}>Sources</p>
      <ol className={styles.sourceList}>
        {citations.map((c, i) => (
          <li key={`${c.source}-${c.chunk}-${i}`} id={c.n ? `${idPrefix}-${c.n}` : undefined} className={styles.source}>
            <span className={styles.sourceN}>{c.n ? `[${c.n}]` : ""}</span>
            <div className={styles.sourceBody}>
              <p className={styles.sourceHead}>
                <Stamp tone={TONE[c.corpus] ?? "neutral"}>{c.label}</Stamp>
                <span className={styles.sourceTitle}>{c.title || c.source}</span>
              </p>
              {c.quote && <p className={styles.quote}>{c.quote}</p>}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
