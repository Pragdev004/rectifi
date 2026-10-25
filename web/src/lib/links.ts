import { qs } from "../api/client";
import type { InsightLink } from "../api/types";

const PAGES: Record<string, string> = {
  overview: "/",
  review: "/review",
  insights: "/insights",
  add: "/add",
  ledger: "/ledger",
  budgets: "/budgets",
  accounts: "/accounts",
  phone: "/phone",
  ask: "/ask",
};

/** Where an insight's button goes. The server names the page and its filters; the browser only turns that into a path. */
export function hrefFor(link: InsightLink | null | undefined): string | null {
  if (!link) return null;
  const base = PAGES[link.page];
  if (!base) return null;
  return base + qs(link.params);
}

export const pageHref = (page: string | null | undefined): string | null => (page ? (PAGES[page] ?? null) : null);
