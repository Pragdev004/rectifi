"""Live checks against a real Ollama (skipped unless you run `pytest -m ollama`). Needs the chat, grade and embedding models pulled."""

import pytest

pytestmark = pytest.mark.ollama


def test_a_guide_question_is_answered_from_the_guides_with_every_mode():
    from rectifi.agent.llm import get_llm
    from rectifi.rag import index
    from rectifi.rag.embeddings import get_rag_embeddings
    from rectifi.rag.graph import build_rag_subgraph

    emb = get_rag_embeddings()
    index.reindex(emb, corpora=("guide",))
    graph = build_rag_subgraph(llms={"grade": get_llm("grade"), "chat": get_llm("chat")}, embeddings=emb)
    for mode in ("rag", "crag", "self_rag", "full"):
        out = graph.invoke({"question": "How many months of expenses should an emergency fund cover?", "mode": mode, "history": [], "uid": "live"},
                           {"configurable": {"thread_id": f"live-{mode}"}})
        assert out["answer"] and out["unverified"] == [], mode
        assert any(c["source"].startswith("guides/") for c in out["citations"]) or out["abstained"], mode
