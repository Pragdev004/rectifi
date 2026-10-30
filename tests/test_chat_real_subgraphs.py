"""The supervisor with the REAL knowledge (CRAG + Self-RAG) and chart subgraphs, built the way the app builds them
(`contracts.build_*` -> rectifi.rag.graph / rectifi.charts.graph). Only the models and the embeddings are scripted.
Proves the state keys, interrupt kinds and streamed nodes the chat window depends on."""

import pytest
from chat_support import cfg, last_ai, make, tid, turn
from charts_support import GOOD, reply, seed_spending
from langchain_core.messages import AIMessage
from langgraph.types import Command
from rag_support import RuleModel
from support.fakes import BowEmbeddings, ScriptedModel

from rectifi import config, tools
from rectifi.charts import graph as chart_graph
from rectifi.charts import sandbox
from rectifi.rag import index, web

ANSWER = "Keep three to six months of basic expenses [1]."


@pytest.fixture(autouse=True)
def real_workers(tmp_path, monkeypatch):
    """The embeddings and the models are scripted; everything else is the code that ships."""
    monkeypatch.setenv("RECTIFI_EMBED_MODEL", "bow")
    monkeypatch.setattr(sandbox, "CACHE_DIR", tmp_path / "mpl")
    monkeypatch.setattr(chart_graph, "RETRY_INTERVAL", 0)
    embeddings = BowEmbeddings()
    index.reindex(embeddings, embed_model=config.embed_model())
    monkeypatch.setattr("rectifi.agent.llm.get_embeddings", lambda: embeddings)
    seed_spending()
    return embeddings


def grader(doc="yes"):
    return RuleModel(
        structured={"DocGrade": lambda t: {"relevance": doc, "reason": "x"},
                    "GroundCheck": lambda t: {"supported": "fully", "unsupported_claims": []},
                    "UseCheck": lambda t: {"useful": True, "score": 5},
                    "RouteDecision": lambda t: {"route": "knowledge", "reason": "a guide question"},
                    "QueryList": lambda t: {"queries": ["emergency fund months"]}},
        write=lambda t: "emergency fund size in months")


def writer(text=ANSWER):
    return RuleModel(write=lambda t: text)


def test_the_real_knowledge_subgraph_answers_with_sources_and_checks(monkeypatch):
    g = make(real=True, grade=grader(), chat=writer())
    t = tid()
    out = turn(g, t, "How big should my emergency fund be?", mode="full", forced="knowledge")
    reply_ = last_ai(out)
    kw = reply_.additional_kwargs
    assert reply_.content == ANSWER and kw["route"] == "knowledge"
    assert kw["citations"] and {"n", "corpus", "label", "source"} <= set(kw["citations"][0])
    assert kw["support"] in ("full", "partial", "none") and kw["useful"] is True
    assert [e["step"] for e in kw["trace"]]                                # the graph's own "how this was checked" list
    assert kw["unverified_claims"] == [] and kw["abstained"] is False and kw["rag_mode"] == "full"


@pytest.mark.parametrize("style,expected", [("rag", "rag"), ("plain", "rag"), ("crag", "crag"), ("self_rag", "self_rag"), ("full", "full"), ("auto", "auto")])
def test_every_answer_style_reaches_the_graph(style, expected):
    g = make(real=True, grade=grader(), chat=writer())
    out = turn(g, tid(), "How big should my emergency fund be?", mode=style, forced="knowledge")
    assert last_ai(out).additional_kwargs["rag_mode"] == expected


def test_the_web_consent_pause_comes_up_through_the_supervisor_and_resumes(monkeypatch):
    sent = []
    monkeypatch.setattr(web, "available", lambda: True)
    monkeypatch.setattr(web, "search", lambda q: sent.append(q) or [])
    g = make(real=True, grade=grader(doc="no"), chat=writer())
    t = tid()
    turn(g, t, "How does a SIP work?", mode="crag", forced="knowledge")
    snap = g.get_state(cfg(t))
    (pause,) = [i for task in snap.tasks for i in task.interrupts]
    assert pause.value["type"] == "web_search" and "query" in pause.value and sent == []
    from rectifi.api.ask import interrupt_kind

    assert interrupt_kind(pause.value) == "web_consent"                    # the one word the chat window switches on
    out = g.invoke(Command(resume=True), cfg(t))
    assert sent == ["emergency fund size in months"] and last_ai(out).additional_kwargs["route"] == "knowledge"


@pytest.fixture
def code_model(monkeypatch):
    holder = {}
    monkeypatch.setattr("rectifi.agent.llm.get_llm", lambda role="chat": holder["model"])
    return holder


def test_the_real_chart_subgraph_draws_and_hands_back_an_artifact(code_model):
    code_model["model"] = ScriptedModel(script=[reply(GOOD)])
    g = make(real=True)
    out = turn(g, tid(), "plot my food spending by month, last 3 months")
    ai = last_ai(out)
    chart_id = ai.additional_kwargs["artifact"]["chart_id"]
    assert ai.additional_kwargs["route"] == "chart" and tools.get_chart(chart_id)["title"] == "Food & Dining spend by month"
    assert [e["step"] for e in ai.additional_kwargs["trace"]][:2] == ["write", "validate"]


def test_the_real_chart_review_pause_uses_the_review_code_kind_and_resumes(code_model):
    code_model["model"] = ScriptedModel(script=[reply(GOOD)])
    g = make(real=True)
    t = tid()
    turn(g, t, "draw my food spend by month", review=True)
    (pause,) = [i for task in g.get_state(cfg(t)).tasks for i in task.interrupts]
    assert pause.value["kind"] == "review_code" and "plt." in pause.value["code"]
    out = g.invoke(Command(resume=True), cfg(t))
    assert last_ai(out).additional_kwargs["artifact"]["chart_id"]


def test_cancelling_the_review_ends_without_a_chart(code_model):
    code_model["model"] = ScriptedModel(script=[reply(GOOD)])
    g = make(real=True)
    t = tid()
    turn(g, t, "draw my food spend by month", review=True)
    out = g.invoke(Command(resume=False), cfg(t))
    assert "artifact" not in last_ai(out).additional_kwargs


def test_a_chart_the_model_cannot_fix_says_why_instead_of_crashing(code_model):
    code_model["model"] = ScriptedModel(script=[reply("import os\nos.system('x')\n")] * 3)
    out = turn(make(real=True), tid(), "plot my spending")
    ai = last_ai(out)
    assert "artifact" not in ai.additional_kwargs and ai.content
