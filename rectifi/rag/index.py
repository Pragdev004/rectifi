"""Incremental indexing: the guides, your notes, your entries and the files you added become chunks with embeddings.

A chunk is re-embedded only when its text changed (content_hash). Unchanged chunks cost nothing, so a re-index after one new
payment embeds one chunk. Chunks of documents or notes that no longer exist are removed. Transaction chunks are removed by
the database itself (ON DELETE CASCADE from transactions), so undoing an import needs no work here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from .. import config
from .. import tools as core
from ..db import connect
from .loaders import GuideDirectoryLoader, NotesLoader, TransactionLoader, UploadLoader, sha, split_document
from .store import PgVectorStore

EMBED_BATCH = 32

_lock = threading.Lock()


def _existing(conn, embed_model: str, corpus: str, key_col: str, key: int) -> dict[int, str]:
    rows = conn.execute(
        f"SELECT chunk_index, content_hash FROM rag_chunks WHERE embed_model = %s AND corpus = %s AND {key_col} = %s",
        (embed_model, corpus, key)).fetchall()
    return {r["chunk_index"]: r["content_hash"] for r in rows}


def _sync_chunks(store: PgVectorStore, chunks: list[Document], corpus: str, key_col: str, key: int, progress: Callable[[int], None] | None) -> dict:
    """Make the stored chunks of ONE document / transaction equal to `chunks`. Returns {added, skipped, removed}."""
    for c in chunks:
        c.metadata["content_hash"] = sha(c.page_content)
        c.metadata[key_col] = key
    with connect() as conn:
        have = _existing(conn, store.embed_model, corpus, key_col, key)
    fresh = [c for c in chunks if have.get(c.metadata["chunk_index"]) != c.metadata["content_hash"]]
    skipped = len(chunks) - len(fresh)
    removed = 0
    keep = {c.metadata["chunk_index"] for c in chunks}
    stale = [i for i in have if i not in keep]
    if stale:
        with connect() as conn:
            removed = conn.execute(f"DELETE FROM rag_chunks WHERE embed_model = %s AND corpus = %s AND {key_col} = %s AND chunk_index = ANY(%s)",
                                   (store.embed_model, corpus, key, stale)).rowcount
    for start in range(0, len(fresh), EMBED_BATCH):
        part = fresh[start:start + EMBED_BATCH]
        store.add_documents(part)
        if progress:
            progress(len(part))
    return {"added": len(fresh), "skipped": skipped, "removed": removed}


def index_document(store: PgVectorStore, doc: Document, corpus: str, progress: Callable[[int], None] | None = None) -> dict:
    """Store one guide / note / upload as a rag_documents row and sync its chunks."""
    saved = core.rag_upsert_document(corpus, doc.metadata["source"], doc.metadata["title"], doc.page_content)
    return _sync_chunks(store, split_document(doc), corpus, "document_id", saved["id"], progress)


def index_transactions(store: PgVectorStore, progress: Callable[[int], None] | None = None) -> dict:
    """One chunk per transaction. Re-embeds a payment only when its sentence changed (say, it was recategorised).
    Multi-user mode: never (the ledger's text is sealed and the library is plain, D8)."""
    if config.multiuser():
        return {"added": 0, "skipped": 0, "removed": 0}
    total = {"added": 0, "skipped": 0, "removed": 0}
    with connect() as conn:
        have = {r["txn_id"]: r["content_hash"] for r in conn.execute(
            "SELECT txn_id, content_hash FROM rag_chunks WHERE embed_model = %s AND corpus = 'txn' AND chunk_index = 0", (store.embed_model,)).fetchall()}
    batch: list[Document] = []

    def flush() -> None:
        if batch:
            store.add_documents(list(batch))
            if progress:
                progress(len(batch))
            total["added"] += len(batch)
            batch.clear()

    for doc in TransactionLoader().lazy_load():
        digest = sha(doc.page_content)
        if have.get(doc.metadata["txn_id"]) == digest:
            total["skipped"] += 1
            continue
        doc.metadata.update({"chunk_index": 0, "content_hash": digest})
        batch.append(doc)
        if len(batch) >= EMBED_BATCH:
            flush()
    flush()
    return total


def count_work(embed_model: str) -> int:
    """About how many chunks a full re-index may need to embed (for a progress bar). Not exact: unchanged ones are skipped."""
    status = core.rag_status(embed_model)
    return status["transactions"] + sum(status["documents"].values()) * 4 + len(core.rag_guide_files())


def reindex(embeddings: Embeddings, *, embed_model: str | None = None, corpora: tuple[str, ...] = ("guide", "note", "upload", "txn"),
            progress: Callable[[int], None] | None = None) -> dict:
    """Bring the index up to date. Returns {corpus: {added, skipped, removed}, "embed_model": ...}. Only one runs at a time."""
    model = embed_model or config.embed_model()
    store = PgVectorStore(embeddings, embed_model=model)
    out: dict = {"embed_model": model}
    with _lock:
        if "guide" in corpora:
            r = {"added": 0, "skipped": 0, "removed": 0}
            for doc in GuideDirectoryLoader().lazy_load():
                for k, v in index_document(store, doc, "guide", progress).items():
                    r[k] += v
            core.rag_delete_missing("guide", [d["source"] for d in core.rag_guide_files()])
            out["guide"] = r
        for corpus, loader in (("note", NotesLoader()), ("upload", UploadLoader())):
            if corpus not in corpora:
                continue
            r = {"added": 0, "skipped": 0, "removed": 0}
            keep = []
            for doc in loader.lazy_load():
                keep.append(doc.metadata["source"])
                for k, v in index_document(store, doc, corpus, progress).items():
                    r[k] += v
            if corpus == "note":                      # a removed note disappears from the index; uploads are removed by their own route
                core.rag_delete_missing("note", keep)
            out[corpus] = r
        if "txn" in corpora:
            if config.multiuser():
                # D8: ledger text lives in sealed columns; the library must not hold a plain copy. Drop any copy left from before.
                with connect() as conn:
                    removed = conn.execute("DELETE FROM rag_chunks WHERE corpus = 'txn'").rowcount
                out["txn"] = {"added": 0, "skipped": 0, "removed": removed}
            else:
                out["txn"] = index_transactions(store, progress)
    return out
