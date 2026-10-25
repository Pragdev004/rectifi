"""PgVectorStore: a LangChain VectorStore over the plain `rag_chunks` table.

Why not pgvector, Chroma or FAISS: pgvector is not installed on a Windows Postgres; Chroma and FAISS keep a second store on
disk that needs its own backup. A Postgres table rides along with the database, and ON DELETE CASCADE from `transactions`
keeps "undo import" correct. The library is small (a few hundred guide chunks, up to tens of thousands of entries), so the
embeddings (REAL[]) are loaded into one numpy float32 matrix and cosine similarity takes milliseconds.

The matrix is cached on the store object and rebuilt when (row count, max id, sum of content hashes) changes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

import numpy as np
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore
from langchain_core.vectorstores.utils import maximal_marginal_relevance

from ..db import connect

_STOPWORDS = frozenset("the a an and or of to in on at for is are was were do does did how what why when which who my me i it its this that "
                       "with from by be can about should would".split())


def tokens_of(text: str) -> list[str]:
    """Lower-case words of 3+ letters or digits without the small filler words. Used by the keyword pass."""
    seen: list[str] = []
    for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(word) >= 3 and word not in _STOPWORDS and word not in seen:
            seen.append(word)
    return seen


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class PgVectorStore(VectorStore):
    """Documents carry metadata: corpus (guide | note | upload | txn), and either document_id or txn_id, plus chunk_index.
    `filter` accepts {"corpus": "guide"} or {"corpus": ["guide", "note"]}."""

    def __init__(self, embedding: Embeddings, *, embed_model: str = "nomic-embed-text") -> None:
        self._embedding = embedding
        self.embed_model = embed_model
        self._cache: dict[str, Any] = {"key": None}

    @property
    def embeddings(self) -> Embeddings:
        return self._embedding

    # ── writing ──

    def add_documents(self, documents: list[Document], **kwargs: Any) -> list[str]:
        return self.add_texts([d.page_content for d in documents], [d.metadata for d in documents], **kwargs)

    def add_texts(self, texts: Iterable[str], metadatas: list[dict] | None = None, *, vectors: list[list[float]] | None = None,
                  **kwargs: Any) -> list[str]:
        """Embed (unless `vectors` is given) and upsert. Returns the chunk ids as strings."""
        texts = list(texts)
        if not texts:
            return []
        metadatas = metadatas or [{} for _ in texts]
        vectors = vectors if vectors is not None else self._embedding.embed_documents(texts)
        ids: list[str] = []
        with connect() as conn:
            for text, meta, vec in zip(texts, metadatas, vectors):
                corpus = meta.get("corpus")
                if corpus not in ("guide", "note", "upload", "txn"):
                    raise ValueError(f"metadata.corpus must be guide, note, upload or txn (got {corpus!r}).")
                clean = {k: v for k, v in meta.items() if k not in ("corpus", "document_id", "txn_id", "chunk_index")}
                row = conn.execute(
                    """INSERT INTO rag_chunks (document_id, txn_id, corpus, chunk_index, content, metadata, content_hash, embed_model, embedding)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                       ON CONFLICT (corpus, COALESCE(document_id, 0), COALESCE(txn_id, 0), chunk_index, embed_model)
                       DO UPDATE SET content = EXCLUDED.content, metadata = EXCLUDED.metadata, content_hash = EXCLUDED.content_hash,
                                     embedding = EXCLUDED.embedding, created_at = now()
                       RETURNING id""",
                    (meta.get("document_id"), meta.get("txn_id"), corpus, int(meta.get("chunk_index", 0)), text, json.dumps(clean, default=str),
                     meta.get("content_hash") or _hash(text), self.embed_model, [float(x) for x in vec])).fetchone()
                ids.append(str(row["id"]))
        return ids

    def delete(self, ids: list[str] | None = None, **kwargs: Any) -> bool | None:
        if not ids:
            return False
        with connect() as conn:
            conn.execute("DELETE FROM rag_chunks WHERE id = ANY(%s)", ([int(i) for i in ids],))
        return True

    @classmethod
    def from_texts(cls, texts: list[str], embedding: Embeddings, metadatas: list[dict] | None = None, *, embed_model: str = "nomic-embed-text",
                   **kwargs: Any) -> "PgVectorStore":
        store = cls(embedding, embed_model=embed_model)
        store.add_texts(texts, metadatas)
        return store

    # ── the matrix ──

    def _fingerprint(self, conn) -> tuple:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m, COALESCE(SUM(hashtext(content_hash)::bigint), 0) AS h "
            "FROM rag_chunks WHERE embed_model = %s", (self.embed_model,)).fetchone()
        return (row["n"], row["m"], int(row["h"]))

    def _matrix(self):
        with connect() as conn:
            key = self._fingerprint(conn)
            if self._cache["key"] == key:
                return self._cache
            rows = conn.execute("SELECT id, corpus, embedding FROM rag_chunks WHERE embed_model = %s ORDER BY id", (self.embed_model,)).fetchall()
        if rows:
            mat = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            mat = mat / np.where(norms == 0, 1.0, norms)
        else:
            mat = np.zeros((0, 0), dtype=np.float32)
        self._cache = {"key": key, "ids": np.asarray([r["id"] for r in rows], dtype=np.int64),
                       "corpus": np.asarray([r["corpus"] for r in rows], dtype=object), "mat": mat}
        return self._cache

    def _query_vector(self, embedding: list[float]) -> np.ndarray:
        q = np.asarray(embedding, dtype=np.float32)
        n = float(np.linalg.norm(q))
        return q / n if n else q

    def _scored(self, embedding: list[float], filter: dict | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(ids, scores, matrix rows) for the chunks that pass the filter, best first."""
        c = self._matrix()
        if len(c["ids"]) == 0:
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float32), np.zeros((0, 0), dtype=np.float32)
        mask = self._mask(c["corpus"], filter)
        ids, mat = c["ids"][mask], c["mat"][mask]
        if len(ids) == 0 or mat.shape[1] != len(embedding):
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float32), np.zeros((0, 0), dtype=np.float32)
        scores = mat @ self._query_vector(embedding)
        order = np.argsort(-scores, kind="stable")
        return ids[order], scores[order], mat[order]

    @staticmethod
    def _mask(corpus: np.ndarray, filter: dict | None) -> np.ndarray:
        wanted = (filter or {}).get("corpus")
        if wanted is None:
            return np.ones(len(corpus), dtype=bool)
        wanted = {wanted} if isinstance(wanted, str) else set(wanted)
        return np.asarray([c in wanted for c in corpus], dtype=bool)

    def _documents(self, ids: list[int], scores: dict[int, float] | None = None) -> list[Document]:
        if not ids:
            return []
        with connect() as conn:
            rows = {r["id"]: r for r in conn.execute(
                """SELECT c.id, c.corpus, c.content, c.metadata, c.chunk_index, c.document_id, c.txn_id, d.title, d.source
                   FROM rag_chunks c LEFT JOIN rag_documents d ON d.id = c.document_id WHERE c.id = ANY(%s)""", (ids,)).fetchall()}
        out = []
        for i in ids:
            r = rows.get(i)
            if r is None:
                continue
            meta = {**(r["metadata"] or {}), "corpus": r["corpus"], "chunk_id": r["id"], "chunk_index": r["chunk_index"],
                    "document_id": r["document_id"], "txn_id": r["txn_id"]}
            meta.setdefault("title", r["title"] or (f"Entry #{r['txn_id']}" if r["txn_id"] else "Library"))
            meta.setdefault("source", r["source"] or (f"txn:{r['txn_id']}" if r["txn_id"] else "library"))
            if scores is not None and i in scores:
                meta["score"] = float(scores[i])
            out.append(Document(id=str(r["id"]), page_content=r["content"], metadata=meta))
        return out

    # ── reading ──

    def similarity_search_with_score(self, query: str, k: int = 4, filter: dict | None = None, **kwargs: Any) -> list[tuple[Document, float]]:
        ids, scores, _ = self._scored(self._embedding.embed_query(query), filter)
        ids, scores = ids[:k], scores[:k]
        docs = self._documents([int(i) for i in ids], {int(i): float(s) for i, s in zip(ids, scores)})
        by_id = {int(d.id): d for d in docs}
        return [(by_id[int(i)], float(s)) for i, s in zip(ids, scores) if int(i) in by_id]

    def similarity_search(self, query: str, k: int = 4, filter: dict | None = None, **kwargs: Any) -> list[Document]:
        return [d for d, _ in self.similarity_search_with_score(query, k, filter)]

    def max_marginal_relevance_search(self, query: str, k: int = 4, fetch_k: int = 20, lambda_mult: float = 0.5,
                                      filter: dict | None = None, **kwargs: Any) -> list[Document]:
        """Maximal marginal relevance: the best `k` of the closest `fetch_k`, trading closeness to the question against
        similarity to what is already picked (langchain_core's maximal_marginal_relevance)."""
        embedding = self._embedding.embed_query(query)
        ids, scores, mat = self._scored(embedding, filter)
        if len(ids) == 0:
            return []
        ids, scores, mat = ids[:fetch_k], scores[:fetch_k], mat[:fetch_k]
        picked = maximal_marginal_relevance(self._query_vector(embedding), list(mat), lambda_mult=lambda_mult, k=k)
        chosen = [int(ids[i]) for i in picked]
        docs = {int(d.id): d for d in self._documents(chosen, {int(i): float(s) for i, s in zip(ids, scores)})}
        return [docs[i] for i in chosen if i in docs]

    def keyword_search(self, query: str, k: int = 4, filter: dict | None = None) -> list[Document]:
        """Postgres ILIKE pass: chunks ranked by how many of the question's words they contain. The other half of the hybrid."""
        words = tokens_of(query)[:8]
        if not words:
            return []
        score_sql = " + ".join("(CASE WHEN content ILIKE %s THEN 1 ELSE 0 END)" for _ in words)
        likes = [_like(w) for w in words]
        wanted = (filter or {}).get("corpus")
        corpora = None if wanted is None else ([wanted] if isinstance(wanted, str) else list(wanted))
        sql = (f"SELECT id, ({score_sql}) AS score FROM rag_chunks WHERE embed_model = %s AND (%s::text[] IS NULL OR corpus = ANY(%s)) "
               f"AND ({score_sql}) > 0 ORDER BY score DESC, id LIMIT %s")
        with connect() as conn:
            rows = conn.execute(sql, [*likes, self.embed_model, corpora, corpora, *likes, k]).fetchall()
        return self._documents([r["id"] for r in rows], {r["id"]: r["score"] for r in rows})

    def _select_relevance_score_fn(self):
        return lambda score: (score + 1.0) / 2.0


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
