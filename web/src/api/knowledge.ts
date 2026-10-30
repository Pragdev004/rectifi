import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post, del, request } from "./client";

// What the knowledge routes send. Money never appears here: a ₹ figure inside an answer is already text.

export type Section = "guide" | "note" | "txn" | "upload" | "web" | "tool";

export interface KnowledgeDoc {
  id: number | null;
  corpus: "guide" | "upload";
  label: string;
  title: string;
  source: string;
  chunks: number;
  created_at: string | null;
  builtin: boolean;
  indexed: boolean;
}

export interface KnowledgeJob {
  state: "idle" | "running" | "done" | "failed";
  done: number;
  total: number;
  error: string | null;
  started_at: number | null;
  finished_at: number | null;
}

export interface KnowledgeStatus {
  embed_model: string;
  chunks: Record<string, number>;
  chunks_total: number;
  documents: Record<string, number>;
  transactions: number;
  other_model_chunks: number;
  updated_at: string | null;
  guides_on_disk: number;
  job: KnowledgeJob;
}

export interface Citation {
  n: number | null;
  source: string;
  title: string;
  chunk: number;
  quote: string;
  corpus: Section;
  label: string;
}

export interface TraceEvent {
  step: string;
  verdict: string;
  detail: string;
}

/** One knowledge answer as the chat window draws it (RagOutput through api/wire.rag_answer). */
export interface RagAnswer {
  answer: string;
  citations: Citation[];
  trace: TraceEvent[];
  unverified: string[];
  support: "full" | "partial" | "none" | null;
  useful: boolean | null;
  route: string | null;
  mode: string | null;
  abstained: boolean;
}

/** The graph asks before it searches the web (a LangGraph interrupt). */
export interface WebConsentRequest {
  type: "web_search";
  query: string;
  message: string;
  choices: string[];
}

export interface UploadResult {
  doc: KnowledgeDoc;
  indexed: boolean;
  note: string | null;
}

export const knowledgeKeys = {
  docs: ["knowledge", "docs"] as const,
  status: ["knowledge", "status"] as const,
};

export const useKnowledgeDocs = () =>
  useQuery({ queryKey: knowledgeKeys.docs, queryFn: ({ signal }) => get<{ docs: KnowledgeDoc[] }>("/knowledge/docs", signal) });

/** Polls every second while a re-index runs, so the progress is live. */
export const useKnowledgeStatus = () =>
  useQuery({
    queryKey: knowledgeKeys.status,
    queryFn: ({ signal }) => get<KnowledgeStatus>("/knowledge/status", signal),
    refetchInterval: (q) => (q.state.data?.job.state === "running" ? 1000 : false),
  });

export function useKnowledgeActions() {
  const client = useQueryClient();
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["knowledge"] });
  };
  const reindex = useMutation({ mutationFn: () => post<{ started: boolean; job: KnowledgeJob }>("/knowledge/reindex"), onSuccess: refresh });
  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return request<UploadResult>("POST", "/knowledge/docs", { form });
    },
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: (id: number) => del<{ deleted: number }>(`/knowledge/docs/${id}`), onSuccess: refresh });
  return { reindex, upload, remove };
}

const SHORT_DAY = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short" });

/** "Indexed with nomic-embed-text · 214 passages · updated 25 Oct" */
export function statusLine(s: KnowledgeStatus): string {
  if (s.chunks_total === 0) return `Nothing is indexed yet with ${s.embed_model}.`;
  const when = s.updated_at ? ` · updated ${SHORT_DAY.format(new Date(s.updated_at)).replace("Sept", "Sep")}` : "";
  return `Indexed with ${s.embed_model} · ${s.chunks_total} ${s.chunks_total === 1 ? "passage" : "passages"}${when}`;
}

