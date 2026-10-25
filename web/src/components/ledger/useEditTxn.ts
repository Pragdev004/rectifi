import { useQueryClient } from "@tanstack/react-query";
import { patch } from "../../api/client";
import type { Category, EditResult, Txn, TransactionList } from "../../api/types";
import { useToast } from "../Toast";

export interface EditBody {
  category?: string;
  note?: string | null;
  account_id?: number | null;
}

/** Send one change to the server and put the corrected row back into the open list, so the row never jumps. */
export function useEditTxn() {
  const client = useQueryClient();
  const toast = useToast();
  return async function edit(t: Txn, body: EditBody): Promise<Txn> {
    const result = await patch<EditResult>(`/transactions/${t.id}`, body);
    client.setQueriesData<TransactionList>({ queryKey: ["transactions"] }, (old) =>
      old ? { ...old, items: old.items.map((x) => (x.id === result.transaction.id ? result.transaction : x)) } : old,
    );
    for (const key of ["overview", "review", "insights", "budgets", "accounts"]) void client.invalidateQueries({ queryKey: [key] });
    if (result.learned_rule) toast(`Rectifi will file “${result.learned_rule}” under ${result.transaction.category} from now on.`, "note");
    return result.transaction;
  };
}

/** The categories that fit this entry: money out takes spending and transfer categories, money in takes income and transfer. */
export function categoriesFor(t: Txn, all: Category[]): Category[] {
  const kind = t.direction === "credit" ? "income" : "expense";
  return all.filter((c) => c.kind === kind || c.kind === "transfer" || c.id === t.category_id);
}

export const STATEMENT_NOTE = "This entry came from a bank statement, so it belongs to that statement's account. Undo the import to move it.";
