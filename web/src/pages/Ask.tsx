import { useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Bot, RotateCcw, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, get, post } from "../api/client";
import { streamAsk } from "../api/ndjson";
import { refreshBooks, useAskStatus } from "../api/queries";
import type { AskStatus, AskStep, TranscriptItem } from "../api/types";
import { Button, LinkButton } from "../components/forms";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { Callout, Disclosure, EmptyState, Eyebrow, Skeleton, Stamp } from "../components/ui";
import { cx } from "../lib/cx";
import { Markdown } from "../lib/markdown";
import { formatInr } from "../lib/money";
import styles from "./pages.module.css";
import a from "./ask.module.css";

const STORAGE_KEY = "rectifi.ask.thread";
const EXAMPLES = [
  "spent 250 on swiggy",
  "got 8000 stipend today",
  "how much did I spend on food this month?",
  "set a 3000 budget for food",
  "am I over budget anywhere?",
];
const MAX_TEXT = 4000;

// The thread id is the only thing kept in the browser (the conversation itself is in the database).
function readThread(): string | null {
  try { return window.localStorage.getItem(STORAGE_KEY); } catch { return null; }
}
function writeThread(id: string | null) {
  try { if (id) window.localStorage.setItem(STORAGE_KEY, id); else window.localStorage.removeItem(STORAGE_KEY); } catch { /* storage blocked: the chat just starts fresh next time */ }
}

type Pending = { steps: { tool: string; error: boolean }[]; startedAt: number };

function useElapsed(since: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (since === null) return;
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [since]);
  return since === null ? 0 : Math.max(0, Math.floor((now - since) / 1000));
}

// ── transcript ──

function Steps({ steps }: { steps: AskStep[] }) {
  if (steps.length === 0) return null;
  return (
    <Disclosure summary={`What Rectifi did · ${steps.length} ${steps.length === 1 ? "step" : "steps"}`}>
      <ul className={a.steps} data-testid="ask-steps">
        {steps.map((s, i) => (
          <li key={i}>
            <p className={a.stepHead}><code>{s.tool}</code>{s.error && <Stamp tone="over">Refused</Stamp>}</p>
            <pre className={a.stepOut}>{s.output}</pre>
          </li>
        ))}
      </ul>
    </Disclosure>
  );
}

function Turn({ item }: { item: TranscriptItem }) {
  if (item.role === "user") {
    return (
      <div className={a.turn} data-role="user">
        <Eyebrow>You</Eyebrow>
        <p className={a.user}>{item.text}</p>
      </div>
    );
  }
  return (
    <div className={a.turn} data-role="assistant">
      <Eyebrow>Rectifi</Eyebrow>
      <Markdown text={item.text} className={a.md} />
      {item.unverified.length > 0 && (
        <p className={a.unverified} data-testid="not-verified">
          <TriangleAlert size={16} strokeWidth={1.5} aria-hidden="true" />
          <span>
            <strong>Not verified: {item.unverified.map((v) => formatInr(v)).join(", ")}.</strong>{" "}
            I can{"’"}t match this to anything you typed or to a number Rectifi worked out, so check it before you rely on it.
          </span>
        </p>
      )}
      <Steps steps={item.steps} />
    </div>
  );
}

function Working({ pending }: { pending: Pending }) {
  const secs = useElapsed(pending.startedAt);
  return (
    <div className={a.working} role="status" aria-live="polite" data-testid="ask-working">
      <p>Rectifi is working{"…"}{secs >= 5 && <span className={a.elapsed}> {secs} s</span>}</p>
      {pending.steps.length > 0 && (
        <ul>{pending.steps.map((s, i) => <li key={i}>{s.error ? "✗" : "✓"} <code>{s.tool}</code></li>)}</ul>
      )}
    </div>
  );
}

// ── composer ──

function Composer({ busy, onSend }: { busy: boolean; onSend: (text: string) => void }) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  const grow = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 6 * 24 + 22)}px`;
  }, []);
  useEffect(grow, [text, grow]);
  function submit() {
    const t = text.trim();
    if (!t || busy) return;
    onSend(t);
    setText("");
  }
  return (
    <form className={a.composer} onSubmit={(e) => { e.preventDefault(); submit(); }} aria-label="Message Rectifi">
      <label htmlFor="ask-input" className="sr-only">Message Rectifi</label>
      <textarea
        id="ask-input"
        ref={ref}
        className={a.input}
        rows={1}
        maxLength={MAX_TEXT}
        value={text}
        readOnly={busy}
        aria-busy={busy || undefined}
        placeholder="Spent, received, paste an SMS, or ask a question…"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); }
        }}
      />
      <Button type="submit" variant="primary" disabled={busy || text.trim() === ""} aria-label="Send" className={a.send}>
        <ArrowUp size={20} strokeWidth={2} aria-hidden="true" />
      </Button>
    </form>
  );
}

// ── the conversation ──

function Chat({ status }: { status: AskStatus }) {
  const client = useQueryClient();
  const [thread, setThread] = useState<string | null>(null);
  const [items, setItems] = useState<TranscriptItem[]>([]);
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [error, setError] = useState<{ message: string; text: string } | null>(null);
  const abort = useRef<AbortController | null>(null);
  const end = useRef<HTMLDivElement>(null);

  const start = useCallback(async (fresh: boolean) => {
    setLoadError(null);
    setReady(false);
    try {
      let id = fresh ? null : readThread();
      let history: TranscriptItem[] = [];
      if (id) {
        try {
          history = (await get<{ items: TranscriptItem[] }>(`/ask/threads/${id}`)).items;
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) id = null;
          else throw e;
        }
      }
      if (!id) id = (await post<{ thread_id: string }>("/ask/threads")).thread_id;
      writeThread(id);
      setThread(id);
      setItems(history);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Could not open the conversation.");
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    void start(false);
    return () => abort.current?.abort();
  }, [start]);

  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    end.current?.scrollIntoView({ block: "end", behavior: reduce ? "auto" : "smooth" });
  }, [items.length, pending?.steps.length, error]);

  async function send(text: string) {
    if (!thread || pending) return;
    setError(null);
    setItems((prev) => [...prev, { role: "user", text }]);
    setPending({ steps: [], startedAt: Date.now() });
    const ctl = new AbortController();
    abort.current = ctl;
    let failed: string | null = null;
    try {
      await streamAsk(`/ask/threads/${thread}/messages`, { text }, (ev) => {
        if (ev.type === "step") setPending((p) => (p ? { ...p, steps: [...p.steps, { tool: ev.tool, error: ev.error }] } : p));
        else if (ev.type === "done") setItems((prev) => [...(ev.items[0]?.role === "user" ? prev.slice(0, -1) : prev), ...ev.items]);
        else if (ev.type === "error") failed = ev.message;
      }, ctl.signal);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      failed = e instanceof Error ? e.message : "Something went wrong. Try again.";
    }
    setPending(null);
    if (failed) {
      setItems((prev) => (prev[prev.length - 1]?.role === "user" ? prev.slice(0, -1) : prev));
      setError({ message: failed, text });
    }
    refreshBooks(client);
  }

  async function fresh() {
    abort.current?.abort();
    setPending(null);
    setError(null);
    await start(true);
  }

  const busy = pending !== null;
  const empty = items.length === 0 && !busy;
  return (
    <>
      <PageHeader
        name="Ask"
        title="Ask Rectifi"
        sub={`Model: ${status.model} · amounts are checked against what you type before anything is saved`}
        controls={<Button variant="secondary" icon={<RotateCcw size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => void fresh()} disabled={busy}>New conversation</Button>}
      />
      <div className={a.chat}>
      {status.notice && <Callout tone="note" className={a.notice}>{status.notice}</Callout>}
      {!ready && <div className={styles.skelStack} aria-hidden="true"><Skeleton height={64} /><Skeleton height={64} /></div>}
      {ready && loadError && (
        <Callout tone="over" role="alert" lead="Could not open the conversation." actions={<Button variant="secondary" onClick={() => void start(false)}>Try again</Button>}>{loadError}</Callout>
      )}
      {ready && !loadError && (
        <>
          {empty && (
            <section aria-label="Try one of these">
              <Eyebrow>Try</Eyebrow>
              <div className={a.examples}>
                {EXAMPLES.map((ex) => <Button key={ex} variant="secondary" onClick={() => void send(ex)}>{ex}</Button>)}
              </div>
            </section>
          )}
          <div className={a.transcript} data-testid="ask-transcript">
            {items.map((item, i) => <Turn key={i} item={item} />)}
            {pending && <Working pending={pending} />}
            {error && (
              <div className={a.error} role="alert" data-testid="ask-error">
                <Callout tone="over" lead={error.message === "Still answering your last message." ? "Hold on." : "That did not go through."}>{error.message}</Callout>
                <Button variant="secondary" icon={<RotateCcw size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => void send(error.text)}>Try again</Button>
              </div>
            )}
            <div ref={end} className={a.end} />
          </div>
          <Composer busy={busy} onSend={(t) => void send(t)} />
        </>
      )}
      </div>
    </>
  );
}

export default function AskPage() {
  const status = useAskStatus();
  return (
    <Loaded query={status} skeleton={<div className={cx(styles.skelStack)} aria-hidden="true"><Skeleton height={40} width="50%" /><Skeleton height={80} /></div>}>
      {(s) =>
        s.ok ? (
          <Chat status={s} />
        ) : (
          <>
            <PageHeader name="Ask" title="Ask Rectifi" sub={`Model: ${s.model} · amounts are checked against what you type before anything is saved`} />
            <Callout tone="near" icon={Bot} lead="The assistant is not ready." role="status">{s.why}</Callout>
            <EmptyState title="The assistant is off — the rest of Rectifi works." actions={<LinkButton to="/add" variant="primary">Go to Add</LinkButton>}>
              You can still add entries, import statements and set budgets by hand.
            </EmptyState>
          </>
        )
      }
    </Loaded>
  );
}
