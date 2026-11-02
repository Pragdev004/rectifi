import { OWNER_ONLY_PATHS, hasAccount, isOwnerView, type AuthStatus } from "../../api/auth";
import {
  BellRing, BookText, BookOpen, Brain, ChartColumn, ClipboardCheck, Droplets, House, Landmark, Lightbulb, MessageSquare, PiggyBank, ScrollText, Smartphone, UserRound,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Other paths that count as this item being open. */
  also?: string[];
  /** Shown only when people have accounts (multi-user mode) and you are signed in. */
  needs?: "account";
}

export const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: "Daily",
    items: [
      { to: "/", label: "Overview", icon: House, also: ["/overview"] },
      { to: "/review", label: "Review", icon: ClipboardCheck },
      { to: "/insights", label: "Insights", icon: Lightbulb },
      { to: "/alerts", label: "Alerts", icon: BellRing },
      { to: "/ask", label: "Ask", icon: MessageSquare },
    ],
  },
  {
    title: "Records",
    items: [
      { to: "/ledger", label: "Ledger", icon: BookText },
      { to: "/charts", label: "Charts", icon: ChartColumn },
      { to: "/leaks", label: "Leaks", icon: Droplets },
      { to: "/budgets", label: "Budgets", icon: PiggyBank },
      { to: "/accounts", label: "Accounts", icon: Landmark },
    ],
  },
  {
    title: "Setup",
    items: [
      { to: "/phone", label: "Phone", icon: Smartphone },
      { to: "/library", label: "Library", icon: BookOpen },
      { to: "/memory", label: "Memory", icon: Brain },
      { to: "/traces", label: "Traces", icon: ScrollText },
      { to: "/settings", label: "Settings", icon: UserRound, needs: "account" },
    ],
  },
];

/** What the phone's top bar says for each page. */
export const PAGE_NAMES: Record<string, string> = {
  "/": "Overview", "/overview": "Overview", "/review": "Review", "/insights": "Insights", "/alerts": "Alerts", "/leaks": "Leaks", "/ask": "Ask", "/add": "Add",
  "/ledger": "Ledger", "/budgets": "Budgets", "/accounts": "Accounts", "/phone": "Phone",
  "/charts": "Charts", "/library": "Library", "/memory": "Memory", "/traces": "Traces", "/login": "Sign in", "/settings": "Settings",
};

export const MORE_ITEMS: NavItem[] = [
  { to: "/review", label: "Review", icon: ClipboardCheck },
  { to: "/insights", label: "Insights", icon: Lightbulb },
  { to: "/alerts", label: "Alerts", icon: BellRing },
  { to: "/charts", label: "Charts", icon: ChartColumn },
  { to: "/leaks", label: "Leaks", icon: Droplets },
  { to: "/budgets", label: "Budgets", icon: PiggyBank },
  { to: "/accounts", label: "Accounts", icon: Landmark },
  { to: "/phone", label: "Phone", icon: Smartphone },
  { to: "/library", label: "Library", icon: BookOpen },
  { to: "/memory", label: "Memory", icon: Brain },
  { to: "/traces", label: "Traces", icon: ScrollText },
  { to: "/settings", label: "Settings", icon: UserRound, needs: "account" },
];

export function isActive(path: string, item: NavItem): boolean {
  return path === item.to || (item.also?.includes(path) ?? false);
}

/** The menu for one person. A friend does not see the owner-only pages (the server answers 403 for them anyway); the Settings page needs an account. */
export function visibleItems<T extends NavItem>(items: T[], status: AuthStatus | undefined): T[] {
  const owner = isOwnerView(status);
  return items.filter((i) => {
    if (i.needs === "account" && !hasAccount(status)) return false;
    return owner || !(OWNER_ONLY_PATHS as readonly string[]).includes(i.to);
  });
}

export function visibleGroups(status: AuthStatus | undefined): { title: string; items: NavItem[] }[] {
  return NAV_GROUPS.map((g) => ({ title: g.title, items: visibleItems(g.items, status) })).filter((g) => g.items.length > 0);
}
