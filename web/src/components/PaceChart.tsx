import { useEffect, useRef, useState, type PointerEvent } from "react";
import type { Pace } from "../api/types";
import { fmtShort } from "../lib/dates";
import { compactInr, formatInr } from "../lib/money";
import { Money } from "./Money";
import { Disclosure } from "./ui";
import styles from "./PaceChart.module.css";

const HEIGHT = 220;
const PAD = { top: 12, right: 76, bottom: 26, left: 44 };

// The numbers below are for pixel positions only. They never add or compare money; the words and tooltip use the server's strings.
const num = (m: string) => Number.parseFloat(m);

function niceStep(raw: number): number {
  const pow = 10 ** Math.floor(Math.log10(raw));
  const f = raw / pow;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * pow;
}

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

export function PaceChart({ pace, daysInMonth }: { pace: Pace; daysInMonth: number }) {
  const [wrap, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const days = pace.days;
  const last = days[days.length - 1];
  if (!last) return null;

  const w = Math.max(width, 280);
  const innerW = w - PAD.left - PAD.right;
  const innerH = HEIGHT - PAD.top - PAD.bottom;
  const top = Math.max(...days.map((d) => Math.max(num(d.cumulative), num(d.pace))), num(pace.total_limit) * 0.0001, 1);
  const step = niceStep(top / 3);
  const yMax = Math.ceil(top / step) * step;
  const ticks = [1, 2, 3].map((i) => (yMax / 3) * i).filter((t) => t > 0);
  const x = (i: number) => PAD.left + (daysInMonth <= 1 ? 0 : (i / (daysInMonth - 1)) * innerW);
  const y = (v: number) => PAD.top + innerH - (v / yMax) * innerH;
  const line = (pick: (d: Pace["days"][number]) => string) =>
    days.map((d, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(num(pick(d))).toFixed(1)}`).join(" ");

  const youEnd = y(num(last.cumulative));
  const paceEnd = y(num(last.pace));
  const youAbove = youEnd <= paceEnd;
  const endX = x(days.length - 1) + 8;

  function move(e: PointerEvent<SVGSVGElement>) {
    const box = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - box.left;
    const i = Math.round(((px - PAD.left) / innerW) * (daysInMonth - 1));
    setHover(Math.max(0, Math.min(days.length - 1, i)));
  }

  const point = hover !== null ? days[hover] : undefined;
  const summary = `Spending so far is ${formatInr(last.cumulative, { paise: false })}. An even pace through the budget would be ${formatInr(last.pace, { paise: false })} by ${fmtShort(last.day)}.`;

  return (
    <figure className={styles.figure}>
      <div ref={wrap}>
        <svg
          className={styles.svg}
          width={w}
          height={HEIGHT}
          viewBox={`0 0 ${w} ${HEIGHT}`}
          role="img"
          aria-label={summary}
          onPointerMove={move}
          onPointerDown={move}
          onPointerLeave={() => setHover(null)}
        >
          {ticks.map((t) => (
            <g key={t}>
              <line className={styles.grid} x1={PAD.left} x2={PAD.left + innerW} y1={y(t)} y2={y(t)} />
              <text className={styles.tickLabel} x={PAD.left - 8} y={y(t)} textAnchor="end" dominantBaseline="middle">{compactInr(t)}</text>
            </g>
          ))}
          <line className={styles.grid} x1={PAD.left} x2={PAD.left + innerW} y1={y(0)} y2={y(0)} />
          <text className={styles.axisLabel} x={PAD.left} y={HEIGHT - 6}>1</text>
          <text className={styles.axisLabel} x={PAD.left + innerW} y={HEIGHT - 6} textAnchor="end">{daysInMonth}</text>
          <path className={styles.pace} d={line((d) => d.pace)} />
          <path className={styles.you} d={line((d) => d.cumulative)} />
          <text className={`${styles.endLabel} ${styles.endYou}`} x={endX} y={youEnd + (youAbove ? -4 : 12)}>You</text>
          <text className={`${styles.endLabel} ${styles.endPace}`} x={endX} y={paceEnd + (youAbove ? 12 : -4)}>Budget pace</text>
          {hover !== null && point && (
            <g>
              <line className={styles.cross} x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={PAD.top + innerH} />
              <circle className={`${styles.dot} ${styles.dotPace}`} cx={x(hover)} cy={y(num(point.pace))} r={4} />
              <circle className={`${styles.dot} ${styles.dotYou}`} cx={x(hover)} cy={y(num(point.cumulative))} r={4} />
            </g>
          )}
        </svg>
        {hover !== null && point && (
          <div className={styles.tip} style={{ left: Math.max(76, Math.min(w - 76, x(hover))) }} role="presentation">
            <p className={styles.when}>{fmtShort(point.day)}</p>
            <p><span>You</span><Money value={point.cumulative} paise={false} /></p>
            <p><span>Budget pace</span><Money value={point.pace} paise={false} /></p>
          </div>
        )}
      </div>
      <figcaption className={styles.caption}>{summary}</figcaption>
      <Disclosure summary="Show as table">
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead><tr><th scope="col">Day</th><th scope="col">Spent</th><th scope="col">Total so far</th><th scope="col">Budget pace</th></tr></thead>
            <tbody>
              {days.map((d) => (
                <tr key={d.day}>
                  <td>{fmtShort(d.day)}</td>
                  <td><Money value={d.spent} /></td>
                  <td><Money value={d.cumulative} /></td>
                  <td><Money value={d.pace} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Disclosure>
    </figure>
  );
}
