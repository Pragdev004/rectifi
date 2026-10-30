import type { Interrupt } from "../../api/chat";
import { Button } from "../forms";
import { Callout } from "../ui";
import c from "./chat.module.css";

/** A question the graph is waiting on. The answer goes back as `value` to POST …/resume. */
export function InterruptCard({ interrupt, busy, onAnswer }: { interrupt: Interrupt; busy: boolean; onAnswer: (value: unknown) => void }) {
  const p = interrupt.payload;
  const text = typeof p.text === "string" ? p.text : "";
  if (interrupt.kind === "memory") {
    return (
      <div className={c.interrupt} data-testid="interrupt-card">
        <Callout tone="near" lead="Save this to memory?" role="status" actions={
          <>
            <Button variant="primary" disabled={busy} onClick={() => onAnswer(true)}>Save</Button>
            <Button disabled={busy} onClick={() => onAnswer(false)}>Don{"’"}t</Button>
          </>
        }>
          <blockquote>{text}</blockquote>
          <p className={c.hint}>Memories never supply amounts. Figures always come from your records. You can edit or delete this on the Memory page.</p>
        </Callout>
      </div>
    );
  }
  if (interrupt.kind === "web_consent") {
    const query = typeof p.query === "string" ? p.query : text;
    return (
      <div className={c.interrupt} data-testid="interrupt-card">
        <Callout tone="near" lead={`Search the web for “${query}”?`} role="status" actions={
          <>
            <Button variant="primary" disabled={busy} onClick={() => onAnswer(true)}>Search</Button>
            <Button disabled={busy} onClick={() => onAnswer(false)}>Answer without it</Button>
          </>
        }>
          <p className={c.hint}>Your question would leave this laptop. Nothing else is sent.</p>
        </Callout>
      </div>
    );
  }
  if (interrupt.kind === "review_code" || typeof p.code === "string") {
    return (
      <div className={c.interrupt} data-testid="interrupt-card">
        <Callout tone="near" lead="Read this chart code before it runs" role="status" actions={
          <>
            <Button variant="primary" disabled={busy} onClick={() => onAnswer(true)}>Run this code</Button>
            <Button disabled={busy} onClick={() => onAnswer(false)}>Cancel</Button>
          </>
        }>
          <pre><code>{String(p.code ?? "")}</code></pre>
        </Callout>
      </div>
    );
  }
  return (
    <div className={c.interrupt} data-testid="interrupt-card">
      <Callout tone="near" lead={typeof p.prompt === "string" ? p.prompt : "Rectifi is waiting for you."} role="status" actions={
        <>
          <Button variant="primary" disabled={busy} onClick={() => onAnswer(true)}>Continue</Button>
          <Button disabled={busy} onClick={() => onAnswer(false)}>No</Button>
        </>
      }>
        {text && <blockquote>{text}</blockquote>}
      </Callout>
    </div>
  );
}
