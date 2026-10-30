import { useState } from "react";
import { Button } from "../forms";
import { Callout } from "../ui";
import styles from "./charts.module.css";

/** The review gate: the model's code, editable, before anything runs. An edit is checked again by the same safety rules. */
export function ReviewGate({ code, busy, onRun, onCancel }: { code: string; busy: boolean; onRun: (code: string) => void; onCancel: () => void }) {
  const [text, setText] = useState(code);
  return (
    <section className={styles.review} aria-labelledby="review-h" data-testid="chart-review">
      <h2 id="review-h">Read the code first</h2>
      <Callout tone="note" lead="Nothing has run yet.">
        This is the Python the model wrote. You can change it. It still goes through the same safety check before it runs, in a locked room with no files or network.
      </Callout>
      <label className="sr-only" htmlFor="chart-code">Chart code</label>
      <textarea id="chart-code" className={styles.codeEdit} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} autoCapitalize="off" autoCorrect="off" />
      <div className={styles.actions}>
        <Button variant="primary" loading={busy} loadingLabel="Running…" onClick={() => onRun(text)}>Run this code</Button>
        <Button variant="tertiary" onClick={onCancel} disabled={busy}>Cancel</Button>
      </div>
    </section>
  );
}
