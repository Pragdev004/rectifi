import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { Router } from "wouter";
import { describe, expect, it } from "vitest";
import { alertKeys, type TodayView } from "../api/alerts";
import { TodayStrip } from "./TodayStrip";

function render(view: TodayView): string {
  const client = new QueryClient();
  client.setQueryData(alertKeys.today, view);
  return renderToStaticMarkup(<QueryClientProvider client={client}><Router ssrPath="/"><TodayStrip /></Router></QueryClientProvider>);
}

// The server worked out every figure here. The strip must show them, not recompute them.
const VIEW: TodayView = {
  today: "2026-09-14",
  unread: 3,
  small_spends: [{ txn_id: 1, label: "Swiggy", group: "Food delivery", amount: "349.00" }],
  income: { amount: "100000.00", source: "manual" },
  safe: {
    spendable: "100000.00", source: "income", spent_before_today: "40000.00", committed: "12000.00",
    committed_items: [{ name: "Rent", amount: "12000.00", day: 28 }], days_left: 17, daily: "2823.00", spent_today: "349.00",
    left_today: "2474.00", over_today: false,
  },
};

describe("TodayStrip", () => {
  it("shows the server's figures and the unread count in words", () => {
    const html = render(VIEW);
    expect(html).toContain("Safe to spend today");
    expect(html).toContain("₹2,823");
    expect(html).toContain("Left today");
    expect(html).toContain("₹2,474");
    expect(html).toContain("Swiggy");
    expect(html).toContain("3 new alerts");
    expect(html).toContain("₹40,000");
    expect(html).toContain("₹12,000");
    expect(html).not.toContain("style=");
  });
  it("says Over today by (a word, not only a colour) when the server says so", () => {
    const html = render({ ...VIEW, safe: { ...VIEW.safe!, left_today: "-150.00", over_today: true } });
    expect(html).toContain("Over today by");
    expect(html).toContain("₹150");
    expect(html).not.toContain("−₹150");
  });
  it("tells you what to set when the server has no safe figure", () => {
    const html = render({ ...VIEW, safe: null, unread: 0 });
    expect(html).toContain("Set a budget or your monthly income to see this.");
    expect(html).toContain("Leak alerts");
  });
});

describe("the alert pages do no money arithmetic", () => {
  for (const file of ["TodayStrip.tsx", "../pages/Alerts.tsx", "../pages/Leaks.tsx", "../components/shell/UnreadChip.tsx"]) {
    it(`${file} only formats what the server sent`, () => {
      const source = readFileSync(new URL(file, import.meta.url), "utf8").replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
      expect(source).not.toMatch(/parseFloat|\.reduce\(|toFixed|Math\.round|BigInt/);
      expect(source).not.toMatch(/style=/);
    });
  }
});
