import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, NetworkError, REQUEST_HEADER, del, get, post } from "./client";

// ── shapes (what rectifi/api/routes/charts.py sends; there is no money in here, every ₹ figure is inside the picture) ──

export interface ChartStatus { ok: boolean; why: string; model: string; plotly: boolean }

export interface ChartSummary {
  id: number; title: string; prompt: string; kind: string; pinned: boolean;
  period_start: string | null; period_end: string | null; model: string | null; attempts: number;
  created_at: string; refreshed_at: string | null; ok: boolean; thumb: string | null;
}

export interface TraceRow { attempt: number; step: string; verdict: string; detail: string; traceback?: string; chart_id?: number }
export interface ChartCheck { name: string; ok: boolean; detail: string }

export interface ChartDetail extends ChartSummary {
  code: string; trace: TraceRow[]; checks: ChartCheck[]; entries: number; alt: string;
  image_light: string | null; image_dark: string | null;
}

export type DrawEvent =
  | { type: "progress"; step: string; verdict: string; detail: string; attempt: number }
  | { type: "tick" }
  | { type: "interrupt"; thread_id: string; code: string; attempt: number }
  | { type: "done"; thread_id: string; chart: ChartDetail; trace: TraceRow[] }
  | { type: "failed"; thread_id: string; message: string; trace: TraceRow[] }
  | { type: "error"; message: string };

export interface DrawBody { request: string; period: string; review: boolean }

// ── queries ──

export const chartKeys = { status: ["charts", "status"] as const, list: ["charts", "list"] as const };

export const useChartStatus = () =>
  useQuery({ queryKey: chartKeys.status, queryFn: ({ signal }) => get<ChartStatus>("/charts/status", signal), staleTime: 30_000 });

export const useCharts = () =>
  useQuery({ queryKey: chartKeys.list, queryFn: ({ signal }) => get<{ items: ChartSummary[] }>("/charts", signal).then((r) => r.items) });

export function useChartActions() {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: chartKeys.list });
  return {
    pin: useMutation({ mutationFn: (v: { id: number; pinned: boolean }) => post<ChartSummary>(`/charts/${v.id}/pin`, { pinned: v.pinned }), onSuccess: refresh }),
    rerun: useMutation({ mutationFn: (id: number) => post<ChartDetail>(`/charts/${id}/rerun`), onSuccess: refresh }),
    remove: useMutation({ mutationFn: (id: number) => del<{ deleted: boolean }>(`/charts/${id}`), onSuccess: refresh }),
    open: (id: number) => get<ChartDetail>(`/charts/${id}`),
    refresh,
  };
}

// ── streaming: one JSON object per line ──

export async function streamChart(path: string, body: unknown, onEvent: (event: DrawEvent) => void, signal?: AbortSignal): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method: "POST",
      headers: { [REQUEST_HEADER]: "1", "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify(body),
      signal,
      cache: "no-store",
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new NetworkError();
  }
  if (!res.ok) {
    let message = "Something went wrong. Try again.";
    let code = "error";
    try {
      const data = (await res.json()) as { error?: { code?: string; message?: string } };
      if (data.error?.message) message = data.error.message;
      if (data.error?.code) code = data.error.code;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, code, message);
  }
  if (!res.body) throw new ApiError(500, "error", "This browser can't read a streamed answer.");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const flush = (line: string) => {
    const text = line.trim();
    if (!text) return;
    try {
      onEvent(JSON.parse(text) as DrawEvent);
    } catch {
      /* a half line is ignored; the next chunk completes the next one */
    }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let at = buffer.indexOf("\n");
    while (at >= 0) {
      flush(buffer.slice(0, at));
      buffer = buffer.slice(at + 1);
      at = buffer.indexOf("\n");
    }
  }
  flush(buffer + decoder.decode());
}
