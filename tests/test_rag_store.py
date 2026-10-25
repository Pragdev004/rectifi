"""PgVectorStore, incremental indexing and the cascade from transactions."""

from datetime import date
from decimal import Decimal

import pytest
from langchain_core.documents import Document
from langchain_core.vectorstores import InMemoryVectorStore

from rectifi import config, tools
from rectifi.db import connect
from rectifi.importer import StatementRow, import_rows
from rectifi.rag import index
from rectifi.rag.store import PgVectorStore, tokens_of
from support.fakes import BowEmbeddings

TEXTS = [
    "how to build an emergency fund with savings",
    "emergency fund size is three to six months of expenses",
    "upi fraud scam collect request never share pin",
    "credit card minimum due interest trap",
    "sip mutual fund units nav monthly",
    "rent agreement deposit notice period",
]


def make_store(texts=TEXTS, model="bow"):
    store = PgVectorStore(BowEmbeddings(), embed_model=model)
    store.add_texts(texts, [{"corpus": "guide", "txn_id": None, "document_id": None, "chunk_index": i} for i in range(len(texts))])
    return store


def test_cosine_topk_orders_by_similarity():
    store = make_store()
    hits = store.similarity_search_with_score("emergency fund", k=3)
    assert [d.page_content for d, _ in hits][0].startswith(("how to build an emergency", "emergency fund size"))
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True) and all(0 <= s <= 1.0001 for s in scores)
    assert hits[0][0].metadata["corpus"] == "guide" and hits[0][0].metadata["chunk_id"]


def test_store_matches_in_memory_vector_store_on_top_k():
    emb = BowEmbeddings()
    pg = make_store()
    mem = InMemoryVectorStore(emb)
    mem.add_texts(TEXTS)
    for query in ("emergency fund months", "upi scam pin", "credit card interest", "rent deposit"):
        a, b = pg.similarity_search_with_score(query, k=3), mem.similarity_search_with_score(query, k=3)
        assert a[0][0].page_content == b[0][0].page_content                                 # same best passage
        assert [round(s, 4) for _, s in a] == [round(s, 4) for _, s in b]                  # same scores (ties may swap order)


def test_mmr_is_more_diverse_than_plain_similarity():
    texts = ["emergency fund savings plan", "emergency fund savings plan basics", "emergency fund savings plans explained",
             "emergency fund and credit card interest warning"]
    store = make_store(texts)
    plain = [d.page_content for d in store.similarity_search("emergency fund savings plan", k=3)]
    mmr = [d.page_content for d in store.max_marginal_relevance_search("emergency fund savings plan", k=3, fetch_k=4, lambda_mult=0.1)]
    assert len(set(mmr)) == 3
    assert "emergency fund and credit card interest warning" in mmr and "emergency fund and credit card interest warning" not in plain


def test_filter_by_corpus_and_other_embed_model_is_ignored():
    store = make_store()
    store.add_texts(["emergency fund note from me"], [{"corpus": "note", "chunk_index": 0, "document_id": None}])
    assert {d.metadata["corpus"] for d in store.similarity_search("emergency fund", k=9, filter={"corpus": "note"})} == {"note"}
    assert {d.metadata["corpus"] for d in store.similarity_search("emergency fund", k=9, filter={"corpus": ["note", "guide"]})} == {"note", "guide"}
    other = PgVectorStore(BowEmbeddings(), embed_model="other-model")
    assert other.similarity_search("emergency fund") == []


def test_cache_refreshes_after_a_write_and_after_delete():
    store = make_store()
    assert store.similarity_search("zebra giraffe", k=1)[0].page_content in TEXTS or True
    ids = store.add_texts(["zebra giraffe safari"], [{"corpus": "guide", "chunk_index": 99, "document_id": None}])
    assert store.similarity_search("zebra giraffe", k=1)[0].page_content == "zebra giraffe safari"
    store.delete(ids)
    assert store.similarity_search("zebra giraffe", k=1)[0].page_content != "zebra giraffe safari"


def test_keyword_search_finds_exact_words_and_escapes_wildcards():
    store = make_store()
    assert [d.page_content for d in store.keyword_search("rent deposit", k=2)][0].startswith("rent agreement")
    assert store.keyword_search("100%_", k=2) == []
    assert tokens_of("How do I save for my rent?") == ["save", "rent"]


def test_from_texts_and_bad_corpus():
    s = PgVectorStore.from_texts(["a b c d"], BowEmbeddings(), [{"corpus": "upload", "chunk_index": 0}], embed_model="bow")
    assert s.similarity_search("a b c", k=1)[0].metadata["corpus"] == "upload"
    with pytest.raises(ValueError):
        s.add_texts(["x"], [{"corpus": "weird"}])


def test_reindex_is_incremental_by_content_hash():
    tools.add_transaction("250", "debit", "today", payee="chai", source="manual")
    emb = BowEmbeddings()
    first = index.reindex(emb, embed_model="bow")
    assert first["guide"]["added"] >= 12 and first["txn"]["added"] == 1
    embedded_after_first = len(emb.calls)
    second = index.reindex(emb, embed_model="bow")
    assert second["guide"]["added"] == 0 and second["txn"]["added"] == 0 and second["txn"]["skipped"] == 1
    assert len(emb.calls) == embedded_after_first                              # nothing was embedded again
    tools.add_transaction("90", "debit", "today", payee="bus", source="manual")
    third = index.reindex(emb, embed_model="bow")
    assert third["txn"] == {"added": 1, "skipped": 1, "removed": 0} and len(emb.calls) == embedded_after_first + 1


def test_changed_text_is_re_embedded_and_removed_notes_disappear():
    emb = BowEmbeddings()
    tools.rag_upsert_document("upload", "a.md", "A", "# A\nfirst version of the note")
    index.reindex(emb, embed_model="bow", corpora=("upload",))
    before = len(emb.calls)
    tools.rag_upsert_document("upload", "a.md", "A", "# A\nsecond version of the note")
    out = index.reindex(emb, embed_model="bow", corpora=("upload",))
    assert out["upload"]["added"] == 1 and len(emb.calls) == before + 1
    tools.save_commitment("walk to class")
    index.reindex(emb, embed_model="bow", corpora=("note",))
    assert tools.rag_list_documents("note")
    with connect() as conn:
        conn.execute("DELETE FROM weekly_reviews")
    index.reindex(emb, embed_model="bow", corpora=("note",))
    assert tools.rag_list_documents("note") == []
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rag_chunks WHERE corpus = 'note'").fetchone()["n"] == 0


def test_a_recategorised_entry_is_re_embedded():
    t = tools.add_transaction("500", "debit", "today", payee="mystery", category="Shopping", source="manual")["transaction"]
    emb = BowEmbeddings()
    index.reindex(emb, embed_model="bow", corpora=("txn",))
    tools.set_category(t["id"], "Food & Dining", False)
    out = index.reindex(emb, embed_model="bow", corpora=("txn",))
    assert out["txn"]["added"] == 1
    assert "Food & Dining" in PgVectorStore(emb, embed_model="bow").similarity_search("mystery", k=1)[0].page_content


def test_txn_chunks_cascade_when_an_import_is_undone():
    tools.add_account("Savings", "4417")
    rows = [StatementRow(date(2026, 10, 1), Decimal("120.00"), "debit", "CHAI POINT", None, None, 1),
            StatementRow(date(2026, 10, 2), Decimal("340.00"), "debit", "BOOK SHOP", None, None, 1)]
    result = import_rows(rows, "Savings", "oct.csv")
    index.reindex(BowEmbeddings(), embed_model="bow", corpora=("txn",))
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rag_chunks WHERE corpus = 'txn'").fetchone()["n"] == 2
    tools.undo_import(result.batch_id)
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rag_chunks WHERE corpus = 'txn'").fetchone()["n"] == 0


def test_status_counts_chunks_per_section():
    index.reindex(BowEmbeddings(), embed_model="bow", corpora=("guide",))
    st = tools.rag_status("bow")
    assert st["chunks"]["guide"] >= 12 and st["documents"]["guide"] >= 12 and st["updated_at"] and st["other_model_chunks"] == 0
    assert tools.rag_status("nomic-embed-text")["chunks_total"] == 0 and tools.rag_status("nomic-embed-text")["other_model_chunks"] > 0


def test_delete_document_removes_chunks_and_delete_missing():
    saved = tools.rag_upsert_document("upload", "b.md", "B", "# B\nsome words here")
    index.reindex(BowEmbeddings(), embed_model="bow", corpora=("upload",))
    assert tools.rag_delete_document(saved["id"], corpus="guide") is False
    assert tools.rag_delete_document(saved["id"], corpus="upload") is True
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rag_chunks WHERE document_id = %s", (saved["id"],)).fetchone()["n"] == 0
