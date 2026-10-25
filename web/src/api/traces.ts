import { useQuery } from "@tanstack/react-query";
import { del, get, qs } from "./client";

export interface TraceRun {
  root_run_id: string;
  entry: string;
  route: string | null;
  thread_id: string | null;
  started_at: string;
  latency_ms: number | null;
  status: "ok" | "error" | "running";
  span_count: number;
  model: string | null;
  tokens: number;
  preview: string | null;
}
export interface TraceSpan {
  run_id: string;
  parent_run_id: string | null;
  depth: number;
  name: string;
  run_type: string;
  model: string | null;
  status: string;
  started_at: string;
  ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  input_preview: string | null;
  output_preview: string | null;
  error: string | null;
}
export interface TraceList {
  runs: TraceRun[];
  langsmith: { opted_in: boolean; sending: boolean; note: string };
}
export interface TraceTree {
  run: Partial<TraceRun> & { root_run_id: string };
  spans: TraceSpan[];
}

export const traceKeys = { all: ["traces"] as const, list: (status: string) => ["traces", "list", status] as const, one: (id: string) => ["traces", "one", id] as const };

export const useTraceRuns = (status: string) =>
  useQuery({
    queryKey: traceKeys.list(status),
    queryFn: ({ signal }) => get<TraceList>(`/traces${qs({ limit: 60, status })}`, signal),
    refetchInterval: 15_000,
  });
export const useTraceTree = (id: string | null) =>
  useQuery({ queryKey: traceKeys.one(id ?? ""), queryFn: ({ signal }) => get<TraceTree>(`/traces/${id}`, signal), enabled: id !== null });
export const clearTraces = () => del<{ deleted: number }>("/traces");
