import { Pencil } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { streamChart, useChartActions, useChartStatus, useCharts, type ChartDetail, type DrawEvent, type TraceRow } from "../api/charts";
import { ChartGallery } from "../components/charts/ChartGallery";
import { ChartProgress } from "../components/charts/ChartProgress";
import { ChartSlip, HowItGotHere } from "../components/charts/ChartSlip";
import { EXAMPLES, PERIODS } from "../components/charts/describe";
import { ReviewGate } from "../components/charts/ReviewGate";
import { Button, Checkbox, Field, Select, TextArea } from "../components/forms";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { useToast } from "../components/Toast";
import { Callout, ErrorBlock, Section, Skeleton } from "../components/ui";
import styles from "../components/charts/charts.module.css";

const MAX_REQUEST = 500;
type Progress = Extract<DrawEvent, { type: "progress" }>;

type Phase =
  | { kind: "idle" }
  | { kind: "drawing"; events: Progress[]; startedAt: number }
  | { kind: "review"; thread: string; code: string; busy: boolean }
  | { kind: "done"; chart: ChartDetail; thread: string | null; trace: TraceRow[] }
  | { kind: "failed"; thread: string | null; message: string; trace: TraceRow[] };

export default function ChartsPage() {
  const status = useChartStatus();
  const gallery = useCharts();
  const actions = useChartActions();
  const toast = useToast();
  const [request, setRequest] = useState("");
  const [period, setPeriod] = useState("last-3-months");
  const [review, setReview] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [rerunId, setRerunId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const resultRef = useRef<HTMLDivElement>(null);

  useEffect(() => () => abort.current?.abort(), []);
  useEffect(() => {
    if (phase.kind === "done" || phase.kind === "review") resultRef.current?.scrollIntoView({ block: "nearest" });
  }, [phase.kind]);

  const modelOn = status.data?.ok ?? true;

  const run = useCallback(async (path: string, body: unknown) => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setError(null);
    const events: Progress[] = [];
    const startedAt = Date.now();
    setPhase({ kind: "drawing", events, startedAt });
    let thread: string | null = null;
    try {
      await streamChart(path, body, (e) => {
        if (e.type === "progress") {
          events.push(e);
          setPhase({ kind: "drawing", events: [...events], startedAt });
        } else if (e.type === "interrupt") {
          thread = e.thread_id;
          setPhase({ kind: "review", thread: e.thread_id, code: e.code, busy: false });
        } else if (e.type === "done") {
          thread = e.thread_id;
          setPhase({ kind: "done", chart: e.chart, thread: e.thread_id, trace: e.trace });
          void actions.refresh();
        } else if (e.type === "failed") {
          thread = e.thread_id;
          setPhase({ kind: "failed", thread: e.thread_id, message: e.message, trace: e.trace });
        } else if (e.type === "error") {
          setPhase({ kind: "failed", thread, message: e.message, trace: [] });
        }
      }, controller.signal);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setPhase({ kind: "idle" });
      setError(e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong. Try again.");
    }
  }, [actions]);

  const draw = () => {
    if (!request.trim()) { setError("Say what to draw first."); return; }
    void run("/charts/draw", { request: request.trim(), period, review });
  };

  const drawing = phase.kind === "drawing";
  const open = async (id: number) => {
    try {
      const chart = await actions.open(id);
      setPhase({ kind: "done", chart, thread: null, trace: chart.trace });
    } catch (e) {
      toast(e instanceof Error ? e.message : "Couldn't open that chart.", "over");
    }
  };
  const rerun = async (id: number) => {
    setRerunId(id);
    try {
      const chart = await actions.rerun.mutateAsync(id);
      setPhase({ kind: "done", chart, thread: null, trace: chart.trace });
      toast("Drawn again from the saved code, with your latest entries.");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Couldn't draw it again.", "over");
    } finally {
      setRerunId(null);
    }
  };

  return (
    <div className={styles.page}>
      <PageHeader name="Charts" title="Draw your spending" />

      {!modelOn && (
        <Callout tone="near" lead="The model is off.">
          {status.data?.why || "Drawing a new chart needs the model."} Saved charts below can still be drawn again, because that does not use the model.
        </Callout>
      )}

      <form className={styles.prompt} onSubmit={(e) => { e.preventDefault(); if (!drawing) draw(); }} aria-label="Draw a chart">
        <Field label="What should I draw?" hint={`${request.length}/${MAX_REQUEST}`} error={error}>
          {(p) => (
            <TextArea {...p} rows={3} maxLength={MAX_REQUEST} value={request} onChange={(e) => setRequest(e.target.value)} disabled={drawing}
              placeholder="For example: top 5 payees over the last 3 months" />
          )}
        </Field>
        <div className={styles.examples} role="group" aria-label="Examples">
          {EXAMPLES.map((x) => (
            <Button key={x.label} variant="tertiary" disabled={drawing} onClick={() => { setRequest(x.request); setPeriod(x.period); }}>{x.label}</Button>
          ))}
        </div>
        <div className={styles.controls}>
          <Field label="Period" className={styles.periodField}>
            {(p) => <Select {...p} value={period} onChange={(e) => setPeriod(e.target.value)} disabled={drawing}>{PERIODS.map((x) => <option key={x.value} value={x.value}>{x.label}</option>)}</Select>}
          </Field>
          <Checkbox label="Let me read the code before it runs" checked={review} onChange={setReview} disabled={drawing} />
        </div>
        <div className={styles.submitRow}>
          <Button type="submit" variant="primary" loading={drawing} loadingLabel="Drawing…" disabled={!modelOn} icon={<Pencil size={16} strokeWidth={1.5} aria-hidden="true" />}>Draw</Button>
        </div>
      </form>

      <div ref={resultRef}>
        {phase.kind === "drawing" && <ChartProgress events={phase.events} startedAt={phase.startedAt} />}
        {phase.kind === "review" && (
          <ReviewGate code={phase.code} busy={phase.busy}
            onRun={(code) => { setPhase({ ...phase, busy: true }); void run(`/charts/threads/${phase.thread}/resume`, { run: true, code }); }}
            onCancel={() => void run(`/charts/threads/${phase.thread}/resume`, { run: false })} />
        )}
        {phase.kind === "done" && (
          <ChartSlip chart={phase.chart} trace={phase.trace} busy={rerunId === phase.chart.id}
            onPin={(pinned) => actions.pin.mutate({ id: phase.chart.id, pinned }, { onSuccess: (s) => setPhase({ ...phase, chart: { ...phase.chart, pinned: s.pinned } }) })}
            onRerun={() => void rerun(phase.chart.id)}
            onDelete={() => actions.remove.mutate(phase.chart.id, { onSuccess: () => { setPhase({ kind: "idle" }); toast("Chart deleted."); } })}
            onRetry={phase.thread ? (attempt) => void run(`/charts/threads/${phase.thread}/retry`, { attempt, feedback: "" }) : undefined} />
        )}
        {phase.kind === "failed" && (
          <>
            <ErrorBlock title="I couldn't draw that" message={phase.message} onRetry={draw} />
            <HowItGotHere trace={phase.trace} attempts={Math.max(1, ...phase.trace.map((r) => r.attempt))}
              onRetry={phase.thread ? (attempt) => void run(`/charts/threads/${phase.thread}/retry`, { attempt, feedback: "" }) : undefined} />
          </>
        )}
      </div>

      <Section eyebrow="Saved charts">
        <Loaded query={gallery} skeleton={<Skeleton height={96} />}>
          {(items) => <ChartGallery items={items} busyId={rerunId} onOpen={(id) => void open(id)} onRerun={(id) => void rerun(id)} />}
        </Loaded>
      </Section>
    </div>
  );
}
