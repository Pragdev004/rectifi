"""Retrievers: MMR, hybrid (vector + keyword, fused), parallel over sections, multi-query (LCEL), strip compression."""

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from rag_support import RuleModel
from rectifi import tools
from rectifi.rag import index
from rectifi.rag.retrieve import (HybridRetriever, QueryList, StripCompressor, mmr_retriever, multi_query, multi_query_json_chain,
                                  parallel_retrieval, retrieve_many, rrf)
from rectifi.rag.store import PgVectorStore
from support.fakes import BowEmbeddings


class SameVector(Embeddings):
    """Every text gets the same vector, so only the keyword pass can tell passages apart."""

    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0]


def doc(text, chunk_id, corpus="guide"):
    return Document(page_content=text, metadata={"chunk_id": chunk_id, "corpus": corpus})


def indexed_store():
    tools.add_transaction("1499", "debit", "2026-08-14", payee="CULT FIT", category="Health", note="gym membership", source="manual")
    emb = BowEmbeddings()
    index.reindex(emb, embed_model="bow")
    return PgVectorStore(emb, embed_model="bow")


def test_rrf_prefers_passages_that_appear_in_several_lists():
    a, b, c = doc("a", 1), doc("b", 2), doc("c", 3)
    fused = rrf([[a, b], [c, b]])
    assert [d.metadata["chunk_id"] for d in fused] == [2, 1, 3]            # b is second in one list and first... in both: top


def test_mmr_diversity_through_as_retriever():
    store = PgVectorStore(BowEmbeddings(), embed_model="bow")
    texts = ["emergency fund savings plan", "emergency fund savings plan basics", "emergency fund savings plan explained", "emergency fund credit card warning"]
    store.add_texts(texts, [{"corpus": "guide", "chunk_index": i, "document_id": None} for i in range(4)])
    plain = store.as_retriever(search_kwargs={"k": 3}).invoke("emergency fund savings plan")
    diverse = mmr_retriever(store, k=3, fetch_k=4, lambda_mult=0.1).invoke("emergency fund savings plan")
    assert len(diverse) == 3
    assert "emergency fund credit card warning" in [d.page_content for d in diverse]
    assert "emergency fund credit card warning" not in [d.page_content for d in plain]


def test_hybrid_rrf_keyword_pass_rescues_what_vectors_cannot_tell_apart():
    store = PgVectorStore(SameVector(), embed_model="same")
    texts = [f"filler passage number {i}" for i in range(8)] + ["the xylophone repair shop on main road"]
    store.add_texts(texts, [{"corpus": "guide", "chunk_index": i, "document_id": None} for i in range(len(texts))])
    hybrid = HybridRetriever(store=store, k=3, fetch_k=4)
    got = hybrid.invoke("xylophone repair")
    assert "the xylophone repair shop on main road" in [d.page_content for d in got]
    assert hybrid.last["keyword"] and hybrid.last["vector"]
    vector_only = [d.page_content for d in hybrid.last["vector"]]
    assert "the xylophone repair shop on main road" not in vector_only


def test_hybrid_filters_by_section():
    store = indexed_store()
    got = HybridRetriever(store=store, corpus=["txn"], k=5).invoke("cult fit")
    assert got and {d.metadata["corpus"] for d in got} == {"txn"}


def test_parallel_retrieval_union_over_sections_and_queries():
    store = indexed_store()
    chain = parallel_retrieval(store, ("guide", "note", "txn", "upload"), k=2)
    out = chain.invoke("emergency fund")
    assert set(out) == {"guide", "note", "txn", "upload"} and out["guide"] and out["upload"] == []
    docs = retrieve_many(["emergency fund", "gym membership cult fit"], store, k=2, limit=6)
    corpora = {d.metadata["corpus"] for d in docs}
    assert "guide" in corpora and ("txn" in corpora or "note" in corpora)
    keys = [d.metadata["chunk_id"] for d in docs]
    assert len(keys) == len(set(keys)) and len(docs) <= 6


def test_multiquery_runs_prompt_structured_llm_and_keeps_the_original_first():
    llm = RuleModel(structured={"QueryList": lambda text: QueryList(queries=["how much emergency money", "emergency fund size", "How to build an emergency fund"])})
    queries = multi_query(llm).invoke({"question": "How to build an emergency fund"})
    assert queries[0] == "How to build an emergency fund" and len(queries) == 3          # case-insensitive duplicate dropped
    assert "emergency fund size" in queries
    assert llm.calls("structured", "QueryList")


def test_multiquery_json_parser_fallback():
    class NoStructured(RuleModel):
        def with_structured_output(self, schema, **kwargs):
            raise NotImplementedError

    llm = NoStructured(write='{"queries": ["rent deposit rules", "tenant security deposit"]}')
    assert multi_query_json_chain(llm).invoke({"question": "q"}) == {"queries": ["rent deposit rules", "tenant security deposit"]}
    assert multi_query(llm).invoke({"question": "rent deposit?"}) == ["rent deposit?", "rent deposit rules", "tenant security deposit"]


def test_strip_compressor_keeps_close_sentences_and_always_one():
    emb = BowEmbeddings()
    text = "Rent is due on the fifth. The weather was lovely on Sunday. My cat likes fish. Deposit is returned after notice."
    comp = StripCompressor(embeddings=emb, tau=0.2)
    (out,) = comp.compress_documents([Document(page_content=text, metadata={"corpus": "guide"})], "when is rent due")
    assert "Rent is due on the fifth." in out.page_content and "cat likes fish" not in out.page_content
    assert out.metadata["refined"] is True and out.metadata["strips_total"] == 4
    (strict,) = StripCompressor(embeddings=emb, tau=0.99).compress_documents([Document(page_content=text)], "zzz qqq")
    assert strict.metadata["strips_kept"] == 1                                       # never empties a passage
    one = Document(page_content="2026-08-14 · paid ₹1,499.00 · CULT FIT · Health")
    assert comp.compress_documents([one], "rent") == [one]
    assert comp.compress_documents([], "x") == []
