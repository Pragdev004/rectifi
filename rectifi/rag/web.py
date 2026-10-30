"""Optional web search for general money questions. Off unless RECTIFI_WEB_SEARCH=1 AND the `ddgs` package is installed.

`ddgs` is an unofficial DuckDuckGo scraper with rate limits and grey-area terms, so it is NOT in requirements.txt. The graph
asks the person first (a LangGraph interrupt) and sends only the rewritten, masked, generic query. Nothing about their money.
"""

from __future__ import annotations

import importlib.util

from langchain_core.documents import Document

from .. import config
from ..privacy import mask_sensitive

MAX_RESULTS = 4
SNIPPET_CHARS = 600


def available() -> bool:
    """Web search is allowed only when it is switched on and the package is there. Otherwise the graph never offers it."""
    try:
        return config.web_search() and importlib.util.find_spec("ddgs") is not None
    except (ImportError, ValueError):
        return False


def search(query: str) -> list[Document]:
    """Top results as Documents (corpus 'web'). Any failure gives an empty list: the answer then comes from the library only."""
    query = mask_sensitive(query).strip()[:200]
    if not query:
        return []
    try:
        from ddgs import DDGS

        hits = DDGS().text(query, max_results=MAX_RESULTS) or []
    except Exception:
        return []
    out = []
    for hit in hits:
        body = (hit.get("body") or "").strip()
        if body:
            out.append(Document(page_content=body[:SNIPPET_CHARS],
                                metadata={"corpus": "web", "source": hit.get("href") or "web", "title": (hit.get("title") or "Web result")[:120], "chunk_index": 0}))
    return out
