// Dates are plain "YYYY-MM-DD" text from the server. They are read as calendar days (no time zone), so a day never slips.

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

function parts(iso: string): [number, number, number] {
  const m = DAY.exec(iso);
  if (!m) return [1970, 1, 1];
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

function utc(iso: string): Date {
  const [y, mo, d] = parts(iso);
  return new Date(Date.UTC(y, mo - 1, d));
}

const fmt = (options: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("en-IN", { ...options, timeZone: "UTC" });
const F_FULL = fmt({ day: "numeric", month: "short", year: "numeric" });
const F_SHORT = fmt({ day: "numeric", month: "short" });
const F_WEEKDAY = fmt({ weekday: "long" });
const F_LONG = fmt({ weekday: "long", day: "numeric", month: "long" });
const F_HEAD_WD = fmt({ weekday: "short" });
const F_HEAD_DM = fmt({ day: "numeric", month: "short" });
const F_MONTH_YEAR = fmt({ month: "long", year: "numeric" });
const F_MONTH = fmt({ month: "long" });

/** "21 Oct 2026" */
export const fmtDay = (iso: string) => F_FULL.format(utc(iso)).replace(/Sept\b/, "Sep");
/** "21 Oct" */
export const fmtShort = (iso: string) => F_SHORT.format(utc(iso)).replace(/Sept\b/, "Sep");
/** "Saturday" */
export const weekdayName = (iso: string) => F_WEEKDAY.format(utc(iso));
/** "Saturday 24 October" */
export const fmtLong = (iso: string) => F_LONG.format(utc(iso));
/** "WED 21 OCT" (the passbook day header) */
export const dayHeader = (iso: string) => `${F_HEAD_WD.format(utc(iso))} ${F_HEAD_DM.format(utc(iso))}`.toUpperCase().replace("SEPT", "SEP");

/** "2026-10" -> "October 2026" */
export const monthLabel = (ym: string) => F_MONTH_YEAR.format(utc(`${ym}-01`));
/** "2026-09" -> "September" */
export const monthName = (ym: string) => F_MONTH.format(utc(`${ym}-01`));

/** Monday ISO date -> "19–25 Oct", or "28 Sep–4 Oct" across months. */
export function weekLabel(monday: string): string {
  const end = shiftDay(monday, 6);
  const [, m1] = parts(monday);
  const [, m2, d2] = parts(end);
  const [, , d1] = parts(monday);
  if (m1 === m2) return `${d1}–${d2} ${fmtShort(end).split(" ")[1]}`;
  return `${fmtShort(monday)}–${fmtShort(end)}`;
}

/** Calendar-day arithmetic for the date controls (never money). */
export function shiftDay(iso: string, days: number): string {
  const d = utc(iso);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export const monthOf = (iso: string) => iso.slice(0, 7);

/** "24 Oct, 14:02" in the viewer's own time zone, from an ISO date-time with an offset. */
export function fmtDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const day = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short" }).format(d).replace(/Sept\b/, "Sep");
  const time = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false }).format(d);
  return `${day}, ${time}`;
}

/** "3 min ago", "2 h ago", "5 days ago" */
export function timeAgo(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const s = Math.max(0, Math.round((now - then) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}
