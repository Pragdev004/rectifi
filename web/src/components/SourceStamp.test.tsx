import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { CategorySource } from "../api/classify";
import { SourceStamp, sourceWord } from "./SourceStamp";

describe("SourceStamp words", () => {
  const cases: [CategorySource | null, number | null, string][] = [
    ["user", 100, "Yours"],
    ["learned", 98, "Yours"],
    ["rule", 95, "Rule"],
    ["heuristic", 85, "Pattern"],
    ["retrieval", 70, "Similar"],
    ["llm", 82, "Model 82%"],
    ["fallback", 0, "Unsure"],
    [null, null, "Earlier"],
  ];
  for (const [source, confidence, word] of cases) {
    it(`${source ?? "no source"} says ${word}`, () => {
      expect(sourceWord(source, confidence)).toBe(word);
      expect(renderToStaticMarkup(<SourceStamp source={source} confidence={confidence} />)).toContain(word);
    });
  }
  it("shows the model without a number when none is stored", () => {
    expect(sourceWord("llm", null)).toBe("Model");
  });
});
