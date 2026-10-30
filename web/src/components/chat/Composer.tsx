import { ArrowUp, Square } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { AnswerStyle, Chip, SendOptions } from "../../api/chat";
import { Button, Checkbox, Select } from "../forms";
import { MAX_TEXT } from "./helpers";
import c from "./chat.module.css";

const CHIPS: { value: Exclude<Chip, "">; label: string; hint: string }[] = [
  { value: "agent", label: "Do", hint: "Record or look up your own entries" },
  { value: "knowledge", label: "Look up", hint: "Answer from the money guides" },
  { value: "chart", label: "Chart", hint: "Draw a picture of your spending" },
];
const STYLES: { value: AnswerStyle; label: string }[] = [
  { value: "auto", label: "Auto" }, { value: "rag", label: "Plain" }, { value: "crag", label: "CRAG" },
  { value: "self_rag", label: "Self-RAG" }, { value: "full", label: "Full" },
];

export function Composer({ busy, stopping, onSend, onStop }: {
  busy: boolean;
  stopping: boolean;
  onSend: (text: string, options: SendOptions) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const [chip, setChip] = useState<Chip>("");
  const [style, setStyle] = useState<AnswerStyle>("auto");
  const [review, setReview] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const grow = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 6 * 24 + 22)}px`;
  }, []);
  useEffect(grow, [text, grow]);

  function submit() {
    const t = text.trim();
    if (!t || busy) return;
    onSend(t, { route: chip, mode: style, review: chip === "chart" && review });
    setText("");
    setChip("");
  }

  return (
    <form className={c.composer} onSubmit={(e) => { e.preventDefault(); submit(); }} aria-label="Message Rectifi">
      <div className={c.inputRow}>
        <label htmlFor="ask-input" className="sr-only">Message Rectifi</label>
        <textarea
          id="ask-input" ref={ref} className={c.input} rows={1} maxLength={MAX_TEXT} value={text} readOnly={busy} aria-busy={busy || undefined}
          placeholder="Spent, received, paste an SMS, or ask a question…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }}
        />
        {busy ? (
          <Button variant="secondary" className={c.send} loading={stopping} loadingLabel="Stopping…" onClick={onStop} icon={<Square size={14} strokeWidth={2} fill="currentColor" aria-hidden="true" />}>Stop</Button>
        ) : (
          <Button type="submit" variant="primary" disabled={text.trim() === ""} aria-label="Send" className={c.send}><ArrowUp size={20} strokeWidth={2} aria-hidden="true" /></Button>
        )}
      </div>
      <div className={c.options}>
        {CHIPS.map((o) => (
          <button key={o.value} type="button" className={c.chip} aria-pressed={chip === o.value} title={o.hint} disabled={busy} onClick={() => setChip(chip === o.value ? "" : o.value)}>{o.label}</button>
        ))}
        {chip === "chart" && <Checkbox label="Let me read the code before it runs" checked={review} onChange={setReview} disabled={busy} />}
        <label className={c.style}>
          <span>Answer style</span>
          <Select value={style} disabled={busy} onChange={(e) => setStyle(e.target.value as AnswerStyle)}>
            {STYLES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </Select>
        </label>
      </div>
    </form>
  );
}
