import { SlidersHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useMeta, useTransactions } from "../api/queries";
import type { Txn } from "../api/types";
import { Button, Field, LinkButton, Select, TextInput } from "../components/forms";
import { Dialog } from "../components/Dialog";
import { EntrySheet } from "../components/ledger/EntrySheet";
import { LedgerTable } from "../components/ledger/LedgerTable";
import { LedgerList } from "../components/LedgerList";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { Callout, EmptyState, Skeleton } from "../components/ui";
import { monthLabel } from "../lib/dates";
import { useIsDesktop } from "../lib/media";
import { useUrlState } from "../lib/urlState";
import styles from "../components/ledger/Ledger.module.css";

/** "Sorted by": how the category was chosen. Matches the stamps on each row. */
const SOURCE_FILTERS: [string, string][] = [
  ["fallback", "Miscellaneous (not sure)"], ["llm", "Local model"], ["user", "You"], ["learned", "Taught rules"],
  ["rule", "Built-in rules"], ["heuristic", "Narration words"], ["retrieval", "Like past choices"], ["earlier", "Before this feature"],
];

function LedgerSkeleton() {
  return (
    <div style={{ display: "grid", gap: 12 }} aria-hidden="true">
      {Array.from({ length: 7 }, (_, i) => <Skeleton key={i} height={56} />)}
    </div>
  );
}

export default function LedgerPage() {
  const desktop = useIsDesktop();
  const meta = useMeta();
  const [f, setF] = useUrlState({ period: "all", category: "", account: "", search: "", source: "" });
  const [text, setText] = useState(f.search);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [openId, setOpenId] = useState<number | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  // Typing waits a moment (250 ms) before it changes the address and fetches.
  useEffect(() => {
    if (text === f.search) return;
    const t = window.setTimeout(() => setF({ search: text.trim() }, { replace: true }), 250);
    return () => window.clearTimeout(t);
  }, [text]);
  useEffect(() => setText(f.search), [f.search]);

  // "/" jumps to the search box on a laptop, like in most tools.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement | null;
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable)) return;
      e.preventDefault();
      searchRef.current?.focus();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const query = useTransactions({ period: f.period, category: f.category, account: f.account, search: f.search, source: f.source });
  const categories = meta.data?.categories ?? [];
  const accounts = meta.data?.accounts ?? [];
  const activeFilters = [f.period !== "all", f.category !== "", f.account !== "", f.source !== ""].filter(Boolean).length;
  const open: Txn | null = openId === null ? null : query.data?.items.find((t) => t.id === openId) ?? null;
  const anyFilter = activeFilters > 0 || f.search !== "";

  const periodField = (
    <Field label="Period">
      {(p) => (
        <Select {...p} value={f.period} onChange={(e) => setF({ period: e.target.value })}>
          <option value="all">All time</option>
          {(meta.data?.months ?? []).map((m) => <option key={m} value={m}>{monthLabel(m)}</option>)}
          {f.period !== "all" && !(meta.data?.months ?? []).includes(f.period) && <option value={f.period}>{monthLabel(f.period)}</option>}
        </Select>
      )}
    </Field>
  );
  const categoryField = (
    <Field label="Category">
      {(p) => (
        <Select {...p} value={f.category} onChange={(e) => setF({ category: e.target.value })}>
          <option value="">All categories</option>
          {categories.map((c) => <option key={c.id} value={c.name}>{c.name}</option>)}
        </Select>
      )}
    </Field>
  );
  const accountField = (
    <Field label="Account">
      {(p) => (
        <Select {...p} value={f.account} onChange={(e) => setF({ account: e.target.value })}>
          <option value="">All accounts</option>
          {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          <option value="none">Not linked</option>
        </Select>
      )}
    </Field>
  );
  const sourceField = (
    <Field label="Sorted by">
      {(p) => (
        <Select {...p} value={f.source} onChange={(e) => setF({ source: e.target.value })}>
          <option value="">Anything</option>
          {SOURCE_FILTERS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </Select>
      )}
    </Field>
  );
  const searchField = (
    <Field label="Search payee, narration or note" hideLabel={!desktop} hint={desktop ? undefined : undefined}>
      {(p) => (
        <TextInput
          {...p}
          ref={searchRef}
          type="search"
          value={text}
          placeholder={desktop ? "Search payee, narration or note  (press /)" : "Search payee, note…"}
          onChange={(e) => setText(e.target.value)}
        />
      )}
    </Field>
  );

  const clear = () => { setText(""); setF({ period: "all", category: "", account: "", search: "", source: "" }); };

  return (
    <>
      <PageHeader name="Ledger" title="Ledger" />
      {desktop ? (
        <div className={styles.filters} role="search">{periodField}{categoryField}{accountField}{sourceField}{searchField}</div>
      ) : (
        <div className={styles.phoneBar} role="search">
          {searchField}
          <Button variant="secondary" onClick={() => setFiltersOpen(true)} icon={<SlidersHorizontal size={16} strokeWidth={1.5} aria-hidden="true" />}>
            {activeFilters > 0 ? `Filters (${activeFilters})` : "Filters"}
          </Button>
        </div>
      )}
      <Dialog open={filtersOpen} onClose={() => setFiltersOpen(false)} title="Filters" sheet footer={
        <>
          <Button variant="tertiary" onClick={clear}>Clear filters</Button>
          <Button variant="primary" onClick={() => setFiltersOpen(false)}>Show entries</Button>
        </>
      }>
        <div className={styles.filterStack}>{periodField}{categoryField}{accountField}{sourceField}</div>
      </Dialog>

      <Loaded query={query} skeleton={<LedgerSkeleton />}>
        {(data) =>
          data.items.length === 0 ? (
            <EmptyState title="No transactions match." actions={anyFilter ? <Button variant="secondary" onClick={clear}>Clear filters</Button> : <LinkButton to="/add" variant="primary">Add an entry</LinkButton>}>
              {anyFilter ? "Change the filters, or add entries on the Add page." : "Nothing is recorded yet. Add an entry, paste a bank SMS, or import a statement."}
            </EmptyState>
          ) : (
            <>
              <p className={styles.countLine} aria-live="polite">
                {data.items.length}{data.truncated ? "+" : ""} {data.items.length === 1 ? "entry" : "entries"} {"·"} newest first
                {desktop && <> {"·"} changing a category teaches Rectifi that payee</>}
              </p>
              {data.truncated && (
                <div className={styles.truncated}>
                  <Callout tone="near">Showing only the newest {data.limit}. Pick a month or search to see older ones.</Callout>
                </div>
              )}
              {desktop ? (
                <LedgerTable items={data.items} categories={categories} accounts={accounts} onOpen={(t) => setOpenId(t.id)} />
              ) : (
                <LedgerList items={data.items} grouped onOpen={(t) => setOpenId(t.id)} />
              )}
            </>
          )
        }
      </Loaded>
      <EntrySheet txn={open} categories={categories} accounts={accounts} onClose={() => setOpenId(null)} />
    </>
  );
}
