import { describe, expect, it } from "vitest";
import type { ClassifyJob } from "../../api/classify";
import { jobLine, summaryLine } from "./ImportCategories";

const job = (over: Partial<ClassifyJob>): ClassifyJob => ({
  id: 1, batch_id: 1, status: "running", total: 180, done: 120, by_source: {}, model: null, error: null, finished: false, ...over,
});

describe("import categories text", () => {
  it("builds the summary line and leaves out zero parts", () => {
    expect(summaryLine({ total: 312, by_source: { rule: 200, heuristic: 41, retrieval: 38, llm: 20, fallback: 13 }, miscellaneous: 13 }))
      .toBe("312 entries · 241 by rules · 38 like your past choices · 20 by the local model · 13 in Miscellaneous");
    expect(summaryLine({ total: 1, by_source: { user: 1 }, miscellaneous: 0 })).toBe("1 entry · 1 chosen by you");
  });
  it("says what the model is doing", () => {
    expect(jobLine(job({}), 60)?.text).toBe("Sorting the rest with the local model… 120 of 180");
    expect(jobLine(job({ status: "no_model" }), 13)?.text).toBe("Model is off. 13 entries wait in Miscellaneous.");
    expect(jobLine(job({ status: "no_model" }), 1)?.text).toBe("Model is off. 1 entry waits in Miscellaneous.");
    expect(jobLine(job({ status: "failed", error: "Ollama isn't running." }), 3)?.text).toBe("Ollama isn't running.");
    expect(jobLine(job({ status: "cancelled" }), 3)).toBeNull();
    expect(jobLine(null, 3)).toBeNull();
  });
});
