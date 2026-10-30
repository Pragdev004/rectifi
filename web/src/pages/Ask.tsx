import { useQueryClient } from "@tanstack/react-query";
import { Bot, MessageSquare, Plus } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import {
  chatKeys, createThread, deleteThread, getThread, renameThread, stopThread, streamMessage, streamRegenerate, streamResume, useChatStatus, useThreads,
  type Alternative, type ChatEvent, type ChatItem, type Interrupt, type SendOptions,
} from "../api/chat";
import { refreshBooks } from "../api/queries";
import type { AskStatus } from "../api/types";
import { Composer } from "../components/chat/Composer";
import { InterruptCard } from "../components/chat/InterruptCard";
import { AssistantTurn, LiveTurn, UserTurn } from "../components/chat/Message";
import { ThreadList } from "../components/chat/ThreadList";
import { applyEvent, freshLive, lastAssistantIndex, shownAlternative, type Live } from "../components/chat/helpers";
import { Dialog } from "../components/Dialog";
import { Button, LinkButton } from "../components/forms";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { useToast } from "../components/Toast";
import { Callout, EmptyState, Eyebrow, Skeleton } from "../components/ui";
import { cx } from "../lib/cx";
import { useIsDesktop } from "../lib/media";
import c from "../components/chat/chat.module.css";
import styles from "./pages.module.css";

const STORAGE_KEY = "rectifi.chat.thread";
const EXAMPLES = [
  "spent 250 on swiggy",
  "got 8000 stipend today",
  "how much did I spend on food this month?",
  "what is an emergency fund?",
  "draw my spending by category",
];

// Only the id of the open conversation is kept in the browser. The conversation itself is in the database.
function readThread(): string | null {
  try { return window.localStorage.getItem(STORAGE_KEY); } catch { return null; }
}
function writeThread(id: string | null) {
  try { if (id) window.localStorage.setItem(STORAGE_KEY, id); else window.localStorage.removeItem(STORAGE_KEY); } catch { /* storage blocked: the chat just starts fresh next time */ }
}

type Starter = (on: (e: ChatEvent) => void, signal: AbortSignal) => Promise<void>;

// ── one conversation ──

function Conversation({ threadId, onCreated, status }: { threadId: string | null; onCreated: (id: string) => void; status: AskStatus }) {
  const client = useQueryClient();
  const [items, setItems] = useState<ChatItem[]>([]);
  const [alts, setAlts] = useState<Alternative[]>([]);
  const [offset, setOffset] = useState(0);
  const [interrupt, setInterrupt] = useState<Interrupt | null>(null);
  const [live, setLive] = useState<Live | null>(null);
  const [error, setError] = useState<{ message: string; retry: () => void } | null>(null);
  const [ready, setReady] = useState(threadId === null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const idRef = useRef<string | null>(threadId);
  const end = useRef<HTMLDivElement>(null);

  const load = useCallback(async (id: string) => {
    setLoadError(null);
    setReady(false);
    try {
      const view = await getThread(id);
      setItems(view.items);
      setAlts(view.alternatives);
      setInterrupt(view.pending_interrupt);
      setOffset(0);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) writeThread(null);
      setLoadError(e instanceof Error ? e.message : "Could not open the conversation.");
    } finally {
      setReady(true);
    }
  }, []);

  // The page remounts this component (a new key) whenever the person opens another conversation, so the id is read once.
  // A conversation that is created by the first message must not reload itself in the middle of the answer.
  useEffect(() => {
    if (threadId) void load(threadId);
    return () => abort.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    end.current?.scrollIntoView({ block: "end", behavior: reduce ? "auto" : "smooth" });
  }, [items.length, live?.steps.length, live?.text.length === 0, interrupt, error]);

  async function run(start: Starter, restore: ChatItem[]) {
    setError(null);
    setInterrupt(null);
    setOffset(0);
    setLive(freshLive());
    const ctl = new AbortController();
    abort.current = ctl;
    let failed: string | null = null;
    let finished = false;
    try {
      await start((ev) => {
        if (ev.type === "done") {
          finished = true;
          setLive(null);                    // same render as the final items: the streamed text never shows twice
          setItems(ev.items);
          setAlts(ev.alternatives);
          setInterrupt(ev.pending_interrupt);
        } else if (ev.type === "error") failed = ev.message;
        else setLive((l) => (l ? applyEvent(l, ev) : l));
      }, ctl.signal);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      failed = e instanceof Error ? e.message : "Something went wrong. Try again.";
    }
    setLive(null);
    if (failed && !finished) {
      setItems(restore);
      setError({ message: failed, retry: () => void 0 });
    }
    void client.invalidateQueries({ queryKey: chatKeys.allThreads });
    refreshBooks(client);
  }

  async function send(text: string, options: SendOptions) {
    if (live) return;
    let id = idRef.current;
    const before = items;
    setItems((prev) => [...prev, { id: null, role: "user", text, ts: new Date().toISOString() }]);
    if (!id) {
      try {
        id = (await createThread()).thread_id;
      } catch (e) {
        setItems(before);
        setError({ message: e instanceof Error ? e.message : "Could not start the conversation.", retry: () => void send(text, options) });
        return;
      }
      idRef.current = id;
      writeThread(id);
      onCreated(id);
    }
    const target = id;
    await run((on, signal) => streamMessage(target, text, options, on, signal), before);
    setError((e) => (e ? { ...e, retry: () => void send(text, options) } : e));
  }

  async function answerAgain() {
    const id = idRef.current;
    if (!id || live) return;
    const before = items;
    const at = lastAssistantIndex(items);
    setItems(items.slice(0, at));
    await run((on, signal) => streamRegenerate(id, on, signal), before);
    setError((e) => (e ? { ...e, retry: () => void answerAgain() } : e));
  }

  async function answerQuestion(value: unknown) {
    const id = idRef.current;
    if (!id || live) return;
    const pending = interrupt;
    await run((on, signal) => streamResume(id, value, on, signal), items);
    setError((e) => (e ? { ...e, retry: () => { setInterrupt(pending); } } : e));
  }

  async function stop() {
    const id = idRef.current;
    if (!id) return;
    setLive((l) => (l ? { ...l, stopping: true } : l));
    try { await stopThread(id); } catch { /* the turn may have just finished */ }
  }

  const busy = live !== null;
  const empty = items.length === 0 && !busy;
  const lastAt = lastAssistantIndex(items);
  const picked = shownAlternative(alts, offset);

  if (!ready) return <div className={styles.skelStack} aria-hidden="true"><Skeleton height={64} /><Skeleton height={64} /></div>;
  if (loadError) {
    return <Callout tone="over" role="alert" lead="Could not open the conversation." actions={threadId ? <Button variant="secondary" onClick={() => void load(threadId)}>Try again</Button> : undefined}>{loadError}</Callout>;
  }
  return (
    <div className={c.convCol}>
      {status.notice && <Callout tone="note">{status.notice}</Callout>}
      {empty && (
        <section aria-label="Try one of these">
          <Eyebrow>Try</Eyebrow>
          <div className={c.examples}>
            {EXAMPLES.map((ex) => <Button key={ex} variant="secondary" onClick={() => void send(ex, {})}>{ex}</Button>)}
          </div>
        </section>
      )}
      <div className={c.transcript} data-testid="ask-transcript">
        {items.map((item, i) =>
          item.role === "user" ? (
            <UserTurn key={item.id ?? `u${i}`} item={item} />
          ) : (
            <AssistantTurn
              key={item.id ?? `a${i}`} item={item} last={i === lastAt && !busy && interrupt === null} busy={busy}
              alternatives={i === lastAt ? alts.length : 0}
              earlier={i === lastAt && picked.alt && offset > 0 ? { text: picked.alt.text, ts: picked.alt.ts, index: picked.index, total: picked.total } : null}
              onAnswerAgain={() => void answerAgain()}
              onAlternative={(d) => setOffset((o) => Math.min(Math.max(o + d, 0), Math.max(alts.length - 1, 0)))}
            />
          ),
        )}
        {live && <LiveTurn live={live} />}
        {interrupt && <InterruptCard interrupt={interrupt} busy={busy} onAnswer={(v) => void answerQuestion(v)} />}
        {error && (
          <div className={c.error} role="alert" data-testid="ask-error">
            <Callout tone="over" lead={error.message === "Still answering your last message." ? "Hold on." : "That did not go through."}>{error.message}</Callout>
            <Button variant="secondary" onClick={error.retry}>Try again</Button>
          </div>
        )}
        <div ref={end} className={c.end} />
      </div>
      <Composer busy={busy} stopping={live?.stopping ?? false} onSend={(t, o) => void send(t, o)} onStop={() => void stop()} />
    </div>
  );
}

// ── the page ──

function ChatPage({ status }: { status: AskStatus }) {
  const client = useQueryClient();
  const toast = useToast();
  const desktop = useIsDesktop();
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [active, setActive] = useState<string | null>(() => readThread());
  const [sheet, setSheet] = useState(false);
  const [nav, setNav] = useState(0);                  // changes when the person opens a different conversation (remounts the window)
  const threads = useThreads(search);

  useEffect(() => {
    const t = window.setTimeout(() => setSearch(query.trim()), 250);
    return () => window.clearTimeout(t);
  }, [query]);

  const open = useCallback((id: string | null) => { setActive(id); writeThread(id); setNav((n) => n + 1); }, []);

  async function rename(id: string, title: string) {
    try {
      await renameThread(id, title);
      await client.invalidateQueries({ queryKey: chatKeys.allThreads });
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not rename it.", "over");
      throw e;
    }
  }
  async function remove(id: string) {
    try {
      await deleteThread(id);
      if (id === active) open(null);
      await client.invalidateQueries({ queryKey: chatKeys.allThreads });
      toast("Conversation deleted.", "note");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not delete it.", "over");
      throw e;
    }
  }

  const list = (
    <ThreadList
      threads={threads.data} loading={threads.isPending} activeId={active} query={query} onQuery={setQuery}
      onOpen={open} onNew={() => open(null)} onRename={rename} onDelete={remove} onChosen={() => setSheet(false)}
    />
  );

  return (
    <>
      <PageHeader
        name="Ask"
        title="Ask Rectifi"
        sub={`Model: ${status.model} · amounts are checked against what you type before anything is saved`}
        controls={desktop ? undefined : (
          <>
            <Button variant="secondary" icon={<MessageSquare size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => setSheet(true)}>Conversations</Button>
            <Button variant="secondary" aria-label="New conversation" icon={<Plus size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => open(null)} />
          </>
        )}
      />
      <div className={c.shell}>
        {desktop && <aside className={c.listCol}>{list}</aside>}
        <Conversation key={nav} threadId={active} status={status} onCreated={(id) => setActive(id)} />
      </div>
      {!desktop && (
        <Dialog open={sheet} onClose={() => setSheet(false)} title="Conversations" sheet>{list}</Dialog>
      )}
    </>
  );
}

export default function AskPage() {
  const status = useChatStatus();
  return (
    <Loaded query={status} skeleton={<div className={cx(styles.skelStack)} aria-hidden="true"><Skeleton height={40} width="50%" /><Skeleton height={80} /></div>}>
      {(s) =>
        s.ok ? (
          <ChatPage status={s} />
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
