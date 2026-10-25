import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { del } from "../../api/client";
import { refreshBooks } from "../../api/queries";
import type { Txn } from "../../api/types";
import { Button } from "../forms";
import { Money } from "../Money";
import { useToast } from "../Toast";
import { Callout } from "../ui";
import styles from "./add.module.css";

export interface ReceiptState {
  status: "added" | "duplicate";
  txn: Txn;
  at: number;
}

/** "Saved ₹250.00 · Swiggy · Food & Dining", with Undo for 10 seconds. */
export function Receipt({ receipt }: { receipt: ReceiptState }) {
  const client = useQueryClient();
  const toast = useToast();
  const [canUndo, setCanUndo] = useState(receipt.status === "added");
  const [undone, setUndone] = useState(false);
  useEffect(() => {
    setCanUndo(receipt.status === "added");
    setUndone(false);
    if (receipt.status !== "added") return;
    const t = window.setTimeout(() => setCanUndo(false), 10_000);
    return () => window.clearTimeout(t);
  }, [receipt]);

  const undo = useMutation({
    mutationFn: () => del<{ deleted: boolean }>(`/transactions/${receipt.txn.id}`),
    onSuccess: () => {
      setUndone(true);
      setCanUndo(false);
      toast("Entry removed.");
      refreshBooks(client);
    },
    onError: (e) => toast(e instanceof Error ? e.message : "Could not undo that.", "over"),
  });

  if (receipt.status === "duplicate") {
    return <Callout tone="note" role="status">Already recorded (same UPI reference). Nothing was added.</Callout>;
  }
  if (undone) return <Callout tone="note" role="status">Removed. That entry is gone.</Callout>;
  const t = receipt.txn;
  return (
    <div className={styles.receipt} role="status" data-testid="receipt">
      <span>
        <span className={styles.meta}>Saved</span>
        <strong><Money value={t.amount} /></strong> {"·"} {t.payee ?? "—"} {"·"} {t.category ?? "Uncategorised"}
      </span>
      {canUndo && <Button variant="tertiary" loading={undo.isPending} loadingLabel="Removing…" onClick={() => undo.mutate()}>Undo</Button>}
    </div>
  );
}
