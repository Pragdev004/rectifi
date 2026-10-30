import { useQuery } from "@tanstack/react-query";
import { get, post, qs } from "./client";
import type { Txn } from "./types";

export type { CategorySource } from "./types";

/** The fields the server adds to every transaction (they are optional on `Txn`). Percent fields are whole numbers (82), never money. */
export type ClassifyFields = Required<Pick<Txn, "category_source" | "category_confidence" | "category_reason" | "suggested_category" | "suggested_confidence">>;
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

/** What POST /api/imports answers beyond the basic outcome: the sorting job (if rows were left) and the source counts. */
export interface ClassifyOutcome {
  classify_job_id: number | null;
  categories: BatchSummary;
}
