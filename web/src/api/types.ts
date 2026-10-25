// What the server sends. Every amount is a string with two decimals ("1250.50", "-12.00"); every percentage is a string
// with one decimal or null. The browser formats these strings and never adds them up.

export type Money = string;
export type Percent = string | null;
export type IsoDate = string; // YYYY-MM-DD
export type Direction = "debit" | "credit";
export type CategoryKind = "expense" | "income" | "transfer";

export interface Category {
  id: number;
  name: string;
  kind: CategoryKind;
}
export interface AccountRef {
  id: number;
  name: string;
  last4: string | null;
}
export interface Meta {
  today: IsoDate;
  months: string[]; // "YYYY-MM", newest first
  weeks: IsoDate[]; // Mondays, newest first
  categories: Category[];
  accounts: AccountRef[];
  default_account_id: number | null;
}

export interface Txn {
  id: number;
  date: IsoDate;
  amount: Money;
  direction: Direction;
  payee: string | null;
  description: string | null;
  note: string | null;
  source: string;
  account_id: number | null;
  account: string | null;
  category_id: number | null;
  category: string | null;
  category_kind: CategoryKind | null;
}

export interface CategoryShare {
  category: string;
  amount: Money;
  count: number;
  share: Percent;
}
export interface Summary {
  income: Money;
  spending: Money;
  saved: Money;
  savings_rate: Percent;
  count: number;
  by_category: CategoryShare[];
}

export type BudgetState = "ok" | "near" | "over";
export interface BudgetRowData {
  category: string;
  limit: Money;
  spent: Money;
  remaining: Money;
  percent: Percent;
  state: BudgetState;
}
export interface BudgetStatus {
  month: string;
  pace: Percent;
  pace_day: number;
  days_in_month: number;
  categories: BudgetRowData[];
  total_limit: Money;
  total_spent: Money;
}
export interface Pace {
  through: IsoDate | null;
  total_limit: Money;
  days: { day: IsoDate; spent: Money; cumulative: Money; pace: Money }[];
}

export interface Recon {
  account_id: number;
  opening_date: IsoDate;
  opening: Money;
  credits: Money;
  debits: Money;
  count: number;
  expected_closing: Money;
  closing_date: IsoDate;
  closing: Money;
  gap: Money;
  balanced: boolean;
  explanation: string;
}
export interface Proof {
  account: AccountRef;
  reconcile: Recon | null;
  reason: string | null;
}

export type Severity = "act" | "watch" | "info";
export interface InsightLink {
  page: string;
  params: Record<string, string>;
}
export interface Insight {
  key: string;
  kind: string;
  severity: Severity;
  title: string;
  detail: string;
  numbers: Record<string, string>;
  action_label: string | null;
  action_page: string | null;
  is_data_quality: boolean;
  can_apply: boolean;
  link: InsightLink | null;
}

export interface Overview {
  month: string;
  start: IsoDate;
  end: IsoDate;
  summary: Summary;
  budgets: BudgetStatus;
  pace: Pace;
  recent: Txn[];
  proofs: Proof[];
  top_insights: Insight[] | null;
  insights_error: boolean;
}

export interface Review {
  week_start: IsoDate;
  week_end: IsoDate;
  through: IsoDate;
  days_elapsed: number;
  is_current: boolean;
  count: number;
  income: Money;
  spending: Money;
  saved: Money;
  savings_rate: Percent;
  prev_start: IsoDate;
  prev_end: IsoDate;
  prev_spending: Money;
  spending_change: Money;
  spending_change_pct: Percent;
  trend: "up" | "down" | "flat" | "no_prior";
  top_categories: CategoryShare[];
  biggest_debits: Txn[];
  uncategorised_count: number;
  unlinked_count: number;
  account_count: number;
  all_clear: boolean;
  budget_month: string | null;
  budgets_needing_attention: { category: string; limit: Money; spent: Money; percent: Percent; state: BudgetState }[];
  accounts_needing_attention: { id: number; name: string; reasons: string[] }[];
  last_commitment: string | null;
  commitment: string | null;
  act_insights: Insight[];
  insights_error: boolean;
}

export interface InsightsList {
  span: { count: number; first: IsoDate | null; last: IsoDate | null; days: number };
  items: Insight[];
}
export interface ExplainResult {
  text: string | null;
  dropped: Money[];
  error: string | null;
}

export interface TransactionList {
  items: Txn[];
  truncated: boolean;
  limit: number;
}
export interface AddResult {
  status: "added" | "duplicate";
  transaction: Txn;
}
export interface EditResult {
  transaction: Txn;
  learned_rule: string | null;
}

export interface ParsedSms {
  amount: Money;
  direction: Direction;
  txn_date: IsoDate | null;
  payee: string | null;
  upi_ref: string | null;
  account_last4: string | null;
  account: AccountRef | null;
  warnings: string[];
}
export interface InboxPrefill {
  amount: Money | null;
  direction: Direction | null;
  txn_date: IsoDate | null;
  payee: string | null;
  upi_ref: string | null;
  account_last4: string | null;
  account_id: number | null;
  raw_text: string;
  inbox_id: number;
}

export interface ImportBatch {
  id: number;
  account_id: number | null;
  account: string | null;
  filename: string | null;
  created_at: string;
  added: number;
  merged: number;
  duplicates: number;
  needs_review: number;
  undone_at: string | null;
  rows_left: number;
}
export interface ImportedOutcome {
  outcome: "imported" | "already_imported";
  batch: ImportBatch;
  how: "digits" | "chosen" | "most_recent" | "only" | null;
  needs_review_lines: string[];
  skipped: string[];
  reconcile: Recon | null;
  reconcile_reason: string | null;
}
export interface PreviewRow {
  date: IsoDate;
  narration: string;
  direction: Direction;
  amount: Money;
  balance: Money | null;
}
export interface Preview {
  rows: PreviewRow[];
  review_rows: { date: IsoDate; narration: string; amount: Money; why: string }[];
  problems: string[];
  totals: { count: number; money_out: Money; money_in: Money; first: IsoDate | null; last: IsoDate | null };
  suggested_account_id: number | null;
}
export type Mapping = Record<string, string | null>;
export interface PreviewOutcome {
  outcome: "preview";
  upload_id: string;
  filename: string;
  reasons: string[];
  account_reason: string | null;
  columns: string[];
  mapping: Mapping;
  preview: Preview | null;
  preview_error: string | null;
}
export type ImportOutcome = ImportedOutcome | PreviewOutcome;
export interface UndoResult {
  removed: number;
  snapshots_restored: number;
  already_undone: boolean;
  batch: ImportBatch | null;
}

export interface BudgetsPage {
  status: BudgetStatus;
  expense_categories: string[];
  previous_month: string;
}
export interface AccountItem {
  id: number;
  name: string;
  last4: string | null;
  transactions: number;
  last_snapshot: IsoDate | null;
  reconcile: Recon | null;
  reason: string | null;
}
export interface AccountsPage {
  items: AccountItem[];
  default_account_id: number | null;
}
export interface BalanceResult {
  account_id: number;
  as_of: IsoDate;
  balance: Money;
  reconcile: Recon | null;
  reason: string | null;
}

export interface PhoneStatus {
  tailscale: { installed: boolean; name: string | null; app_published: boolean; gateway_published: boolean };
  app_url: string | null;
  gateway: { up: boolean; port: number; host: string; open: boolean };
  gateway_url: string | null;
  health_url: string | null;
  lan_addresses: { ip: string; label: string }[];
  tailscale_login: string | null;
}
export interface Device {
  id: number;
  name: string;
  created_at: string;
  last_seen_at: string | null;
  revoked_at: string | null;
}
export interface PairResult {
  device: { id: number; name: string; created_at: string };
  token: string;
  setup_file: { filename: string; content: string } | null;
  setup_unavailable_reason: string | null;
  manual: { webhook_url: string | null; headers_json: string; template: string; sender_regex: string; text_regex: string };
}
export interface InboxItem {
  id: number;
  sender: string | null;
  raw_text: string;
  sent_at: string | null;
  received_at: string;
  reason: string | null;
  device: string | null;
}

export interface AskStatus {
  ok: boolean;
  why: string;
  notice: string | null;
  model: string;
}
export interface AskStep {
  tool: string;
  output: string;
  error: boolean;
}
export type TranscriptItem =
  | { role: "user"; text: string }
  | { role: "assistant"; text: string; unverified: Money[]; steps: AskStep[] };
export type AskEvent =
  | { type: "step"; tool: string; error: boolean }
  | { type: "tick" }
  | { type: "done"; items: TranscriptItem[] }
  | { type: "error"; message: string };
