import { useQuery } from "@tanstack/react-query";
import { get, post, qs } from "./client";
import type { Txn } from "./types";

/** How a category was chosen. Rows from before this feature have no source (null). */
export type CategorySource = "user" | "learned" | "rule" | "heuristic" | "retrieval" | "llm" | "fallback";

/** The fields the server adds to every transaction. Percent fields are whole numbers (82), never money. */
export interface ClassifyFields {
  category_source: CategorySource | null;
  category_confidence: number | null;
  category_reason: string | null;
  suggested_category: string | null;
  suggested_confidence: number | null;
}
export type SortedTxn = Txn & ClassifyFields;

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled" | "no_model";
export interface ClassifyJob {
  id: number;
  batch_id: number | null;
  status: JobStatus;
  total: number;
  done: number;
  by_source: Record<string, number>;
  model: string | null;
  error: string | null;
  finished: boolean;
}
export interface BatchSummary {
  total: number;
  by_source: Record<string, number>;
  miscellaneous: number;
}
export type Show = "all" | "misc" | "model" | "suggested";
export interface BatchRows {
  items: SortedTxn[];
  show: Show;
  summary: BatchSummary;
  job: ClassifyJob | null;
}

const ACTIVE: JobStatus[] = ["queued", "running"];
export const jobIsActive = (job: ClassifyJob | null | undefined) => !!job && ACTIVE.includes(job.status);

export const classifyKeys = {
  rows: (batch: number, show: Show) => ["transactions", "import-rows", batch, show] as const,
};

/** The rows one import added. While the model is still sorting, ask again every 2 seconds. */
export const useBatchRows = (batch: number, show: Show) =>
  useQuery({
    queryKey: classifyKeys.rows(batch, show),
    queryFn: ({ signal }) => get<BatchRows>(`/imports/${batch}/rows${qs({ show })}`, signal),
    refetchInterval: (q) => (jobIsActive(q.state.data?.job) ? 2000 : false),
  });

export const startSorting = (batchId: number | null) =>
  post<{ job: ClassifyJob | null; waiting: number }>("/classify/jobs", batchId === null ? {} : { batch_id: batchId });

export const applyToSimilar = (txnId: number) => post<{ changed: number }>(`/transactions/${txnId}/apply-similar`);
