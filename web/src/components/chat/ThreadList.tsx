import { EllipsisVertical, Pencil, Plus, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { Thread } from "../../api/chat";
import { cx } from "../../lib/cx";
import { Button, IconButton, TextInput } from "../forms";
import { ConfirmDialog } from "../Dialog";
import { Eyebrow, Skeleton } from "../ui";
import { groupThreads, msgTime } from "./helpers";
import c from "./chat.module.css";

interface Props {
  threads: Thread[] | undefined;
  loading: boolean;
  activeId: string | null;
  query: string;
  onQuery: (q: string) => void;
  onOpen: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  /** Called after a choice on a phone, where the list is a sheet that should close. */
  onChosen?: () => void;
}

function Row({ thread, active, onOpen, onRename, onAskDelete }: {
  thread: Thread; active: boolean; onOpen: () => void; onRename: (title: string) => Promise<void>; onAskDelete: () => void;
}) {
  const [menu, setMenu] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(thread.title);
  const [saving, setSaving] = useState(false);
  const box = useRef<HTMLLIElement>(null);

  useEffect(() => {
    if (!menu) return;
    const close = (e: Event) => { if (box.current && !box.current.contains(e.target as Node)) setMenu(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(false); };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", esc); };
  }, [menu]);

  async function save() {
    const title = draft.trim();
    if (!title || title === thread.title) { setEditing(false); return; }
    setSaving(true);
    try { await onRename(title); setEditing(false); } finally { setSaving(false); }
  }

  if (editing) {
    return (
      <li className={c.row} data-active={active}>
        <form className={c.renameRow} onSubmit={(e) => { e.preventDefault(); void save(); }}>
          <label className="sr-only" htmlFor={`rename-${thread.thread_id}`}>Conversation name</label>
          <TextInput
            id={`rename-${thread.thread_id}`} value={draft} maxLength={80} autoFocus
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); setEditing(false); } }}
          />
          <div className={c.renameActions}>
            <Button type="submit" variant="primary" loading={saving} loadingLabel="Saving…">Save</Button>
            <Button onClick={() => setEditing(false)}>Cancel</Button>
          </div>
        </form>
      </li>
    );
  }

  return (
    <li className={c.row} data-active={active} ref={box}>
      <button type="button" className={c.open} onClick={onOpen} aria-current={active ? "true" : undefined}>
        <span className={c.title}>{thread.title}</span>
        {thread.preview && <span className={c.preview}>{thread.preview}</span>}
        <span className={c.when}>{msgTime(thread.updated_at)}</span>
      </button>
      <IconButton label={`More for ${thread.title}`} className={c.more} aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
        <EllipsisVertical size={18} strokeWidth={1.5} aria-hidden="true" />
      </IconButton>
      {menu && (
        <div className={c.menu} role="menu" aria-label={`Actions for ${thread.title}`}>
          <button type="button" role="menuitem" className={c.menuItem} autoFocus onClick={() => { setMenu(false); setDraft(thread.title); setEditing(true); }}>
            <Pencil size={16} strokeWidth={1.5} aria-hidden="true" /> Rename
          </button>
          <button type="button" role="menuitem" className={cx(c.menuItem, c.danger)} onClick={() => { setMenu(false); onAskDelete(); }}>
            <Trash2 size={16} strokeWidth={1.5} aria-hidden="true" /> Delete
          </button>
        </div>
      )}
    </li>
  );
}

export function ThreadList({ threads, loading, activeId, query, onQuery, onOpen, onNew, onRename, onDelete, onChosen }: Props) {
  const [doomed, setDoomed] = useState<Thread | null>(null);
  const [deleting, setDeleting] = useState(false);
  const groups = threads ? groupThreads(threads) : [];

  async function confirmDelete() {
    if (!doomed) return;
    setDeleting(true);
    try { await onDelete(doomed.thread_id); setDoomed(null); } finally { setDeleting(false); }
  }

  return (
    <nav aria-label="Conversations">
      <div className={c.listHead}>
        <Button variant="secondary" block icon={<Plus size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => { onNew(); onChosen?.(); }}>New conversation</Button>
        <label className="sr-only" htmlFor="chat-search">Search conversations</label>
        <TextInput id="chat-search" type="search" value={query} placeholder="Search conversations" autoComplete="off" onChange={(e) => onQuery(e.target.value)} />
      </div>
      {loading && !threads && <div aria-hidden="true"><Skeleton height={56} /><Skeleton height={56} style={{ marginTop: 8 }} /></div>}
      {threads && threads.length === 0 && (
        <p className={c.emptyList} role="status">{query ? "No conversation matches that." : "Your conversations will be listed here."}</p>
      )}
      {groups.map(({ group, threads: rows }) => (
        <section key={group} className={c.group} aria-label={group}>
          <Eyebrow>{group}</Eyebrow>
          <ul className={c.rows}>
            {rows.map((t) => (
              <Row
                key={t.thread_id} thread={t} active={t.thread_id === activeId}
                onOpen={() => { onOpen(t.thread_id); onChosen?.(); }}
                onRename={(title) => onRename(t.thread_id, title)}
                onAskDelete={() => setDoomed(t)}
              />
            ))}
          </ul>
        </section>
      ))}
      <ConfirmDialog
        open={doomed !== null} title="Delete this conversation?" confirmLabel="Delete" busyLabel="Deleting…" busy={deleting}
        onConfirm={() => void confirmDelete()} onClose={() => setDoomed(null)}
      >
        <p>{doomed ? `“${doomed.title}” and everything said in it will be removed from this laptop.` : ""}</p>
        <p className={c.hint}>Entries Rectifi saved for you during the chat stay in your ledger.</p>
      </ConfirmDialog>
    </nav>
  );
}
