import { describe, expect, it } from "vitest";
import { isPlumbing } from "./Traces";
import type { TraceSpan } from "../api/traces";

const span = (name: string, run_type = "chain", status = "ok"): TraceSpan => ({
  run_id: name, parent_run_id: null, depth: 0, name, run_type, model: null, status, started_at: "", ms: 1,
  input_tokens: null, output_tokens: null, input_preview: null, output_preview: null, error: null,
});

describe("isPlumbing", () => {
  it("hides LangChain wiring", () => {
    for (const n of ["RunnableSequence", "RunnableParallel<a,b>", "ChatPromptTemplate", "run", "LangGraph"]) expect(isPlumbing(span(n))).toBe(true);
  });
  it("keeps graph nodes, model calls, tools, retrievers and anything that failed", () => {
    expect(isPlumbing(span("retrieve"))).toBe(false);
    expect(isPlumbing(span("ChatOllama", "llm"))).toBe(false);
    expect(isPlumbing(span("HybridRetriever", "retriever"))).toBe(false);
    expect(isPlumbing(span("RunnableSequence", "chain", "error"))).toBe(false);
  });
});
