import { type LucideIcon, Check, ChevronRight, Copy, Info, TriangleAlert, OctagonAlert, CircleCheck } from "lucide-react";
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { cx } from "../lib/cx";
import styles from "./ui.module.css";
import { Button } from "./forms";

export type Tone = "ok" | "near" | "over" | "accent" | "neutral";

export function Eyebrow({ children, className, as: Tag = "p" }: { children: ReactNode; className?: string; as?: "p" | "h2" | "h3" | "span" }) {
  return <Tag className={cx(styles.eyebrow, className)}>{children}</Tag>;
}

export function Stamp({ tone = "neutral", children, pop, className }: { tone?: Tone; children: ReactNode; pop?: boolean; className?: string }) {
  return <span className={cx(styles.stamp, styles[tone], pop && styles.pop, className)}>{children}</span>;
}

const CALLOUT_ICON = { note: Info, near: TriangleAlert, over: OctagonAlert, ok: CircleCheck } as const;

export function Callout({
  tone = "note", lead, children, actions, role, className, icon,
}: {
  icon?: LucideIcon;
  tone?: keyof typeof CALLOUT_ICON;
  lead?: string;
  children?: ReactNode;
  actions?: ReactNode;
  role?: "alert" | "status";
  className?: string;
}) {
  const Icon = icon ?? CALLOUT_ICON[tone];
  return (
    <div className={cx(styles.callout, styles[tone], className)} role={role}>
      <Icon className={styles.calloutIcon} size={18} strokeWidth={1.5} aria-hidden="true" />
      <div className={styles.calloutBody}>
        {lead && <p className={styles.calloutLead}>{lead}</p>}
        {children && <div>{children}</div>}
        {actions && <div className={styles.calloutActions}>{actions}</div>}
      </div>
    </div>
  );
}

export function EmptyState({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className={styles.empty}>
      <h2>{title}</h2>
      {children && <p>{children}</p>}
      {actions && <div className={styles.actions}>{actions}</div>}
    </div>
  );
}

export function Skeleton({ width, height = 16, style }: { width?: number | string; height?: number | string; style?: CSSProperties }) {
  return <span className={styles.skeleton} style={{ width: width ?? "100%", height, ...style }} aria-hidden="true" />;
}

/** True once `waiting` has lasted `ms`. A skeleton that flashes for 100 ms is worse than none. */
export function useDelayed(waiting: boolean, ms = 200): boolean {
  const [late, setLate] = useState(false);
  useEffect(() => {
    if (!waiting) {
      setLate(false);
      return;
    }
    const t = window.setTimeout(() => setLate(true), ms);
    return () => window.clearTimeout(t);
  }, [waiting, ms]);
  return waiting && late;
}

export function Disclosure({ summary, children, defaultOpen = false }: { summary: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  return (
    <details className={styles.disclosure} open={defaultOpen || undefined}>
      <summary>
        <ChevronRight className={styles.chev} size={16} strokeWidth={1.5} aria-hidden="true" />
        <span>{summary}</span>
      </summary>
      <div className={styles.disclosureBody}>{children}</div>
    </details>
  );
}

/** A value to copy (a URL, a command, a header). The "Copied" word appears beside the icon, never colour alone. */
export function CodeValue({ value, label, wrap }: { value: string; label: string; wrap?: boolean }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    setCopied(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 2000);
  }
  return (
    <div className={cx(styles.code, wrap && styles.wrap)}>
      <code tabIndex={0} aria-label={label}>{value}</code>
      <button type="button" className={styles.copy} onClick={copy} aria-label={`Copy ${label}`}>
        {copied ? <Check size={16} strokeWidth={1.5} aria-hidden="true" /> : <Copy size={16} strokeWidth={1.5} aria-hidden="true" />}
        <span aria-live="polite">{copied ? "Copied" : "Copy"}</span>
      </button>
    </div>
  );
}

export function InlineStatus({ kind, children }: { kind?: "saving" | "saved" | "error"; children: ReactNode }) {
  return <span className={cx(styles.status, kind === "saved" && styles.saved, kind === "error" && styles.error)} role={kind === "error" ? "alert" : "status"}>{children}</span>;
}

export function Section({ eyebrow, action, children, id }: { eyebrow?: ReactNode; action?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section className={styles.section} aria-labelledby={id}>
      {(eyebrow || action) && (
        <div className={styles.sectionHead}>
          {eyebrow && <Eyebrow as="h2" className={styles.eyebrow}>{<span id={id}>{eyebrow}</span>}</Eyebrow>}
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

export function ErrorBlock({ message, onRetry, title = "That did not load" }: { message: string; onRetry?: () => void; title?: string }) {
  return (
    <div className={styles.errorBlock} role="alert">
      <Callout tone="over" lead={title}>{message}</Callout>
      {onRetry && <Button variant="secondary" onClick={onRetry}>Try again</Button>}
    </div>
  );
}
