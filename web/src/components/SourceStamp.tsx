import type { CategorySource } from "../api/classify";
import type { Tone } from "./ui";
import { Stamp } from "./ui";

/** The word on the stamp for each way a category can be chosen. A stamp always carries a word, never only a colour. */
export function sourceWord(source: CategorySource | null, confidence?: number | null): string {
  switch (source) {
    case "user":
    case "learned":
      return "Yours";
    case "rule":
      return "Rule";
    case "heuristic":
      return "Pattern";
    case "retrieval":
      return "Similar";
    case "llm":
      return confidence === null || confidence === undefined ? "Model" : `Model ${confidence}%`;
    case "fallback":
      return "Unsure";
    default:
      return "Earlier"; // an entry from before Rectifi recorded where its category came from
  }
}

const TONE: Record<CategorySource, Tone> = {
  user: "accent", learned: "accent", rule: "neutral", heuristic: "neutral", retrieval: "neutral", llm: "neutral", fallback: "near",
};

export const sourceHelp: Record<CategorySource, string> = {
  user: "You chose this category.",
  learned: "A rule you taught Rectifi chose this.",
  rule: "A built-in merchant rule chose this.",
  heuristic: "A word in the narration (like ATM or salary) chose this.",
  retrieval: "This looks like a payment you sorted before.",
  llm: "The local model chose this.",
  fallback: "Rectifi was not sure, so this is in Miscellaneous.",
};

export function SourceStamp({ source, confidence, className }: { source: CategorySource | null; confidence?: number | null; className?: string }) {
  return (
    <Stamp tone={source ? TONE[source] : "neutral"} className={className}>
      {sourceWord(source, confidence)}
    </Stamp>
  );
}
