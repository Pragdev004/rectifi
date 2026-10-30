import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { clearTraces, traceKeys, useTraceRuns, useTraceTree, type TraceRun, type TraceSpan } from "../api/traces";
import { ConfirmDialog, Dialog } from "../components/Dialog";
import { Button, Checkbox, SegmentedControl } from "../components/forms";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { useToast } from "../components/Toast";
import { Callout, Disclosure, EmptyState, Eyebrow, Skeleton, Stamp, type Tone } from "../components/ui";
import { fmtDateTime } from "../lib/dates";
import { useIsDesktop } from "../lib/media";
import t from "./traces.module.css";

/** 820 ms, 1.4 s, 2 min 5 s. */
export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "…";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
}

const STATUS: Record<string, { tone: Tone; word: string }> = {
  ok: { tone: "ok", word: "OK" },
  error: { tone: "over", word: "Failed" },
  running: { tone: "near", word: "Running" },
};
const ROUTE_WORD: Record<string, string> = { agent: "Did it", knowledge: "Looked it up", chart: "Chart", chat: "Chat", memory: "Memory" };

function SpanRow({ span }: { span: TraceSpan }) {
  const s = STATUS[span.status] ?? STATUS.ok!;
  const tokens = (span.input_tokens ?? 0) + (span.output_tokens ?? 0);
  return (
    <li className={t.span} style={{ ["--depth" as string]: Math.min(span.depth, 8) }} data-testid="trace-span">
      <div className={t.spanHead}>
        <span className={t.spanName}>{span.name}</span>
        <Stamp>{span.run_type}</Stamp>
        {span.status !== "ok" && <Stamp tone={s.tone}>{s.word}</Stamp>}
        <span className={t.ms}>{fmtMs(span.ms)}</span>
        {span.model && <span className={t.model}>{span.model}</span>}
        {tokens > 0 && <span className={t.ms}>{tokens} tokens</span>}
      </div>
      {span.error && <p className={t.err}>{span.error}</p>}
      {(span.input_preview || span.output_preview) && (
        <Disclosure summary="What went in and out (masked)">
          {span.input_preview && <><p className={t.label}>In</p><pre className={t.preview}>{span.input_preview}</pre></>}
          {span.output_preview && <><p className={t.label}>Out</p><pre className={t.preview}>{span.output_preview}</pre></>}
        </Disclosure>
      )}
    </li>
  );
}

// LangChain's own wiring (sequence, prompt template, "run") says nothing a person can use; the real steps are the named nodes, model calls, tools and retrievers.
const PLUMBING = new Set(["ChatPromptTemplate", "PromptTemplate", "run", "LangGraph", "StrOutputParser", "PydanticOutputParser", "JsonOutputParser", "ChannelWrite", "ChannelRead"]);
export function isPlumbing(span: TraceSpan): boolean {
  return span.run_type === "chain" && span.status === "ok" && (span.name.startsWith("Runnable") || PLUMBING.has(span.name));
}

function Tree({ id }: { id: string }) {
  const tree = useTraceTree(id);
  const [internal, setInternal] = useState(false);
  return (
    <Loaded query={tree} skeleton={<div aria-hidden="true"><Skeleton height={40} /><Skeleton height={40} style={{ marginTop: 8 }} /></div>}>
      {(data) => (
        <div className={t.panel}>
          {(() => {
            const hidden = data.spans.filter(isPlumbing).length;
            const shown = internal ? data.spans : data.spans.filter((s) => !isPlumbing(s));
            return (
              <>
                <Eyebrow>Steps · {shown.length}</Eyebrow>
                {hidden > 0 && <Checkbox label={`Show LangChain's internal wiring (${hidden} more)`} checked={internal} onChange={setInternal} />}
                <ul className={t.tree} aria-label="Steps of this run">{shown.map((s) => <SpanRow key={s.run_id} span={s} />)}</ul>
              </>
            );
          })()}
        </div>
      )}
    </Loaded>
  );
}

function RunRow({ run, selected, onPick }: { run: TraceRun; selected: boolean; onPick: () => void }) {
  const s = STATUS[run.status] ?? STATUS.ok!;
  return (
    <li>
      <button type="button" className={t.runBtn} aria-current={selected ? "true" : undefined} onClick={onPick}>
        <span className={t.time}>{fmtDateTime(run.started_at)}</span>
        <span className={t.what}>
          <strong>{run.entry}{run.route ? ` · ${ROUTE_WORD[run.route] ?? run.route}` : ""}</strong>
          {run.preview && <span>{run.preview.replace(/\s+/g, " ").slice(0, 90)}</span>}
        </span>
        <span style={{ display: "grid", gap: 4, justifyItems: "end" }}>
          <Stamp tone={s.tone}>{s.word}</Stamp>
          <span className={t.ms}>{fmtMs(run.latency_ms)}</span>
        </span>
      </button>
    </li>
  );
}

export default function TracesPage() {
  const client = useQueryClient();
  const toast = useToast();
  const desktop = useIsDesktop();
  const [status, setStatus] = useState<"" | "error">("");
  const [picked, setPicked] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [clearing, setClearing] = useState(false);
  const runs = useTraceRuns(status);

  async function clear() {
    setClearing(true);
    try { await clearTraces(); setPicked(null); await client.invalidateQueries({ queryKey: traceKeys.all }); setConfirm(false); toast("Traces cleared.", "note"); }
    catch (e) { toast(e instanceof Error ? e.message : "Could not clear them.", "over"); }
    finally { setClearing(false); }
  }

  return (
    <>
      <PageHeader
        name="Traces"
        title="What the assistant did"
        sub="Every model call, tool and step, kept on this laptop for 14 days. Account numbers are masked."
        controls={<Button variant="secondary" icon={<RefreshCw size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => void client.invalidateQueries({ queryKey: traceKeys.all })}>Refresh</Button>}
      />
      <Loaded query={runs} skeleton={<div aria-hidden="true"><Skeleton height={56} /><Skeleton height={56} style={{ marginTop: 8 }} /></div>}>
        {(data) => (
          <>
            <Callout tone={data.langsmith.sending ? "near" : "note"}>{data.langsmith.note}</Callout>
            <div className={t.bar} style={{ marginTop: 16 }}>
              <SegmentedControl
                legend="Show" name="trace-status" value={status} onChange={setStatus}
                options={[{ value: "", label: "All runs" }, { value: "error", label: "Failed" }]}
              />
              {data.runs.length > 0 && <Button variant="dangerQuiet" onClick={() => setConfirm(true)}>Clear traces</Button>}
            </div>
            {data.runs.length === 0 ? (
              <EmptyState title={status ? "No failed runs." : "No runs yet."}>Ask Rectifi something and its steps show up here.</EmptyState>
            ) : (
              <div className={t.layout}>
                <ul className={t.list} aria-label="Recent runs">
                  {data.runs.map((r) => <RunRow key={r.root_run_id} run={r} selected={picked === r.root_run_id} onPick={() => setPicked(r.root_run_id)} />)}
                </ul>
                {desktop && (picked ? <Tree id={picked} /> : <p className={t.muted}>Pick a run to see its steps.</p>)}
              </div>
            )}
          </>
        )}
      </Loaded>
      {!desktop && <Dialog open={picked !== null} onClose={() => setPicked(null)} title="Steps of this run" sheet>{picked && <Tree id={picked} />}</Dialog>}
      <ConfirmDialog open={confirm} title="Clear all traces?" confirmLabel="Clear traces" busyLabel="Clearing…" busy={clearing} onConfirm={() => void clear()} onClose={() => setConfirm(false)}>
        <p>This only removes the debugging record of past runs. Your conversations and entries stay.</p>
      </ConfirmDialog>
    </>
  );
}
