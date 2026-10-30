import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { InterruptCard } from "./InterruptCard";

const show = (kind: string, payload: Record<string, unknown>) =>
  renderToStaticMarkup(<InterruptCard interrupt={{ id: "1", kind, payload }} busy={false} onAnswer={() => {}} />);

describe("interrupt cards", () => {
  it("the RAG web-search pause is drawn by the library's WebConsent card", () => {
    const html = show("web_consent", { type: "web_search", query: "how does a SIP work", message: "m", choices: ["Search", "Answer without it"] });
    expect(html).toContain("how does a SIP work");
    expect(html).toContain("Answer without it");
    expect(html).toContain("Nothing about your money is sent");
  });
  it("the chart review shows the code", () => {
    expect(show("review_code", { code: "plt.bar([1],[2])" })).toContain("plt.bar");
  });
  it("memory asks before saving", () => {
    const html = show("memory", { text: "I prefer short answers" });
    expect(html).toContain("Save this to memory?");
    expect(html).toContain("I prefer short answers");
  });
});
