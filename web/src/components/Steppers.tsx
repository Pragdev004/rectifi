import { ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { monthLabel, weekLabel } from "../lib/dates";
import { IconButton } from "./forms";
import styles from "./Steppers.module.css";

function Stepper({
  kind, value, options, labelFor, onChange,
}: {
  kind: "month" | "week";
  value: string;
  options: string[]; // newest first
  labelFor: (v: string) => string;
  onChange: (v: string) => void;
}) {
  const list = options.includes(value) ? options : [value, ...options];
  const i = list.indexOf(value);
  const older = list[i + 1];
  const newer = list[i - 1];
  return (
    <div className={styles.stepper} role="group" aria-label={`Choose a ${kind}`}>
      <IconButton label={`Previous ${kind}`} disabled={older === undefined} onClick={() => older !== undefined && onChange(older)}>
        <ChevronLeft size={20} strokeWidth={1.5} aria-hidden="true" />
      </IconButton>
      <span className={styles.pick}>
        <select aria-label={kind === "month" ? "Month" : "Week"} value={value} onChange={(e) => onChange(e.target.value)}>
          {list.map((o) => (
            <option key={o} value={o}>{labelFor(o)}</option>
          ))}
        </select>
        <ChevronDown className={styles.caret} size={14} strokeWidth={1.5} aria-hidden="true" />
      </span>
      <IconButton label={`Next ${kind}`} disabled={newer === undefined} onClick={() => newer !== undefined && onChange(newer)}>
        <ChevronRight size={20} strokeWidth={1.5} aria-hidden="true" />
      </IconButton>
    </div>
  );
}

export function MonthStepper({ value, months, onChange }: { value: string; months: string[]; onChange: (v: string) => void }) {
  return <Stepper kind="month" value={value} options={months} labelFor={monthLabel} onChange={onChange} />;
}

export function WeekStepper({ value, weeks, onChange }: { value: string; weeks: string[]; onChange: (v: string) => void }) {
  const label = (v: string) => {
    const span = weekLabel(v);
    if (v === weeks[0]) return `This week · ${span}`;
    if (v === weeks[1]) return `Last week · ${span}`;
    return span;
  };
  return <Stepper kind="week" value={value} options={weeks} labelFor={label} onChange={onChange} />;
}
