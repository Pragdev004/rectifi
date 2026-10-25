import { describe, expect, it } from "vitest";
import cases from "./money.cases.json";
import { compactInr, formatInr, formatPercent, splitMoney } from "./money";

describe("formatInr matches Python fmt_inr", () => {
  for (const c of cases) {
    it(`${c.value} paise=${c.paise} sign=${c.sign}`, () => {
      expect(formatInr(c.value, { paise: c.paise, sign: c.sign })).toBe(c.expected);
    });
  }
});

describe("formatInr edges", () => {
  it("shows a dash for a missing value", () => {
    expect(formatInr(null)).toBe("—");
    expect(formatInr(undefined)).toBe("—");
  });
  it("never shows a negative zero", () => {
    expect(formatInr("-0.00")).toBe("₹0.00");
    expect(formatInr("-0.00", { paise: false })).toBe("₹0");
  });
  it("truncates paise when asked, like the Python side", () => {
    expect(formatInr("999.99", { paise: false })).toBe("₹999");
  });
  it("throws on text that is not a 2-decimal amount (development) so a bug is loud", () => {
    expect(() => formatInr("1250.5")).toThrow();
    expect(() => formatInr("abc")).toThrow();
    expect(() => formatInr("1,250.00")).toThrow();
  });
});

describe("splitMoney", () => {
  it("splits a wire amount", () => {
    expect(splitMoney("125000.50")).toEqual({ negative: false, positive: true, integer: "1,25,000", paise: "50" });
    expect(splitMoney("-12.00")?.negative).toBe(true);
    expect(splitMoney("0.00")).toMatchObject({ negative: false, positive: false });
    expect(splitMoney("12")).toBeNull();
  });
});

describe("formatPercent and compactInr", () => {
  it("formats server percentages", () => {
    expect(formatPercent("27.5")).toBe("27.5%");
    expect(formatPercent("-3.0")).toBe("−3.0%");
    expect(formatPercent(null)).toBe("—");
  });
  it("makes compact Indian ticks", () => {
    expect(compactInr(0)).toBe("₹0");
    expect(compactInr(5000)).toBe("₹5K");
    expect(compactInr(12500)).toBe("₹12.5K");
    expect(compactInr(100000)).toBe("₹1L");
    expect(compactInr(25000000)).toBe("₹2.5Cr");
  });
});
