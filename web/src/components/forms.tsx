import { ChevronDown } from "lucide-react";
import { Link } from "wouter";
import {
  useId, useRef, useState, type AnchorHTMLAttributes, type ButtonHTMLAttributes, type InputHTMLAttributes, type KeyboardEvent, type ReactNode, type Ref,
  type SelectHTMLAttributes, type TextareaHTMLAttributes,
} from "react";
import { cx } from "../lib/cx";
import styles from "./forms.module.css";

type Variant = "primary" | "secondary" | "tertiary" | "danger" | "dangerQuiet";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  loading?: boolean;
  loadingLabel?: string;
  icon?: ReactNode;
  block?: boolean;
}

export function Button({ variant = "secondary", loading, loadingLabel, icon, block, className, children, onClick, type = "button", ...rest }: ButtonProps) {
  const disabled = Boolean(loading || rest.disabled);
  const { disabled: _drop, ...props } = rest;
  void _drop;
  return (
    <button
      {...props}
      type={type}
      className={cx(styles.button, styles[variant], block && styles.block, className)}
      aria-disabled={disabled || undefined}
      aria-busy={loading || undefined}
      onClick={(e) => {
        if (disabled) {
          e.preventDefault();
          return;
        }
        onClick?.(e);
      }}
    >
      {icon}
      {loading && loadingLabel ? loadingLabel : children}
    </button>
  );
}

interface LinkButtonProps extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> {
  to: string;
  variant?: Variant;
  icon?: ReactNode;
  block?: boolean;
}
export function LinkButton({ to, variant = "secondary", icon, block, className, children, ...rest }: LinkButtonProps) {
  return (
    <Link {...rest} href={to} className={cx(styles.button, styles[variant], block && styles.block, className)}>
      {icon}
      {children}
    </Link>
  );
}

export function IconButton({ label, children, className, type = "button", ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button {...rest} type={type} aria-label={label} className={cx(styles.iconButton, className)}>
      {children}
    </button>
  );
}

// ── fields ──

export interface FieldControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

export function Field({
  label, hint, error, hideLabel, className, children,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  hideLabel?: boolean;
  className?: string;
  children: (props: FieldControlProps) => ReactNode;
}) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cx(styles.field, className)}>
      <label htmlFor={id} className={hideLabel ? "sr-only" : styles.label}>{label}</label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {hint && <p id={hintId} className={styles.hint}>{hint}</p>}
      {error && <p id={errorId} className={styles.error} role="alert">{error}</p>}
    </div>
  );
}

export function TextInput({ className, ...rest }: InputHTMLAttributes<HTMLInputElement> & { ref?: Ref<HTMLInputElement> }) {
  return <input {...rest} className={cx(styles.input, className)} />;
}

export function TextArea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...rest} className={cx(styles.textarea, className)} />;
}

export function AmountInput({
  big, className, ...rest
}: Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "inputMode"> & { big?: boolean; ref?: Ref<HTMLInputElement> }) {
  return (
    <div className={cx(styles.amount, big && styles.big, className)}>
      <span className={styles.prefix} aria-hidden="true">{"₹"}</span>
      <input {...rest} type="text" inputMode="decimal" autoComplete="off" autoCapitalize="off" spellCheck={false} className={styles.input} />
    </div>
  );
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className={cx(styles.selectWrap, className)}>
      <select {...rest} className={styles.select}>{children}</select>
      <ChevronDown className={styles.caret} size={16} strokeWidth={1.5} aria-hidden="true" />
    </div>
  );
}

export function SegmentedControl<T extends string>({
  legend, options, value, onChange, name, full, disabled,
}: {
  legend: string;
  options: { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  name: string;
  full?: boolean;
  disabled?: boolean;
}) {
  return (
    <fieldset className={cx(styles.segmented, full && styles.full)} disabled={disabled}>
      <legend className="sr-only">{legend}</legend>
      {options.map((o) => (
        <label key={o.value} className={styles.segment}>
          <input type="radio" name={name} value={o.value} checked={value === o.value} onChange={() => onChange(o.value)} />
          <span>{o.label}</span>
        </label>
      ))}
    </fieldset>
  );
}

export function Checkbox({ label, checked, onChange, disabled }: { label: ReactNode; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className={styles.check}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

// ── tabs (ARIA tablist: arrow keys move between tabs) ──

export const tabId = (group: string, id: string) => `${group}-tab-${id}`;
export const panelId = (group: string, id: string) => `${group}-panel-${id}`;

export function Tabs<T extends string>({
  group, label, tabs, value, onChange,
}: {
  group: string;
  label: string;
  tabs: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    const i = tabs.findIndex((t) => t.id === value);
    let next = -1;
    if (e.key === "ArrowRight") next = (i + 1) % tabs.length;
    else if (e.key === "ArrowLeft") next = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabs.length - 1;
    if (next < 0) return;
    e.preventDefault();
    const target = tabs[next];
    if (!target) return;
    onChange(target.id);
    refs.current[target.id]?.focus();
  }
  return (
    <div className={styles.tabs} role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t) => (
        <button
          key={t.id}
          ref={(el) => { refs.current[t.id] = el; }}
          type="button"
          role="tab"
          id={tabId(group, t.id)}
          aria-selected={value === t.id}
          aria-controls={panelId(group, t.id)}
          tabIndex={value === t.id ? 0 : -1}
          className={styles.tab}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

// ── file drop: click or drop; shows the chosen file's name and size ──

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function FileDrop({
  file, onFile, accept, busy, label,
}: {
  file: File | null;
  onFile: (f: File) => void;
  accept: string;
  busy?: boolean;
  label: string;
}) {
  const [over, setOver] = useState(false);
  return (
    <label
      className={cx(styles.drop, over && styles.over, busy && styles.busy)}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        const f = e.dataTransfer.files[0];
        if (f && !busy) onFile(f);
      }}
    >
      <input
        type="file"
        accept={accept}
        aria-label={label}
        disabled={busy}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onFile(f);
          e.target.value = ""; // choosing the same file again must still fire
        }}
      />
      <span className={styles.big}>{file ? file.name : "Choose a statement file, or drop it here"}</span>
      <span className={styles.small}>{file ? formatBytes(file.size) : "CSV, Excel or PDF, up to 10 MB"}</span>
    </label>
  );
}
