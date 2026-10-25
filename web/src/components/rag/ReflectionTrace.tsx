import { Disclosure, Stamp, type Tone } from "../ui";
import type { TraceEvent } from "../../api/knowledge";
import styles from "./rag.module.css";

const GOOD = new Set(["CORRECT", "FULLY", "YES", "TRACED", "NONE", "READ-ONLY"]);
const WARN = new Set(["AMBIGUOUS", "PARTLY", "UNSURE", "WITH CAVEAT", "DECLINED", "ASKED"]);
const BAD = new Set(["INCORRECT", "NO", "NOT TRACED", "WITHHELD", "NOT FOUND"]);

/** The colour of a trace stamp. A stamp always carries a word too, so colour is never the only signal. */
export function toneFor(verdict: string): Tone {
  const v = verdict.toUpperCase();
  if (GOOD.has(v)) return "ok";
  if (WARN.has(v)) return "near";
  if (BAD.has(v)) return "over";
  return "neutral";
}

const SHOUT = new Set(["CRAG", "SUPPORTED", "USEFUL", "ANSWER"]);

/** The words after the stamp: the verdict (and detail) in plain English. AMOUNTS reads as one sentence. */
export function rowText(e: TraceEvent): { main: string; extra: string } {
  if (e.step === "AMOUNTS") return { main: e.detail || e.verdict, extra: "" };
  const main = SHOUT.has(e.step) ? e.verdict : e.verdict.charAt(0).toUpperCase() + e.verdict.slice(1);
  return { main, extra: e.detail };
}

/** "How this answer was checked": a ruled list, one row per step the graph took. Self-RAG's checks are done by asking a small
 *  model, not by a model trained for it, and the page says so. */
export function ReflectionTrace({ trace, defaultOpen = false }: { trace: TraceEvent[]; defaultOpen?: boolean }) {
  if (trace.length === 0) return null;
  return (
    <Disclosure summary="How this answer was checked" defaultOpen={defaultOpen}>
      <ol className={styles.trace} data-testid="reflection-trace">
        {trace.map((e, i) => {
          const { main, extra } = rowText(e);
          return (
            <li key={i} className={styles.traceRow}>
              <Stamp tone={toneFor(e.verdict)} className={styles.traceStamp}>{e.step}</Stamp>
              <span className={styles.traceText}>
                {main}
                {extra && <span className={styles.traceExtra}> {"·"} {extra}</span>}
              </span>
            </li>
          );
        })}
      </ol>
      <p className={styles.traceNote}>
        These checks are made by asking a small model short questions. They help, and they can be wrong. The check on ₹ amounts is plain code, not a model.
      </p>
    </Disclosure>
  );
}
