import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { cx } from "../lib/cx";
import styles from "./Toast.module.css";

type ToastTone = "ok" | "over" | "note";
interface ToastItem {
  id: number;
  message: string;
  tone: ToastTone;
}
type Notify = (message: string, tone?: ToastTone) => void;

const ToastContext = createContext<Notify>(() => {});
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const next = useRef(1);
  const notify = useCallback<Notify>((message, tone = "ok") => {
    const id = next.current++;
    setItems((cur) => [...cur.slice(-2), { id, message, tone }]);
    window.setTimeout(() => setItems((cur) => cur.filter((t) => t.id !== id)), 4500);
  }, []);
  const value = useMemo(() => notify, [notify]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className={styles.region} role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={cx(styles.toast, t.tone === "over" && styles.over, t.tone === "note" && styles.note)}>{t.message}</div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
