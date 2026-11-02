import { useQuery } from "@tanstack/react-query";
import { isOwnerView, useAuthStatus } from "./auth";
import { del, get, patch, post, qs } from "./client";
import { streamNdjson } from "./ndjson";
import type { Citation, TraceEvent } from "./knowledge";
import type { AskStatus, Money } from "./types";

export type RouteName = "agent" | "knowledge" | "chart" | "smalltalk" | "remember";
export type AnswerStyle = "auto" | "rag" | "crag" | "self_rag" | "full";
export type Chip = "" | "agent" | "knowledge" | "chart";

export interface Thread {
  thread_id: string;
  title: string;
  preview: string | null;
  created_at: string;
  updated_at: string;
}

export type { Citation, TraceEvent } from "./knowledge";
export interface ToolStep {
  tool: string;
  output: string;
  error: boolean;
}

export type ChatItem =
  | { id: string | null; role: "user"; text: string; ts: string | null }
  | {
      id: string | null;
      role: "assistant";
      text: string;
      ts: string | null;
      route: RouteName | null;
      unverified: Money[];
      steps: ToolStep[];
      citations: Citation[];
      trace: TraceEvent[];
      checks: TraceEvent[];
      artifact: { chart_id: number } | null;
      support: string | null;
      useful: boolean | null;
      abstained: boolean;
      stopped: boolean;
    };

export interface Alternative {
  id: string;
  text: string;
  ts: string | null;
  route: RouteName | null;
}
export interface Interrupt {
  id: string | null;
  kind: string;
  payload: Record<string, unknown>;
}
export interface ThreadView {
  thread: Thread;
  items: ChatItem[];
  pending_interrupt: Interrupt | null;
  alternatives: Alternative[];
}

export type ChatEvent =
  | { type: "token"; id: string | null; text: string }
  | { type: "node"; node: string }
  | { type: "step"; tool: string; error: boolean }
  | { type: "trace"; event: TraceEvent }
  | { type: "interrupt"; id: string | null; kind: string; payload: Record<string, unknown> }
  | { type: "tick" }
  | { type: "done"; stopped: boolean; items: ChatItem[]; pending_interrupt: Interrupt | null; alternatives: Alternative[]; thread: Pick<Thread, "thread_id" | "title" | "preview" | "updated_at"> }
  | { type: "error"; message: string };

export const chatKeys = {
  status: ["chat", "status"] as const,
  threads: (q: string) => ["chat", "threads", q] as const,
  allThreads: ["chat", "threads"] as const,
};

/** The Ask model's state. A friend never asks the server (it would answer 403): the model is the owner's only. */
export const useChatStatus = () => {
  const auth = useAuthStatus(); // wait for the answer: asking before it is known would send a friend's browser to a 403
  return useQuery({
    queryKey: chatKeys.status, queryFn: ({ signal }) => get<AskStatus>("/chat/status", signal), staleTime: 30_000,
    enabled: auth.data !== undefined && isOwnerView(auth.data),
  });
};

export const useThreads = (q: string) =>
  useQuery<Thread[]>({
    queryKey: chatKeys.threads(q),
    queryFn: ({ signal }) => get<{ threads: Thread[] }>(`/chat/threads${qs({ q })}`, signal).then((r) => r.threads),
    placeholderData: (previous) => previous,
  });

export const createThread = (title?: string) => post<Thread>("/chat/threads", title ? { title } : {});
export const getThread = (id: string, signal?: AbortSignal) => get<ThreadView>(`/chat/threads/${id}`, signal);
export const renameThread = (id: string, title: string) => patch<Thread>(`/chat/threads/${id}`, { title });
export const deleteThread = (id: string) => del<{ deleted: boolean }>(`/chat/threads/${id}`);
export const stopThread = (id: string) => post<{ stopping: boolean }>(`/chat/threads/${id}/stop`, {});

export interface SendOptions {
  route?: Chip;
  mode?: AnswerStyle;
  review?: boolean;
}
export const streamMessage = (id: string, text: string, options: SendOptions, on: (e: ChatEvent) => void, signal?: AbortSignal) =>
  streamNdjson<ChatEvent>(`/chat/threads/${id}/messages`, { text, ...options }, on, signal);
export const streamRegenerate = (id: string, on: (e: ChatEvent) => void, signal?: AbortSignal) =>
  streamNdjson<ChatEvent>(`/chat/threads/${id}/regenerate`, {}, on, signal);
export const streamResume = (id: string, value: unknown, on: (e: ChatEvent) => void, signal?: AbortSignal) =>
  streamNdjson<ChatEvent>(`/chat/threads/${id}/resume`, { value }, on, signal);
