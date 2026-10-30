"""What the parallel workstreams code against. Shapes and signatures only; each builder lives in its owner's module.

    build_rag_subgraph    rectifi/rag/graph.py        (WS-RAG)
    build_chart_subgraph  rectifi/charts/graph.py     (WS-CHARTS)
    classify_pending      rectifi/agent/classify.py   (WS-CLASSIFY)

The three functions below import their owner's module only when called, so this file loads without langchain and without
the workstreams being finished. The supervisor and the routes import from here, never from the owner's module directly.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Callable, TypedDict

if TYPE_CHECKING:  # pragma: no cover
    from langgraph.graph.state import CompiledStateGraph


# One line of the trace the UI shows for a RAG or chart run: which step, what it decided, and a short plain-words detail.
TraceEvent = TypedDict("TraceEvent", {"step": str, "verdict": str, "detail": str})


class RagInput(TypedDict):
    question: str
    mode: str           # "auto" | "rag" | "crag" | "self_rag" | "full" (the answer style). Any other word means auto.
    history: list       # earlier messages, already masked
    uid: str            # "owner", or the Tailscale / login subject


class RagOutput(TypedDict):
    answer: str
    citations: list[dict]      # [{"n": int, "corpus": "guide"|"note"|"txn"|"upload"|"web"|"tool", "label": str, "source": str, "title": str, "chunk": int, "quote": str}]
    # The graph's result also carries three extra keys the chat uses: route ("retrieve" | "direct" | "tools"), mode (the style
    # actually used) and abstained (True when it declined or found nothing instead of guessing).
    trace: list[TraceEvent]
    unverified: list[str]      # sentences or ₹ figures the answer could not back with a citation
    support: str | None        # Self-RAG grade: "full" | "partial" | "none" | None when not graded
    useful: bool | None


class ChartInput(TypedDict):
    request: str
    period: str         # "2026-10", "last-30-days", "all" ... whatever tools.resolve_period understands
    review: bool        # True: show the draft to the person before saving


class ChartOutput(TypedDict):
    chart_id: int | None
    ok: bool
    trace: list[TraceEvent]
    error: str | None


def _owner(module: str, name: str) -> Callable[..., Any]:
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise NotImplementedError(f"{module}.{name} is not built yet ({exc}).") from exc


def build_rag_subgraph(*, llms: dict, embeddings, checkpointer=None, store=None) -> "CompiledStateGraph":
    """The CRAG + Self-RAG graph. `llms` maps a role name ("grade", "chat") to a chat model. Input RagInput, output RagOutput."""
    return _owner("rectifi.rag.graph", "build_rag_subgraph")(llms=llms, embeddings=embeddings, checkpointer=checkpointer, store=store)


def build_chart_subgraph(*, llm, checkpointer=None) -> "CompiledStateGraph":
    """The chart harness graph: prepare, write, validate, run, check, repair. Input ChartInput, output ChartOutput."""
    return _owner("rectifi.charts.graph", "build_chart_subgraph")(llm=llm, checkpointer=checkpointer)


def classify_pending(batch_id: int | None, *, llm=None, progress=None) -> dict:
    """Run the model layer over the payments still waiting (one import batch, or all when batch_id is None). `progress` is a
    callable(done, total). Returns {"total", "done", "by_source", "status", "model", "error"}. Writes only through
    tools.apply_classifications (CLAUDE.md rule 14)."""
    return _owner("rectifi.agent.classify", "classify_pending")(batch_id, llm=llm, progress=progress)
