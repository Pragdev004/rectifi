import { useQuery } from "@tanstack/react-query";
import { del, get, patch, post } from "./client";

export type MemoryKind = "preference" | "fact" | "correction";
export interface MemoryItem {
  id: string;
  kind: MemoryKind;
  text: string;
  created_at: string | null;
  updated_at: string | null;
  thread_id: string | null;
}
export interface TaughtRule {
  id: number;
  pattern: string;
  category: string;
  created_at: string;
}
export interface MemoryPage {
  memories: MemoryItem[];
  rules: TaughtRule[];
}

export const memoryKey = ["memory"] as const;
export const useMemory = () => useQuery({ queryKey: memoryKey, queryFn: ({ signal }) => get<MemoryPage>("/memory", signal) });

export const addMemory = (kind: MemoryKind, text: string) => post<MemoryItem>("/memory", { kind, text });
export const editMemory = (id: string, changes: { text?: string; kind?: MemoryKind }) => patch<MemoryItem>(`/memory/${id}`, changes);
export const deleteMemory = (id: string) => del<{ deleted: boolean }>(`/memory/${id}`);
export const forgetEverything = () => del<{ deleted: number }>("/memory");
export const removeRule = (id: number) => del<{ deleted: boolean }>(`/memory/rules/${id}`);
