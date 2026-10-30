"""Embeddings for the library: Ollama (nomic-embed-text) with the model's task prefixes.

nomic-embed-text was trained with a prefix per job: "search_document: " on text you store, "search_query: " on the question.
Whether it matters for your copy of the model is not certain, so it can be switched off with RECTIFI_EMBED_PREFIXES=0 and the
evals (`python -m rectifi.evals.rag --live`) are the way to decide. The default is on.
"""

from __future__ import annotations


from langchain_core.embeddings import Embeddings

DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


def prefixes_enabled() -> bool:
    from .. import config

    return config.embed_prefixes()


class PrefixedEmbeddings(Embeddings):
    """Wraps any Embeddings and adds the document / query prefix. The wrapped object is kept in `.inner`."""

    def __init__(self, inner: Embeddings, *, doc_prefix: str = DOC_PREFIX, query_prefix: str = QUERY_PREFIX) -> None:
        self.inner = inner
        self.doc_prefix = doc_prefix
        self.query_prefix = query_prefix

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed_documents([self.doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(self.query_prefix + text)


def get_rag_embeddings() -> Embeddings:
    """OllamaEmbeddings from agent.llm.get_embeddings(), with prefixes unless RECTIFI_EMBED_PREFIXES=0."""
    from ..agent.llm import get_embeddings

    base = get_embeddings()
    return PrefixedEmbeddings(base) if prefixes_enabled() else base
