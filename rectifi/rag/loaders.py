"""Document loaders and splitters (no langchain-community).

    GuideDirectoryLoader   the built-in guides, one Document per file (lazy_load)
    NotesLoader            weekly commitments and transaction notes: the person's own words
    TransactionLoader      one Document per transaction, built from the database and masked
    UploadLoader           files the person added (.md / .txt), statement-looking files refused
    PdfplumberLoader       optional: pdf text with pdfplumber (NOT wired in: CLAUDE.md rule 8 keeps PDFs out of any model path)

Everything except guides is masked (privacy.mask_sensitive) before it is stored, because chunks reach a model.
split_documents(): MarkdownHeaderTextSplitter by headings, then RecursiveCharacterTextSplitter by size.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from .. import config
from .. import tools as core
from ..money import fmt_inr
from ..privacy import mask_sensitive

HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3")]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GuideDirectoryLoader(BaseLoader):
    """The built-in guides in rectifi/knowledge/*.md. One Document per file; metadata: corpus, source, title."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else core.KNOWLEDGE_DIR

    def lazy_load(self) -> Iterator[Document]:
        for path in sorted(self.directory.glob("*.md")):
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                continue
            yield Document(page_content=text, metadata={"corpus": "guide", "source": f"guides/{path.name}", "title": core._title_of(text, path.stem)})


class NotesLoader(BaseLoader):
    """The person's weekly commitments and transaction notes. Their own words, masked."""

    def lazy_load(self) -> Iterator[Document]:
        for row in core.rag_note_rows():
            yield Document(page_content=mask_sensitive(row["content"]),
                           metadata={"corpus": "note", "source": row["source"], "title": row["title"]})


def transaction_text(row: dict) -> str:
    """'2026-08-14 · paid ₹1,499.00 · CULT FIT · Health · note: …'. The rupee figure is the database's own, formatted by fmt_inr."""
    verb = "paid" if row["direction"] == "debit" else "received"
    who = row.get("payee") or row.get("description") or "unknown"
    parts = [str(row["txn_date"]), f"{verb} {fmt_inr(row['amount'])}", who, row.get("category") or "Uncategorised"]
    if row.get("note") and str(row["note"]).strip():
        parts.append(f"note: {str(row['note']).strip()}")
    return mask_sensitive(" · ".join(parts))


class TransactionLoader(BaseLoader):
    """One Document per transaction. metadata.txn_id links the chunk to the row, so undoing an import removes it (ON DELETE CASCADE)."""

    def __init__(self, after_id: int = 0, limit: int | None = None) -> None:
        self.after_id, self.limit = after_id, limit

    def lazy_load(self) -> Iterator[Document]:
        for row in core.rag_txn_rows(self.after_id, self.limit):
            yield Document(page_content=transaction_text(row),
                           metadata={"corpus": "txn", "source": f"txn:{row['id']}", "title": f"Entry #{row['id']}", "txn_id": row["id"]})


class UploadLoader(BaseLoader):
    """Documents the person added to the library (stored in rag_documents, section 'upload')."""

    def lazy_load(self) -> Iterator[Document]:
        for doc in core.rag_list_documents("upload"):
            full = core.rag_get_document(doc["id"])
            if full:
                yield Document(page_content=mask_sensitive(full["content"]),
                               metadata={"corpus": "upload", "source": full["source"], "title": full["title"], "document_id": full["id"]})


class PdfplumberLoader(BaseLoader):
    """Text of a PDF through pdfplumber (already pinned for statements). Provided for completeness; the library does not accept
    PDFs, because rule 8 says no model ever sees a PDF. Use it only if that rule is changed on purpose."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def lazy_load(self) -> Iterator[Document]:
        import pdfplumber

        with pdfplumber.open(self.path) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text() or "").strip()
                if text:
                    yield Document(page_content=text, metadata={"corpus": "upload", "source": self.path.name, "page": number})


def splitters() -> tuple[MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter]:
    return (MarkdownHeaderTextSplitter(HEADERS, strip_headers=False),
            RecursiveCharacterTextSplitter(chunk_size=config.RAG_CHUNK_CHARS, chunk_overlap=config.RAG_CHUNK_OVERLAP))


def split_document(doc: Document) -> list[Document]:
    """Structure first (one piece per heading section), then size (long sections cut with overlap). Each chunk keeps the
    document's metadata plus chunk_index and, when the section had a heading, section."""
    by_header, by_size = splitters()
    pieces: list[Document] = []
    for part in by_header.split_text(doc.page_content) or [Document(page_content=doc.page_content)]:
        section = " > ".join(part.metadata[h] for _, h in HEADERS if part.metadata.get(h))
        for small in by_size.split_documents([part]):
            small.metadata = {**doc.metadata, **({"section": section} if section else {})}
            pieces.append(small)
    for i, piece in enumerate(pieces):
        piece.metadata["chunk_index"] = i
    return [p for p in pieces if p.page_content.strip()]
