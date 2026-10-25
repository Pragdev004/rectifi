import {
  BookText, ClipboardCheck, House, Landmark, Lightbulb, MessageSquare, PiggyBank, Smartphone,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Other paths that count as this item being open. */
  also?: string[];
}

export const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: "Daily",
    items: [
      { to: "/", label: "Overview", icon: House, also: ["/overview"] },
      { to: "/review", label: "Review", icon: ClipboardCheck },
      { to: "/insights", label: "Insights", icon: Lightbulb },
      { to: "/ask", label: "Ask", icon: MessageSquare },
    ],
  },
  {
    title: "Records",
    items: [
      { to: "/ledger", label: "Ledger", icon: BookText },
      { to: "/budgets", label: "Budgets", icon: PiggyBank },
      { to: "/accounts", label: "Accounts", icon: Landmark },
    ],
  },
  { title: "Setup", items: [{ to: "/phone", label: "Phone", icon: Smartphone }] },
];

/** What the phone's top bar says for each page. */
export const PAGE_NAMES: Record<string, string> = {
  "/": "Overview", "/overview": "Overview", "/review": "Review", "/insights": "Insights", "/ask": "Ask", "/add": "Add",
  "/ledger": "Ledger", "/budgets": "Budgets", "/accounts": "Accounts", "/phone": "Phone",
};

export const MORE_ITEMS: NavItem[] = [
  { to: "/review", label: "Review", icon: ClipboardCheck },
  { to: "/insights", label: "Insights", icon: Lightbulb },
  { to: "/budgets", label: "Budgets", icon: PiggyBank },
  { to: "/accounts", label: "Accounts", icon: Landmark },
  { to: "/phone", label: "Phone", icon: Smartphone },
];

export function isActive(path: string, item: NavItem): boolean {
  return path === item.to || (item.also?.includes(path) ?? false);
}
