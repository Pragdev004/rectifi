import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "./client";
import type { Tone } from "../components/ui";

export type ModelRole = "chat" | "classify" | "code" | "grade";

/** One step of a model check, in the order it ran. A step that was never reached is not in the list. */
export interface ProbeStage {
  name: "reachable" | "model" | "capabilities" | "chat" | "tools";
  ok: boolean;
  ms: number | null;
  detail: string;
}

/** What POST /api/models/probe sends (and GET /api/models/status repeats for the last check under a minute old). */
export interface ModelProbe {
  ok: boolean;
  /** "ok", or the step that failed: config, reachable, model, capabilities, chat, tools. */
  stage: string;
  http_status: number | null;
  provider_message: string;
  latency_ms: number | null;
  tools_ok: boolean | null;
  tools_as_text: boolean;
  hints: string[];
  provider: string;
  model: string;
  role: ModelRole;
  stages: ProbeStage[];
  cached: boolean;
}

export interface ModelStatus {
  ok: boolean;
  why: string;
  notice: string;
  provider: string;
  model: string;
  roles: { role: ModelRole; model: string; named: boolean }[];
  embed_model: string | null;
  probe: ModelProbe | null;
}

export const modelKeys = { status: ["models", "status"] as const };

export const useModelStatus = () =>
  useQuery({ queryKey: modelKeys.status, queryFn: ({ signal }) => get<ModelStatus>("/models/status", signal), staleTime: 30_000 });

/** Runs a real check. It can take a minute the first time, because the model has to load. */
export function useProbeModel(role: ModelRole = "chat") {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => post<ModelProbe>("/models/probe", { role }),
    onSettled: () => void client.invalidateQueries({ queryKey: modelKeys.status }),
  });
}

export interface StampRow {
  key: string;
  label: string;
  stamp: string;
  tone: Tone;
  detail: string;
}

const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

/** The four stamp rows of a check: REACHABLE, MODEL FOUND, REPLIED 3.2 s, TOOLS OK / TOOLS AS TEXT / NO TOOLS.
 *  A step that did not run says NOT CHECKED. Every state is a word, never only a colour. */
export function stampRows(probe: ModelProbe | null): StampRow[] {
  const stage = (name: ProbeStage["name"]) => probe?.stages.find((s) => s.name === name);
  const idle = (key: string, label: string): StampRow => ({ key, label, stamp: "Not checked", tone: "neutral", detail: "" });
  const reached = stage("reachable");
  const found = stage("model");
  const replied = stage("chat");
  const tools = stage("tools");
  const caps = stage("capabilities");

  const rows: StampRow[] = [];
  rows.push(!reached ? idle("reachable", "Reachable")
    : reached.ok ? { key: "reachable", label: "Reachable", stamp: "Reachable", tone: "ok", detail: reached.detail }
    : { key: "reachable", label: "Reachable", stamp: "Not reachable", tone: "over", detail: reached.detail });
  rows.push(!found ? idle("model", "Model")
    : found.ok ? { key: "model", label: "Model", stamp: "Model found", tone: "ok", detail: found.detail }
    : { key: "model", label: "Model", stamp: "Model missing", tone: "over", detail: found.detail });
  rows.push(!replied ? idle("chat", "Reply")
    : replied.ok ? { key: "chat", label: "Reply", stamp: `Replied ${replied.ms === null ? "" : seconds(replied.ms)}`.trim(), tone: "ok", detail: replied.detail }
    : { key: "chat", label: "Reply", stamp: "No reply", tone: "over", detail: replied.detail });

  let toolsRow: StampRow = idle("tools", "Tools");
  if (probe?.tools_as_text) toolsRow = { key: "tools", label: "Tools", stamp: "Tools as text", tone: "over", detail: tools?.detail ?? "" };
  else if (tools?.ok) toolsRow = { key: "tools", label: "Tools", stamp: "Tools OK", tone: "ok", detail: tools.detail };
  else if (probe?.tools_ok === false) toolsRow = { key: "tools", label: "Tools", stamp: "No tools", tone: "over", detail: (tools ?? caps)?.detail ?? "" };
  else if (probe?.ok && probe.tools_ok === null) toolsRow = { key: "tools", label: "Tools", stamp: "Not needed", tone: "neutral", detail: "This job does not call tools." };
  rows.push(toolsRow);
  return rows;
}
