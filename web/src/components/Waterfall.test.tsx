import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { LeakSegment } from "../api/alerts";
import { Waterfall } from "./Waterfall";

const seg = (key: string, label: string, kind: LeakSegment["kind"], amount: string, start: string, end: string, count = 0): LeakSegment => ({ key, label, kind, amount, start, end, count });

// What the server sends for ₹1,00,000 of income (it did all the arithmetic).
const SEGMENTS: LeakSegment[] = [
  seg("income", "Income", "income", "100000.00", "0.00", "100000.00"),
  seg("fixed", "Fixed bills", "outflow", "25000.00", "100000.00", "75000.00", 1),
  seg("big", "Big one-offs", "outflow", "1999.99", "75000.00", "73000.01", 1),
  seg("quick", "Quick commerce", "outflow", "12400.50", "73000.01", "60599.51", 31),
  seg("food", "Food delivery", "outflow", "0.00", "60599.51", "60599.51", 0),
  seg("shopping", "Shopping", "outflow", "333.33", "60599.51", "60266.18", 1),
  seg("other", "Other small spends", "outflow", "120.10", "60266.18", "60146.08", 3),
  seg("left", "Left", "left", "60146.08", "0.00", "60146.08"),
];

describe("Waterfall", () => {
  const html = renderToStaticMarkup(<Waterfall segments={SEGMENTS} />);

  it("shows the server's labels and amounts in Indian format", () => {
    for (const s of SEGMENTS) expect(html).toContain(s.label);
    expect(html).toContain("₹1,00,000");
    expect(html).toContain("₹25,000");
    expect(html).toContain("₹12,400");
    expect(html).toContain("31 payments");
    expect(html).toContain("1 payment<");
    expect(html).toContain("₹60,146");
  });

  it("draws each bar from the server's start and end, not from its own sums", () => {
    // Fixed bills: 100000 -> 75000 on a 100000 line is x=750, width=250 (the bar sits at the right end, then steps left).
    const fixed = /data-key="fixed".*?<rect class="[^"]*bar[^"]*" x="([\d.]+)" y="1" width="([\d.]+)"/.exec(html);
    expect(fixed?.slice(1)).toEqual(["750.0", "250.0"]);
    const income = /data-key="income".*?<rect class="[^"]*bar[^"]*" x="([\d.]+)" y="1" width="([\d.]+)"/.exec(html);
    expect(income?.slice(1)).toEqual(["0.0", "1000.0"]);
  });

  it("draws no bar for a segment with nothing in it, and says so in words", () => {
    const food = /data-key="food".*?<\/li>/.exec(html)![0];
    expect(food).not.toContain('y="1"');
    expect(food).toContain("Food delivery: ₹0 in 0 payments.");
  });

  it("states an overspend in words, not only in colour", () => {
    const over = renderToStaticMarkup(<Waterfall segments={[seg("income", "Income", "income", "1000.00", "0.00", "1000.00"), seg("other", "Other small spends", "outflow", "1500.00", "1000.00", "-500.00", 4), seg("left", "Overspent", "left", "-500.00", "0.00", "-500.00")]} />);
    expect(over).toContain("Overspent");
    expect(over).toContain("−₹500");
    expect(over).toContain("spending went ₹500 past income");
  });

  it("has no inline style (the page's security policy forbids it)", () => {
    expect(html).not.toContain("style=");
  });
});

describe("Waterfall source", () => {
  const source = readFileSync(new URL("./Waterfall.tsx", import.meta.url), "utf8").replace(/\/\/.*$/gm, "");

  it("does no money arithmetic: no sums, no running totals, no Decimal-like maths", () => {
    expect(source).not.toMatch(/\.reduce\(/);
    expect(source).not.toMatch(/\+=|-=|\*=/);
    // the only parseFloat calls are the drawing helpers: pos, and the two reads that set the line length
    expect(source.match(/parseFloat/g)?.length).toBe(3);
    expect(source).not.toMatch(/toFixed\(2\)/);
    expect(source).not.toMatch(/(start|end|amount)\s*[-+]\s*(start|end|amount)/);
  });
});
