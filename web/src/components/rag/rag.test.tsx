import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { statusLine, type Citation, type KnowledgeStatus, type TraceEvent } from "../../api/knowledge";
import { CitationMark, Citations } from "./Citations";
import { ReflectionTrace, rowText, toneFor } from "./ReflectionTrace";
import { WebConsent } from "./WebConsent";

const cite = (over: Partial<Citation> = {}): Citation => ({
  n: 1, source: "guides/emergency-fund.md", title: "Emergency fund", chunk: 0, quote: "Keep three to six months of expenses.", corpus: "guide", label: "GUIDE", ...over,
});

describe("citations", () => {
  it("draws a superscript mark that links to its source", () => {
    const html = renderToStaticMarkup(<CitationMark n={2} />);
    expect(html).toContain('<sup');
    expect(html).toContain('href="#source-2"');
    expect(html).toContain("[2]");
  });

  it("lists each source with a word stamp, the title and the quote", () => {
    const html = renderToStaticMarkup(<Citations citations={[cite(), cite({ n: 2, corpus: "txn", label: "YOUR ENTRY", title: "Entry #4", quote: "2026-08-14 · paid ₹1,499.00 · CULT FIT" })]} />);
    expect(html).toContain('id="source-1"');
    expect(html).toContain('id="source-2"');
    expect(html).toContain("GUIDE");
    expect(html).toContain("YOUR ENTRY");
    expect(html).toContain("Entry #4");
    expect(html).toContain("₹1,499.00");
  });

  it("draws nothing when nothing was cited", () => {
    expect(renderToStaticMarkup(<Citations citations={[]} />)).toBe("");
  });

  it("never turns a quote into markup", () => {
    const html = renderToStaticMarkup(<Citations citations={[cite({ quote: "<img src=x onerror=alert(1)>" })]} />);
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });
});

describe("reflection trace", () => {
  const trace: TraceEvent[] = [
    { step: "ROUTED", verdict: "look it up", detail: "a guide question (model)" },
    { step: "GRADED", verdict: "3 relevant · 1 partly · 2 not", detail: "6 passages checked one by one" },
    { step: "CRAG", verdict: "CORRECT", detail: "at least one passage answers the question" },
    { step: "SUPPORTED", verdict: "PARTLY", detail: "twelve months is enough" },
    { step: "AMOUNTS", verdict: "NOT TRACED", detail: "not found in your records or your question: ₹5,000.00" },
  ];

  it("maps verdict words to tones, never colour alone", () => {
    expect(toneFor("CORRECT")).toBe("ok");
    expect(toneFor("fully")).toBe("ok");
    expect(toneFor("PARTLY")).toBe("near");
    expect(toneFor("AMBIGUOUS")).toBe("near");
    expect(toneFor("NOT TRACED")).toBe("over");
    expect(toneFor("WITHHELD")).toBe("over");
    expect(toneFor("look it up")).toBe("neutral");
  });

  it("writes each row in plain words", () => {
    expect(rowText(trace[0]!)).toEqual({ main: "Look it up", extra: "a guide question (model)" });
    expect(rowText(trace[2]!)).toEqual({ main: "CORRECT", extra: "at least one passage answers the question" });
    expect(rowText(trace[4]!).main).toContain("₹5,000.00");
  });

  it("renders a closed disclosure with one row per step, and says the checks are prompted", () => {
    const html = renderToStaticMarkup(<ReflectionTrace trace={trace} />);
    expect(html).toContain("How this answer was checked");
    expect(html.match(/<li/g)).toHaveLength(5);
    expect(html).toContain("ROUTED");
    expect(html).toContain("can be wrong");
    expect(html).toContain("plain code, not a model");
    expect(html).not.toContain(" open");
  });

  it("draws nothing for an empty trace", () => {
    expect(renderToStaticMarkup(<ReflectionTrace trace={[]} />)).toBe("");
  });
});

describe("web consent", () => {
  it("asks first and offers both choices", () => {
    const html = renderToStaticMarkup(
      <WebConsent request={{ type: "web_search", query: "how does a SIP work", message: "m", choices: ["Search", "Answer without it"] }} onChoose={() => {}} />,
    );
    expect(html).toContain("how does a SIP work");
    expect(html).toContain("Search");
    expect(html).toContain("Answer without it");
    expect(html).toContain("Nothing about your money is sent");
  });
});

describe("index status line", () => {
  const base: KnowledgeStatus = {
    embed_model: "nomic-embed-text", chunks: {}, chunks_total: 214, documents: {}, transactions: 0, other_model_chunks: 0,
    updated_at: "2026-10-25T12:00:00+00:00", guides_on_disk: 12, job: { state: "idle", done: 0, total: 0, error: null, started_at: null, finished_at: null },
  };
  it("matches the design wording", () => {
    expect(statusLine(base)).toMatch(/^Indexed with nomic-embed-text · 214 passages · updated 25 Oct$/);
  });
  it("says so when nothing is indexed", () => {
    expect(statusLine({ ...base, chunks_total: 0 })).toBe("Nothing is indexed yet with nomic-embed-text.");
  });
});
