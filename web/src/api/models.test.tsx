import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ModelStamps } from "../components/ModelCheck";
import { stampRows, type ModelProbe } from "./models";

const base: ModelProbe = {
  ok: true, stage: "ok", http_status: null, provider_message: "", latency_ms: 3200, tools_ok: true, tools_as_text: false, hints: [],
  provider: "ollama", model: "qwen2.5:7b", role: "chat", cached: false,
  stages: [
    { name: "reachable", ok: true, ms: 4, detail: "Ollama 0.12.0" },
    { name: "model", ok: true, ms: 8, detail: "qwen2.5:7b is installed." },
    { name: "capabilities", ok: true, ms: null, detail: "Ollama lists tool support: completion, tools" },
    { name: "chat", ok: true, ms: 3200, detail: "The model replied." },
    { name: "tools", ok: true, ms: 900, detail: "The model called the tool." },
  ],
};
const stamps = (p: ModelProbe | null) => stampRows(p).map((r) => r.stamp);

describe("model check stamps", () => {
  it("says every state in words", () => {
    expect(stamps(base)).toEqual(["Reachable", "Model found", "Replied 3.2 s", "Tools OK"]);
    expect(stampRows(base).every((r) => r.tone === "ok")).toBe(true);
  });

  it("before any check, nothing is claimed", () => {
    expect(stamps(null)).toEqual(["Not checked", "Not checked", "Not checked", "Not checked"]);
  });

  it("a missing model stops after the model step", () => {
    const probe: ModelProbe = {
      ...base, ok: false, stage: "model", http_status: 404, tools_ok: null,
      stages: [base.stages[0]!, { name: "model", ok: false, ms: null, detail: 'model "x" not found' }],
    };
    expect(stamps(probe)).toEqual(["Reachable", "Model missing", "Not checked", "Not checked"]);
  });

  it("nothing answering is not reachable", () => {
    const probe: ModelProbe = { ...base, ok: false, stage: "reachable", tools_ok: null, stages: [{ name: "reachable", ok: false, ms: null, detail: "Can't connect" }] };
    expect(stamps(probe)[0]).toBe("Not reachable");
  });

  it("a tool call written as text is its own state", () => {
    const probe: ModelProbe = {
      ...base, ok: false, stage: "tools", tools_ok: false, tools_as_text: true,
      stages: [...base.stages.slice(0, 4), { name: "tools", ok: false, ms: null, detail: "The model wrote the tool call as text." }],
    };
    expect(stamps(probe)).toEqual(["Reachable", "Model found", "Replied 3.2 s", "Tools as text"]);
  });

  it("a model without tool support says no tools", () => {
    const probe: ModelProbe = {
      ...base, ok: false, stage: "capabilities", tools_ok: false,
      stages: [base.stages[0]!, base.stages[1]!, { name: "capabilities", ok: false, ms: null, detail: "tiny:1b does not support tools." }],
    };
    const rows = stampRows(probe);
    expect(rows.map((r) => r.stamp)).toEqual(["Reachable", "Model found", "Not checked", "No tools"]);
    expect(rows[3]?.detail).toContain("does not support tools");
  });

  it("a job that needs no tools passes without them", () => {
    const probe: ModelProbe = { ...base, role: "classify", tools_ok: null, stages: base.stages.slice(0, 4) };
    expect(stamps(probe)[3]).toBe("Not needed");
  });

  it("draws one list item per row, with the words in the markup", () => {
    const html = renderToStaticMarkup(<ModelStamps probe={base} />);
    expect(html.match(/<li /g)).toHaveLength(4);
    for (const word of ["Reachable", "Model found", "Replied 3.2 s", "Tools OK"]) expect(html).toContain(word);
  });
});
