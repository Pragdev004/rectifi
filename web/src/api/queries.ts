import { keepPreviousData, useQuery, type QueryClient } from "@tanstack/react-query";
import { get, qs } from "./client";
import type {
  AccountsPage, BudgetsPage, Device, InboxItem, InsightsList, Meta, Overview, PhoneStatus, Review, TransactionList,
} from "./types";

export const keys = {
  meta: ["meta"] as const,
  overview: (month: string | null) => ["overview", month] as const,
  review: (week: string | null) => ["review", week] as const,
  insights: ["insights"] as const,
  transactions: (params: TxnParams) => ["transactions", params] as const,
  budgets: (month: string | null) => ["budgets", month] as const,
  accounts: ["accounts"] as const,
  phoneStatus: ["phone", "status"] as const,
  phoneDevices: ["phone", "devices"] as const,
  phoneInbox: ["phone", "inbox"] as const,
};

/** After any change to the books, everything that shows figures is out of date. */
export function refreshBooks(client: QueryClient) {
  for (const key of ["overview", "review", "insights", "transactions", "budgets", "accounts", "meta"]) {
    void client.invalidateQueries({ queryKey: [key] });
  }
}

export const useMeta = () => useQuery({ queryKey: keys.meta, queryFn: ({ signal }) => get<Meta>("/meta", signal), staleTime: 60_000 });

export const useOverview = (month: string | null) =>
  useQuery({
    queryKey: keys.overview(month),
    queryFn: ({ signal }) => get<Overview>(`/overview${qs({ month })}`, signal),
    placeholderData: keepPreviousData,
  });

export const useReview = (week: string | null) =>
  useQuery({
    queryKey: keys.review(week),
    queryFn: ({ signal }) => get<Review>(`/review${qs({ week })}`, signal),
    placeholderData: keepPreviousData,
  });

export const useInsights = () => useQuery({ queryKey: keys.insights, queryFn: ({ signal }) => get<InsightsList>("/insights", signal) });

export interface TxnParams {
  period: string;
  category: string;
  account: string;
  search: string;
  /** How the category was chosen ("" = any): user, learned, rule, heuristic, retrieval, llm, fallback, earlier. */
  source: string;
}
export const useTransactions = (params: TxnParams) =>
  useQuery({
    queryKey: keys.transactions(params),
    queryFn: ({ signal }) => get<TransactionList>(`/transactions${qs({ ...params })}`, signal),
    placeholderData: keepPreviousData,
  });

export const useBudgets = (month: string | null) =>
  useQuery({
    queryKey: keys.budgets(month),
    queryFn: ({ signal }) => get<BudgetsPage>(`/budgets${qs({ month })}`, signal),
    placeholderData: keepPreviousData,
  });

export const useAccounts = () => useQuery({ queryKey: keys.accounts, queryFn: ({ signal }) => get<AccountsPage>("/accounts", signal) });

export const usePhoneStatus = () => useQuery({ queryKey: keys.phoneStatus, queryFn: ({ signal }) => get<PhoneStatus>("/phone/status", signal) });
export const usePhoneDevices = () => useQuery({ queryKey: keys.phoneDevices, queryFn: ({ signal }) => get<Device[]>("/phone/devices", signal) });
export const usePhoneInbox = () => useQuery({ queryKey: keys.phoneInbox, queryFn: ({ signal }) => get<InboxItem[]>("/phone/inbox", signal) });
