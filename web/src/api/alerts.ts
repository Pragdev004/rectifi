import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { get, qs } from "./client";
import type { IsoDate, Money, Percent } from "./types";

export type AlertSeverity = "act" | "watch" | "info";
export type PushStateWord = "pending" | "sending" | "sent" | "held" | "skipped" | "failed";

export interface AlertItem {
  id: number;
  kind: "cap" | "streak" | "bulk" | "digest";
  severity: AlertSeverity;
  title: string;
  detail: string;
  numbers: Record<string, string>;
  created_at: string;
  read_at: string | null;
  is_read: boolean;
  push_state: PushStateWord;
  can_snooze: boolean;
  has_rule: boolean;
}
export interface AlertList {
  items: AlertItem[];
  unread: number;
}

export type RuleScope = "merchant" | "group" | "category" | "total";
export type RulePeriod = "day" | "week" | "month";
export interface AlertRule {
  id: number;
  scope: RuleScope;
  target: string;
  period: RulePeriod;
  cap: Money;
  muted: boolean;
  snoozed_until: string | null;
}

export interface PresetSet { day: Money; week: Money; month: Money }
export interface Preset {
  scope: RuleScope;
  target: string;
  months: number;
  average: PresetSet;
  keep: PresetSet;
  trim: PresetSet;
}

export interface SafeToSpend {
  spendable: Money;
  source: "plan" | "budgets" | "income";
  spent_before_today: Money;
  committed: Money;
  committed_items: { name: string; amount: Money; day: number }[];
  days_left: number;
  daily: Money;
  spent_today: Money;
  left_today: Money;
  over_today: boolean;
}
export interface TodayView {
  today: IsoDate;
  unread: number;
  small_spends: { txn_id: number; label: string; group: string | null; amount: Money }[];
  income: { amount: Money | null; source: "manual" | "salary" | null };
  safe: SafeToSpend | null;
}

export interface AlertPrefs {
  push_on: boolean;
  private_notifications: boolean;
  quiet_start: string;
  quiet_end: string;
  max_per_hour: number;
  max_per_day: number;
  cooldown_hours: number;
  digest_on: boolean;
  digest_time: string;
  monthly_income: Money | null;
  spend_plan: Money | null;
  snooze_until: string | null;
  muted_kinds: string[];
  detected_income: Money | null;
  income_source: "manual" | "salary" | null;
}

export interface MerchantPattern { id: number; pattern: string; merchant: string; group: string; created_at: string }
export interface PushKey { configured: boolean; public_key: string | null; fingerprint: string | null }
export interface PushDevice { id: number; label: string | null; service: string; created_at: string; last_ok_at: string | null }

export interface LeakSegment {
  key: string;
  label: string;
  kind: "income" | "outflow" | "left";
  amount: Money;
  start: Money;
  end: Money;
  count: number;
}
export interface LeakMerchant { merchant: string; group: string; orders: number; total: Money; pct_of_income: Percent }
export interface LeakProjection { spent: Money; day: number; days_in_month: number; projected: Money; income: Money | null; pct_of_income: Percent }
export interface LeakReport {
  month: string;
  income: Money | null;
  income_source: "manual" | "credits" | null;
  spent: Money;
  left: Money | null;
  segments: LeakSegment[];
  merchants: LeakMerchant[];
  projection: LeakProjection | null;
}

export const alertKeys = {
  list: (status: string) => ["alerts", "list", status] as const,
  unread: ["alerts", "unread"] as const,
  today: ["alerts", "today"] as const,
  rules: ["alerts", "rules"] as const,
  presets: ["alerts", "presets"] as const,
  prefs: ["alerts", "prefs"] as const,
  patterns: ["alerts", "patterns"] as const,
  pushKey: ["alerts", "push-key"] as const,
  devices: ["alerts", "devices"] as const,
  leaks: (month: string | null) => ["leaks", month] as const,
};

export const useAlerts = (status: "unread" | "all") =>
  useQuery({ queryKey: alertKeys.list(status), queryFn: ({ signal }) => get<AlertList>(`/alerts${qs({ status })}`, signal) });

/** The little "3 new" in the menu. Asks once a minute; costs one tiny request. */
export const useUnread = () =>
  useQuery({
    queryKey: alertKeys.unread,
    queryFn: ({ signal }) => get<AlertList>(`/alerts${qs({ status: "unread", limit: 1 })}`, signal),
    select: (d) => d.unread,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

export const useToday = () => useQuery({ queryKey: alertKeys.today, queryFn: ({ signal }) => get<TodayView>("/alerts/today", signal), refetchInterval: 120_000 });
export const useRules = () => useQuery({ queryKey: alertKeys.rules, queryFn: ({ signal }) => get<{ items: AlertRule[] }>("/alerts/rules", signal) });
export const usePresets = () => useQuery({ queryKey: alertKeys.presets, queryFn: ({ signal }) => get<{ items: Preset[] }>("/alerts/presets", signal) });
export const usePrefs = () => useQuery({ queryKey: alertKeys.prefs, queryFn: ({ signal }) => get<AlertPrefs>("/alerts/prefs", signal) });
export const usePatterns = () =>
  useQuery({ queryKey: alertKeys.patterns, queryFn: ({ signal }) => get<{ items: MerchantPattern[]; groups: string[] }>("/merchants/patterns", signal) });
export const usePushKey = () => useQuery({ queryKey: alertKeys.pushKey, queryFn: ({ signal }) => get<PushKey>("/push/key", signal), staleTime: 300_000 });
export const useDevices = () => useQuery({ queryKey: alertKeys.devices, queryFn: ({ signal }) => get<{ items: PushDevice[] }>("/push/subscriptions", signal) });
export const useLeaks = (month: string | null) =>
  useQuery({
    queryKey: alertKeys.leaks(month),
    queryFn: ({ signal }) => get<LeakReport>(`/leaks${qs({ month })}`, signal),
    placeholderData: keepPreviousData,
  });
