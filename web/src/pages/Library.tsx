import { RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../api/client";
import { statusLine, useKnowledgeActions, useKnowledgeDocs, useKnowledgeStatus, type KnowledgeDoc, type KnowledgeStatus } from "../api/knowledge";
import { Button, IconButton } from "../components/forms";
import { ConfirmDialog } from "../components/Dialog";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { useToast } from "../components/Toast";
import { Callout, EmptyState, Eyebrow, Skeleton, Stamp } from "../components/ui";
import { formatBytes } from "../components/forms";
import styles from "./library.module.css";

function IndexSlip({ status }: { status: KnowledgeStatus }) {
  const { reindex } = useKnowledgeActions();
  const toast = useToast();
  const job = status.job;
  const running = job.state === "running";
  const share = job.total > 0 ? Math.min(100, Math.round((job.done / job.total) * 100)) : 0;
  return (
    <section className={styles.slip} aria-label="Index status" data-testid="index-status">
      <div>
        <Eyebrow>Index</Eyebrow>
        <p className={styles.statusLine}>{statusLine(status)}</p>
        <p className={styles.meta}>
          {status.chunks.guide ?? 0} from guides {"·"} {status.chunks.note ?? 0} from your notes {"·"} {status.chunks.txn ?? 0} from your entries {"·"} {status.chunks.upload ?? 0} from your files
        </p>
        {running && (
          <div role="status" aria-live="polite">
            <progress className={styles.progress} max={Math.max(job.total, 1)} value={Math.min(job.done, Math.max(job.total, 1))} aria-label="Indexing progress" />
            <p className={styles.meta}>Indexing{"…"} {job.total > 0 ? `${share}%` : ""}</p>
          </div>
        )}
      </div>
      <Button
        variant="secondary"
        icon={<RefreshCw size={16} strokeWidth={1.5} aria-hidden="true" />}
        loading={running || reindex.isPending}
        loadingLabel="Indexing…"
        onClick={() =>
          reindex.mutate(undefined, {
            onSuccess: (r) => toast(r.started ? "Indexing started." : "Indexing is already running."),
            onError: (e) => toast(e instanceof Error ? e.message : "Could not start indexing."),
          })
        }
      >
        Re-index
      </Button>
      {job.state === "failed" && job.error && <Callout tone="over" role="alert" lead="Indexing stopped.">{job.error}</Callout>}
      {status.other_model_chunks > 0 && (
        <Callout tone="near" lead="Some passages were made with another embedding model.">
          They are ignored until you re-index with {status.embed_model}.
        </Callout>
      )}
    </section>
  );
}

function DocRow({ doc, onRemove }: { doc: KnowledgeDoc; onRemove?: () => void }) {
  return (
    <li className={styles.row} data-testid={`doc-${doc.corpus}`}>
      <div className={styles.rowMain}>
        <p className={styles.rowTitle}>
          <Stamp tone={doc.builtin ? "accent" : "neutral"}>{doc.label}</Stamp>
          <span>{doc.title}</span>
        </p>
        <p className={styles.meta}>
          {doc.indexed ? `${doc.chunks} ${doc.chunks === 1 ? "passage" : "passages"}` : "not indexed yet"}
          {doc.builtin ? "" : ` · ${doc.source}`}
        </p>
      </div>
      {onRemove && <IconButton label={`Remove ${doc.title}`} onClick={onRemove}><Trash2 size={18} strokeWidth={1.5} aria-hidden="true" /></IconButton>}
    </li>
  );
}

function AddFile() {
  const { upload } = useKnowledgeActions();
  const toast = useToast();
  const [picked, setPicked] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);

  function send(file: File) {
    setPicked(file);
    setError(null);
    upload.mutate(file, {
      onSuccess: (r) => {
        setPicked(null);
        toast(r.indexed ? `Added “${r.doc.title}”.` : `Saved “${r.doc.title}”. It will be searchable after the next re-index.`);
      },
      onError: (e) => setError(e instanceof ApiError || e instanceof Error ? e.message : "That file was not added."),
    });
  }

  return (
    <div className={styles.add}>
      <label className={styles.drop} aria-busy={upload.isPending || undefined}>
        <input
          type="file"
          accept=".md,.txt,text/markdown,text/plain"
          aria-label="Add a note or file to the library"
          disabled={upload.isPending}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) send(f);
            e.target.value = "";
          }}
        />
        <span className={styles.dropBig}>{upload.isPending && picked ? `Adding ${picked.name}…` : "Choose a note to add"}</span>
        <span className={styles.meta}>.md or .txt, up to {formatBytes(300 * 1024)}. Bank statements go in Add, then Import.</span>
      </label>
      {error && <Callout tone="over" role="alert">{error}</Callout>}
    </div>
  );
}

export default function LibraryPage() {
  const docs = useKnowledgeDocs();
  const status = useKnowledgeStatus();
  const { remove } = useKnowledgeActions();
  const toast = useToast();
  const [removing, setRemoving] = useState<KnowledgeDoc | null>(null);

  return (
    <>
      <PageHeader name="Library" title="Library" sub="The guides and notes Rectifi can look things up in. The guides never contain ₹ amounts, so any amount in an answer comes from your own records." />
      <div className={styles.layout}>
        <div className={styles.col}>
          <Loaded query={status} skeleton={<Skeleton height={96} />}>{(s) => <IndexSlip status={s} />}</Loaded>
          <Loaded query={docs} skeleton={<Skeleton height={160} />}>
            {({ docs: all }) => {
              const guides = all.filter((d) => d.builtin);
              const mine = all.filter((d) => !d.builtin);
              return (
                <div className={styles.col}>
                  <section className={styles.section} aria-labelledby="guides-h">
                    <h2 id="guides-h" className={styles.heading}>Guides</h2>
                    {guides.length === 0 ? (
                      <EmptyState title="No guides found.">The guides ship with Rectifi. Re-install or check the rectifi/knowledge folder.</EmptyState>
                    ) : (
                      <ul className={styles.list}>{guides.map((d) => <DocRow key={d.source} doc={d} />)}</ul>
                    )}
                  </section>
                  <section className={styles.section} aria-labelledby="mine-h">
                    <h2 id="mine-h" className={styles.heading}>Your files</h2>
                    {mine.length === 0 ? (
                      <p className={styles.meta}>Nothing yet. Add a note about your flat rules, a plan or a rule you set for yourself, and Rectifi can use it.</p>
                    ) : (
                      <ul className={styles.list}>{mine.map((d) => <DocRow key={d.id ?? d.source} doc={d} onRemove={() => setRemoving(d)} />)}</ul>
                    )}
                    <AddFile />
                  </section>
                </div>
              );
            }}
          </Loaded>
        </div>
      </div>
      <ConfirmDialog
        open={removing !== null}
        title="Remove this file?"
        confirmLabel="Remove"
        busyLabel="Removing…"
        busy={remove.isPending}
        onClose={() => setRemoving(null)}
        onConfirm={() => {
          if (removing?.id == null) return;
          remove.mutate(removing.id, {
            onSuccess: () => { toast("File removed."); setRemoving(null); },
            onError: (e) => { toast(e instanceof Error ? e.message : "Could not remove that file."); setRemoving(null); },
          });
        }}
      >
        {removing && <>“{removing.title}” and its passages will be deleted from the library. Your original file is not touched.</>}
      </ConfirmDialog>
    </>
  );
}
