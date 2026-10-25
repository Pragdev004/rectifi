"""Loaders, splitters, upload checks and embeddings for the knowledge library."""

import re
import types
from pathlib import Path

import pytest
from langchain_core.documents import Document

from rectifi import config, tools
from rectifi.rag import embeddings as emb
from rectifi.rag.loaders import (GuideDirectoryLoader, NotesLoader, PdfplumberLoader, TransactionLoader, UploadLoader,
                                 split_document, transaction_text)
from support.fakes import BowEmbeddings

RUPEE = re.compile(r"(?i)(?<![a-z])(?:rs\.?|inr|₹)\s*\d|\brupees?\b\s*\d|\b\d[\d,]*\s*(?:lakh|crore)\b")
FIXTURES = Path(__file__).parent / "fixtures"


def test_there_are_twelve_guides_and_none_has_a_rupee_figure():
    guides = tools.rag_guide_files()
    assert len(guides) >= 12
    for g in guides:
        assert g["content"].startswith("# "), g["source"]
        assert not RUPEE.search(g["content"]), g["source"]


def test_guides_have_no_rupee_figures():
    test_there_are_twelve_guides_and_none_has_a_rupee_figure()


def test_guide_loader_is_lazy_and_labels_documents():
    loader = GuideDirectoryLoader()
    it = loader.lazy_load()
    assert isinstance(it, types.GeneratorType)
    first = next(it)
    assert first.metadata["corpus"] == "guide" and first.metadata["source"].startswith("guides/") and first.metadata["title"]
    assert len(loader.load()) == len(tools.rag_guide_files())


def test_notes_loader_reads_commitments_and_transaction_notes_masked():
    tools.add_transaction("250", "debit", "today", payee="Swiggy", note="dinner with ref 123456789012", source="manual")
    tools.save_commitment("no more than 3 delivery orders")
    docs = NotesLoader().load()
    sources = sorted(d.metadata["source"] for d in docs)
    assert any(s.startswith("commitment:") for s in sources) and any(s.startswith("txn-note:") for s in sources)
    text = " ".join(d.page_content for d in docs)
    assert "123456789012" not in text and "9012" in text          # masked to the last four digits
    assert "no more than 3 delivery orders" in text


def test_transaction_loader_one_masked_document_per_row():
    tools.add_transaction("1499", "debit", "2026-08-14", payee="CULT FIT", category="Health", note="gym", source="manual")
    tools.add_transaction("8000", "credit", "2026-08-15", payee="Stipend", source="manual")
    docs = TransactionLoader().load()
    assert len(docs) == 2
    first = docs[0]
    assert first.page_content == "2026-08-14 · paid ₹1,499.00 · CULT FIT · Health · note: gym"
    assert first.metadata["corpus"] == "txn" and first.metadata["txn_id"] == 1
    assert "received ₹8,000.00" in docs[1].page_content
    assert len(TransactionLoader(after_id=1).load()) == 1


def test_transaction_text_masks_account_numbers():
    text = transaction_text({"direction": "debit", "amount": "10", "txn_date": "2026-01-01", "payee": "UPI/123456789012/SHOP", "category": None})
    assert "123456789012" not in text


def test_upload_check_accepts_notes_and_refuses_statements_and_other_files():
    name, title, text = tools.rag_check_upload("my notes.md", b"# Flat rules\nPay rent on the 5th.")
    assert (name, title) == ("my notes.md", "Flat rules") and "5th" in text
    statement = "\n".join(f"0{d}-08-2026  UPI-SHOP  {d}00.00  {d}0,500.00" for d in range(1, 8)).encode()
    for bad_name, data in [("s.txt", statement), ("s.csv", b"a,b"), ("s.pdf", b"%PDF"), ("big.txt", b"x" * (tools.RAG_UPLOAD_MAX + 1)),
                           ("e.md", b"   "), ("bin.txt", b"a\x00b")]:
        with pytest.raises(tools.RectifiError):
            tools.rag_check_upload(bad_name, data)
    with pytest.raises(tools.RectifiError, match="bank statement"):
        tools.rag_check_upload("hdr.txt", b"Statement of Account\nOpening Balance 100.00\nClosing Balance 90.00\nNarration")


def test_upload_loader_reads_stored_documents_masked():
    tools.rag_upsert_document("upload", "flat.md", "Flat rules", "Deposit paid to account 123456789012.")
    (doc,) = UploadLoader().load()
    assert doc.metadata["corpus"] == "upload" and "123456789012" not in doc.page_content


def test_pdfplumber_loader_reads_text_pages():
    docs = PdfplumberLoader(FIXTURES / "statement_text_only.pdf").load()
    assert docs and docs[0].metadata["page"] == 1 and docs[0].page_content.strip()


def test_split_sizes_and_structure(monkeypatch):
    monkeypatch.setattr(config, "RAG_CHUNK_CHARS", 300)
    monkeypatch.setattr(config, "RAG_CHUNK_OVERLAP", 40)
    body = " ".join(f"Sentence number {i} about saving money." for i in range(60))
    doc = Document(page_content=f"# Title\n\n## First part\n{body}\n\n## Second part\nShort section.", metadata={"corpus": "guide", "source": "guides/x.md"})
    chunks = split_document(doc)
    assert len(chunks) > 3
    assert all(len(c.page_content) <= 300 for c in chunks)
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert {c.metadata["source"] for c in chunks} == {"guides/x.md"}
    assert any(c.metadata.get("section", "").endswith("Second part") for c in chunks)
    assert any(c.page_content.startswith("## Second part") for c in chunks)      # structure first: a heading starts its own chunk


def test_prefixed_embeddings_add_task_prefixes():
    inner = BowEmbeddings()
    wrapped = emb.PrefixedEmbeddings(inner)
    wrapped.embed_documents(["rent"])
    wrapped.embed_query("rent?")
    assert inner.calls == ["search_document: rent", "search_query: rent?"]


def test_ollama_embeddings_via_fake_server(fake_ollama, monkeypatch):
    monkeypatch.setenv("RECTIFI_EMBED_PREFIXES", "1")
    e = emb.get_rag_embeddings()
    assert isinstance(e, emb.PrefixedEmbeddings)
    vec = e.embed_query("how to budget")
    assert isinstance(vec, list) and len(vec) > 8
    assert len(e.embed_documents(["a b", "c d"])) == 2
    sent = [b.get("input") for b in fake_ollama.bodies("/api/embed")]
    assert any(isinstance(x, list) and x[0].startswith("search_document: ") or x == "search_query: how to budget" or x == ["search_query: how to budget"] for x in sent)
    monkeypatch.setenv("RECTIFI_EMBED_PREFIXES", "0")
    assert not isinstance(emb.get_rag_embeddings(), emb.PrefixedEmbeddings)
