// Formats money that the server already worked out. This file never adds, subtracts or compares amounts.
// It mirrors Python's rectifi.money.fmt_inr exactly (see money.cases.json, which both sides check), including the one quirk:
// with paise=false the paise are cut off, not rounded. If that is ever fixed, fix both sides and the cases file together.

const WIRE_MONEY = /^(-?)(\d+)\.(\d{2})$/;
const MINUS = "−";
const RUPEE = "₹";

export const NO_VALUE = "—";

export interface FormatOptions {
  paise?: boolean;
  sign?: boolean;
}

function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits;
  let head = digits.slice(0, -3);
  const tail = digits.slice(-3);
  const parts: string[] = [];
  while (head.length > 2) {
    parts.unshift(head.slice(-2));
    head = head.slice(0, -2);
  }
  if (head) parts.unshift(head);
  return parts.join(",") + "," + tail;
}

export interface MoneyParts {
  negative: boolean;
  positive: boolean;
  integer: string; // grouped, e.g. "1,25,000"
  paise: string; // "50"
}

/** Splits a wire amount ("125000.50", "-12.00"). Returns null when the text is not a 2-decimal amount. */
export function splitMoney(value: string): MoneyParts | null {
  const m = WIRE_MONEY.exec(value);
  if (!m) return null;
  const [, minus, whole, paise] = m as unknown as [string, string, string, string];
  const zero = /^0+$/.test(whole) && paise === "00";
  return {
    negative: minus === "-" && !zero,
    positive: minus !== "-" && !zero,
    integer: groupIndian(whole.replace(/^0+(?=\d)/, "")),
    paise,
  };
}

export function formatInr(value: string | null | undefined, options: FormatOptions = {}): string {
  const { paise = true, sign = false } = options;
  if (value === null || value === undefined) return NO_VALUE;
  const parts = splitMoney(value);
  if (!parts) {
    if (import.meta.env?.DEV) throw new Error(`Not a money string: ${JSON.stringify(value)}`);
    return NO_VALUE;
  }
  const text = RUPEE + parts.integer + (paise ? "." + parts.paise : "");
  if (parts.negative) return MINUS + text;
  if (sign && parts.positive) return "+" + text;
  return text;
}

/** Percent strings come as "27.5" (one decimal) or null. */
export function formatPercent(value: string | null | undefined): string {
  if (value === null || value === undefined || !/^-?\d+\.\d$/.test(value)) return NO_VALUE;
  return (value.startsWith("-") ? MINUS + value.slice(1) : value) + "%";
}

/** Compact Indian ticks for chart axes: 5000 -> ₹5K, 100000 -> ₹1L. Draws labels only; nothing is calculated from money here. */
export function compactInr(rupees: number): string {
  const abs = Math.abs(rupees);
  const sign = rupees < 0 ? MINUS : "";
  const trim = (n: number) => String(Math.round(n * 10) / 10).replace(/\.0$/, "");
  if (abs >= 10000000) return `${sign}${RUPEE}${trim(abs / 10000000)}Cr`;
  if (abs >= 100000) return `${sign}${RUPEE}${trim(abs / 100000)}L`;
  if (abs >= 1000) return `${sign}${RUPEE}${trim(abs / 1000)}K`;
  return `${sign}${RUPEE}${trim(abs)}`;
}
