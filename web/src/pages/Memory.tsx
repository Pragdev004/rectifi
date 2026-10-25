import { useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2 } from "lucide-react";
import { useState } from "react";
import { useLocation } from "wouter";
import { addMemory, deleteMemory, editMemory, forgetEverything, memoryKey, removeRule, useMemory, type MemoryItem, type MemoryKind } from "../api/memory";
import { ConfirmDialog } from "../components/Dialog";
import { Button, Field, Select, TextInput } from "../components/forms";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { useToast } from "../components/Toast";
import { Callout, EmptyState, Section, Skeleton, Stamp } from "../components/ui";
import { fmtDateTime } from "../lib/dates";
import m from "./memory.module.css";

const KINDS: { value: MemoryKind; label: string }[] = [
  { value: "preference", label: "Preference" },
  { value: "fact", label: "Fact" },
  { value: "correction", label: "Correction" },
];
const THREAD_KEY = "rectifi.chat.thread";   // the Ask page reads this to know which conversation to open

function Item({ item, onChanged }: { item: MemoryItem; onChanged: () => Promise<void> }) {
  const toast = useToast();
  const [, navigate] = useLocation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.text);
  const [busy, setBusy] = useState(false);

  async function save() {
    const text = draft.trim();
    if (!text || text === item.text) { setEditing(false); return; }
    setBusy(true);
    try { await editMemory(item.id, { text }); await onChanged(); setEditing(false); } catch (e) { toast(e instanceof Error ? e.message : "Could not save.", "over"); } finally { setBusy(false); }
  }
  async function remove() {
    setBusy(true);
    try { await deleteMemory(item.id); await onChanged(); toast("Forgotten.", "note"); } catch (e) { toast(e instanceof Error ? e.message : "Could not delete.", "over"); setBusy(false); }
  }
  function openSource() {
    try { window.localStorage.setItem(THREAD_KEY, item.thread_id ?? ""); } catch { /* storage blocked: Ask just opens as usual */ }
    navigate("/ask");
  }

  return (
    <li className={m.item} data-testid="memory-item">
      <div className={m.itemHead}>
        <Stamp tone={item.kind === "preference" ? "accent" : "neutral"}>{item.kind}</Stamp>
        {item.created_at && <time className={m.when} dateTime={item.created_at}>{fmtDateTime(item.created_at)}</time>}
      </div>
      {editing ? (
        <form className={m.edit} onSubmit={(e) => { e.preventDefault(); void save(); }}>
          <label className="sr-only" htmlFor={`mem-${item.id}`}>What to remember</label>
          <TextInput id={`mem-${item.id}`} value={draft} maxLength={500} autoFocus onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") setEditing(false); }} />
          <div className={m.actions}>
            <Button type="submit" variant="primary" loading={busy} loadingLabel="Saving…">Save</Button>
            <Button onClick={() => { setDraft(item.text); setEditing(false); }}>Cancel</Button>
          </div>
        </form>
      ) : (
        <>
          <p className={m.text}>{item.text}</p>
          <div className={m.actions}>
            <Button variant="tertiary" icon={<Pencil size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => { setDraft(item.text); setEditing(true); }}>Edit</Button>
            <Button variant="dangerQuiet" icon={<Trash2 size={16} strokeWidth={1.5} aria-hidden="true" />} loading={busy} loadingLabel="Deleting…" onClick={() => void remove()}>Delete</Button>
            {item.thread_id && <Button variant="tertiary" onClick={openSource}>Open the conversation</Button>}
          </div>
        </>
      )}
    </li>
  );
}

export default function MemoryPage() {
  const client = useQueryClient();
  const toast = useToast();
  const query = useMemory();
  const [kind, setKind] = useState<MemoryKind>("preference");
  const [text, setText] = useState("");
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [wiping, setWiping] = useState(false);
  const refresh = () => client.invalidateQueries({ queryKey: memoryKey });

  async function add() {
    if (!text.trim()) return;
    setAdding(true);
    setError(null);
    try { await addMemory(kind, text.trim()); setText(""); await refresh(); } catch (e) { setError(e instanceof Error ? e.message : "Could not save that."); } finally { setAdding(false); }
  }
  async function wipe() {
    setWiping(true);
    try { const r = await forgetEverything(); await refresh(); setConfirm(false); toast(r.deleted === 1 ? "Forgot 1 thing." : `Forgot ${r.deleted} things.`, "note"); } catch (e) { toast(e instanceof Error ? e.message : "Could not forget.", "over"); } finally { setWiping(false); }
  }
  async function dropRule(id: number) {
    try { await removeRule(id); await refresh(); toast("Rule removed.", "note"); } catch (e) { toast(e instanceof Error ? e.message : "Could not remove it.", "over"); }
  }

  return (
    <>
      <PageHeader name="Memory" title="What Rectifi remembers about you" sub="You decide what is kept. Everything here lives on this laptop." />
      <Loaded query={query} skeleton={<div aria-hidden="true"><Skeleton height={64} /><Skeleton height={64} style={{ marginTop: 12 }} /></div>}>
        {(data) => (
          <div className={m.wrap}>
            <Callout tone="note">Memories never supply amounts. Figures always come from your records.</Callout>

            <Section eyebrow="Add something" id="mem-add">
              <form className={m.add} onSubmit={(e) => { e.preventDefault(); void add(); }}>
                <div className={m.addRow}>
                  <Field label="Kind">{(p) => (
                    <Select {...p} value={kind} onChange={(e) => setKind(e.target.value as MemoryKind)}>
                      {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
                    </Select>
                  )}</Field>
                  <Field label="What should Rectifi remember?" error={error ?? undefined}>{(p) => (
                    <TextInput {...p} value={text} maxLength={500} placeholder="I prefer short answers" onChange={(e) => setText(e.target.value)} />
                  )}</Field>
                  <Button type="submit" variant="primary" loading={adding} loadingLabel="Saving…" disabled={!text.trim()}>Remember</Button>
                </div>
                <p className={m.muted}>You can also say {"“"}remember that…{"”"} in Ask. Rectifi asks before it saves.</p>
              </form>
            </Section>

            <Section
              eyebrow={`Remembered · ${data.memories.length}`}
              id="mem-list"
              action={data.memories.length > 0 ? <Button variant="dangerQuiet" onClick={() => setConfirm(true)}>Forget everything</Button> : undefined}
            >
              {data.memories.length === 0 ? (
                <EmptyState title="Nothing is remembered yet.">Say {"“"}remember that I prefer short answers{"”"} in Ask, or add something above.</EmptyState>
              ) : (
                <ul className={m.list}>{data.memories.map((it) => <Item key={it.id} item={it} onChanged={refresh} />)}</ul>
              )}
            </Section>

            <Section eyebrow="Category rules you taught Rectifi" id="mem-rules">
              {data.rules.length === 0 ? (
                <p className={m.muted}>When you correct a category and ask Rectifi to remember it, the rule is listed here.</p>
              ) : (
                <ul className={m.list}>
                  {data.rules.map((r) => (
                    <li key={r.id} className={m.rule}>
                      <span><code>{r.pattern}</code> goes to <strong>{r.category}</strong></span>
                      <Button variant="dangerQuiet" aria-label={`Remove the rule for ${r.pattern}`} onClick={() => void dropRule(r.id)}>Remove</Button>
                    </li>
                  ))}
                </ul>
              )}
            </Section>
          </div>
        )}
      </Loaded>
      <ConfirmDialog open={confirm} title="Forget everything?" confirmLabel="Forget everything" busyLabel="Forgetting…" busy={wiping} onConfirm={() => void wipe()} onClose={() => setConfirm(false)}>
        <p>Rectifi will forget every preference, fact and correction listed here. Your entries and the category rules you taught stay.</p>
      </ConfirmDialog>
    </>
  );
}
