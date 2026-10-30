"""Retrievers: the ways the graph finds passages.

    mmr_retriever        store.as_retriever(search_type="mmr"): relevant AND different from each other
    HybridRetriever      a custom BaseRetriever: vector (MMR) + keyword (Postgres ILIKE), fused by reciprocal-rank fusion
    parallel_retrieval   RunnableParallel over the sections (guide / note / txn / upload), run once per query
    multi_query          LCEL: prompt | llm.with_structured_output(QueryList) | merge. Falls back to a JsonOutputParser chain.
    StripCompressor      contextual compression (CRAG's "knowledge refinement"): keep the sentences close to the question
"""

from __future__ import annotations

import re
from operator import itemgetter

import numpy as np
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.documents.compressor import BaseDocumentCompressor
from langchain_core.embeddings import Embeddings
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable, RunnableLambda, RunnableParallel
from pydantic import BaseModel, ConfigDict, Field

from .store import PgVectorStore

CORPORA = ("guide", "note", "txn", "upload")
RRF_K = 60


def refine_tau() -> float:
    """Kept for the callers and tests that already use this name; the setting lives in config.rag_refine_tau."""
    from .. import config

    return config.rag_refine_tau()


def doc_key(doc: Document) -> str:
    return str(doc.metadata.get("chunk_id") or doc.id or hash(doc.page_content))


def rrf(lists: list[list[Document]], k: int = RRF_K) -> list[Document]:
    """Reciprocal-rank fusion: a passage near the top of several lists beats one that is first in a single list."""
    score: dict[str, float] = {}
    first: dict[str, Document] = {}
    for docs in lists:
        for rank, doc in enumerate(docs):
            key = doc_key(doc)
            score[key] = score.get(key, 0.0) + 1.0 / (k + rank + 1)
            first.setdefault(key, doc)
    return [first[key] for key in sorted(score, key=lambda x: (-score[x], x))]


def mmr_retriever(store: PgVectorStore, *, corpus: str | list[str] | None = None, k: int = 6, fetch_k: int = 20, lambda_mult: float = 0.5):
    kwargs: dict = {"k": k, "fetch_k": fetch_k, "lambda_mult": lambda_mult}
    if corpus:
        kwargs["filter"] = {"corpus": corpus}
    return store.as_retriever(search_type="mmr", search_kwargs=kwargs)


class HybridRetriever(BaseRetriever):
    """Vector search finds passages that MEAN the same; keyword search finds passages with the same WORDS (a shop name, a
    note's odd spelling). Each gives a ranked list and rrf() fuses them."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    store: PgVectorStore
    corpus: list[str] | None = None
    k: int = 6
    fetch_k: int = 20
    keyword: bool = True
    lambda_mult: float = 0.5
    last: dict = Field(default_factory=dict)       # what each pass returned on the last call, for tests and the trace

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun | None = None) -> list[Document]:
        flt = {"corpus": self.corpus} if self.corpus else None
        vector = self.store.max_marginal_relevance_search(query, k=self.k, fetch_k=self.fetch_k, lambda_mult=self.lambda_mult, filter=flt)
        words = self.store.keyword_search(query, k=self.k, filter=flt) if self.keyword else []
        self.last = {"vector": vector, "keyword": words}
        return rrf([vector, words])[: self.k]


def parallel_retrieval(store: PgVectorStore, corpora: tuple[str, ...] | list[str] = CORPORA, *, k: int = 4, fetch_k: int = 16) -> Runnable:
    """RunnableParallel({corpus: HybridRetriever(corpus)}): every section searched at the same time. Input: a query string."""
    return RunnableParallel({c: HybridRetriever(store=store, corpus=[c], k=k, fetch_k=fetch_k) for c in corpora})


def retrieve_many(queries: list[str], store: PgVectorStore, corpora: tuple[str, ...] | list[str] = CORPORA, *, k: int = 4, fetch_k: int = 16,
                  limit: int = 6) -> list[Document]:
    """Run every query over every section, then fuse all the ranked lists into one and keep the best `limit`."""
    chain = parallel_retrieval(store, corpora, k=k, fetch_k=fetch_k)
    lists: list[list[Document]] = []
    for result in chain.batch(list(queries)):
        lists.extend(result.values())
    return rrf(lists)[:limit]


# ───────────── multi-query ─────────────

class QueryList(BaseModel):
    queries: list[str] = Field(description="2 or 3 different ways to ask the same question, each a short plain search sentence")


MULTI_QUERY_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You help search a small library of money guides and a person's own notes. Rewrite the question as 2 or 3 different "
               "search queries that mean the same thing but use different words. Keep names and dates. Never add rupee amounts."),
    ("human", "{question}"),
])
MULTI_QUERY_JSON_PROMPT = ChatPromptTemplate.from_messages([
    ("system", 'Rewrite the question as 2 or 3 different search queries with different words. Reply with JSON only: {{"queries": ["...", "..."]}}'),
    ("human", "{question}"),
])


def _merge_queries(parts: dict, limit: int = 4) -> list[str]:
    out = [parts["question"]]
    generated = parts.get("generated")
    items = generated.queries if isinstance(generated, QueryList) else (generated or {}).get("queries", [])
    for q in items:
        q = (q or "").strip()
        if q and q.lower() not in {x.lower() for x in out}:
            out.append(q)
    return out[:limit]


def multi_query(llm) -> Runnable:
    """LCEL multi-query. Input {"question": str}; output list[str] with the original question first.
        RunnableParallel(question=itemgetter, generated=prompt | llm.with_structured_output(QueryList)) | RunnableLambda(merge)
    If the model cannot do structured output, the same chain is built with a JsonOutputParser instead."""
    try:
        generator = MULTI_QUERY_PROMPT | llm.with_structured_output(QueryList)
    except (NotImplementedError, AttributeError):
        generator = multi_query_json_chain(llm)
    return RunnableParallel(question=itemgetter("question"), generated=generator) | RunnableLambda(_merge_queries)


def multi_query_json_chain(llm) -> Runnable:
    return MULTI_QUERY_JSON_PROMPT | llm | JsonOutputParser()


# ───────────── contextual compression ─────────────

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def strips_of(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s and s.strip()]


class StripCompressor(BaseDocumentCompressor):
    """Contextual compression, the way an EmbeddingsFilter does it, but on sentence strips inside each passage: keep the strips
    whose cosine similarity to the question is at least `tau` (always at least the best one), in their original order."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    embeddings: Embeddings
    tau: float = Field(default_factory=refine_tau)

    def compress_documents(self, documents, query: str, callbacks=None) -> list[Document]:
        if not documents:
            return []
        q = np.asarray(self.embeddings.embed_query(query), dtype=np.float64)
        qn = float(np.linalg.norm(q)) or 1.0
        out: list[Document] = []
        for doc in documents:
            strips = strips_of(doc.page_content)
            if len(strips) <= 1:
                out.append(doc)
                continue
            vecs = np.asarray(self.embeddings.embed_documents(strips), dtype=np.float64)
            norms = np.linalg.norm(vecs, axis=1) * qn
            sims = (vecs @ q) / np.where(norms == 0, 1.0, norms)
            keep = [i for i, s in enumerate(sims) if s >= self.tau] or [int(np.argmax(sims))]
            kept = [strips[i] for i in sorted(keep)]
            meta = {**doc.metadata, "refined": len(kept) < len(strips), "strips_kept": len(kept), "strips_total": len(strips)}
            out.append(Document(id=doc.id, page_content=" ".join(kept), metadata=meta))
        return out
