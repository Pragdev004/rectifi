"""Fake RAG and chart subgraphs for the chat tests, built from the contract shapes in rectifi/agent/contracts.py.

They stand in for rectifi.rag.graph and rectifi.charts.graph until the integrator swaps in the real ones. They are test
helpers only: nothing under rectifi/ imports this file."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from rectifi.agent.contracts import ChartInput, ChartOutput, RagInput, RagOutput


def fake_rag(answer: str = "A rainy-day fund is money set aside for surprises.", *, ask_web: bool = False, seen: list | None = None):
    """RagInput in, RagOutput out. With ask_web=True it pauses once to ask for permission, like the real web-consent step."""

    def run(state: RagInput) -> RagOutput:
        if seen is not None:
            seen.append(dict(state))
        allowed = True
        if ask_web:
            allowed = bool(interrupt({"kind": "web_consent", "query": state["question"]}))
        text = answer if allowed else "I answered without the web: " + answer
        return {"answer": text, "citations": [{"source": "guides/budgeting.md", "title": "Budgeting", "chunk": 0, "quote": "set aside"}],
                "trace": [{"step": "retrieve", "verdict": "ok", "detail": "1 passage"}, {"step": "generate", "verdict": "attempt 1", "detail": ""}],
                "unverified": [], "support": "full", "useful": True}

    g = StateGraph(RagInput, output_schema=RagOutput)
    g.add_node("run", run)
    g.add_edge(START, "run")
    g.add_edge("run", END)
    return g.compile()


def fake_chart(chart_id: int | None = 7, *, ok: bool = True, review_first: bool = False, seen: list | None = None):
    def run(state: ChartInput) -> ChartOutput:
        if seen is not None:
            seen.append(dict(state))
        if review_first and state.get("review"):
            if not interrupt({"kind": "review_code", "code": "plt.bar([1], [2])"}):
                return {"chart_id": None, "ok": False, "trace": [], "error": "You cancelled the chart."}
        return {"chart_id": chart_id if ok else None, "ok": ok, "trace": [{"step": "write", "verdict": "ok", "detail": ""}],
                "error": None if ok else "The chart code failed its checks."}

    g = StateGraph(ChartInput, output_schema=ChartOutput)
    g.add_node("run", run)
    g.add_edge(START, "run")
    g.add_edge("run", END)
    return g.compile()
