"""The CRAG + Self-RAG graph, driven by rule-based fake models (no Ollama, no network)."""

import itertools
import threading
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from rag_support import RuleModel
from rectifi import config, tools
from rectifi.agent.graph import READ_ONLY_TOOLS
from rectifi.db import connect
from rectifi.rag import graph as rg
from rectifi.rag import index, web
from rectifi.rag.graph import DECLINE_TEXT, NOT_FOUND_TEXT, WITHHELD_TEXT, build_rag_subgraph, normalize_mode
from support.fakes import BowEmbeddings

Q = "How big should my emergency fund be?"
ANSWER = "Keep three to six months of basic expenses [1]."


def passage(text: str) -> str:
    return text.split("Passage:")[-1].lower()


def tail(text: str, marker: str) -> str:
    return text.split(marker)[-1]


@pytest.fixture
def emb():
    e = BowEmbeddings()
    tools.add_transaction("1499", "debit", "2026-08-14", payee="CULT FIT", category="Health", note="gym membership", source="manual")
    index.reindex(e, embed_model="bow")
    return e


class Rig:
    """One graph plus its two fake models. Override the graders by passing fns; defaults say the 'months' passage is relevant."""

    def __init__(self, emb, *, doc=None, ground=None, use=None, route=None, write=None, rewrite=None, chat_script=None, **kw):
        self.rewritten = threading.Event()
        self.rewrite_text = rewrite or "emergency fund size in months"
        grade = {
            "DocGrade": doc or (lambda t: {"relevance": "yes" if ("months" in passage(t) or "emergency" in passage(t)) else "no", "reason": "x"}),
            "GroundCheck": ground or (lambda t: {"supported": "fully", "unsupported_claims": []}),
            "UseCheck": use or (lambda t: {"useful": True, "score": 5}),
            "RouteDecision": route or (lambda t: {"route": "retrieve", "reason": "a guide question"}),
            "QueryList": lambda t: {"queries": ["emergency fund months", "how many months of expenses to save"]},
        }

        def grade_write(text):                       # the grade model also writes the rewritten query
            self.rewritten.set()
            return self.rewrite_text

        self.grade = RuleModel(structured=grade, write=grade_write)
        self.chat = RuleModel(write=write or (lambda t: ANSWER), messages_script=list(chat_script or []))
        self.graph = build_rag_subgraph(llms={"grade": self.grade, "chat": self.chat}, embeddings=emb, embed_model="bow", **kw)

    def run(self, question=Q, mode="full", thread=None, history=None, resume=None):
        cfg = {"configurable": {"thread_id": thread or uuid.uuid4().hex}}
        self.cfg = cfg
        visited = []
        payload = Command(resume=resume) if resume is not None else {"question": question, "mode": mode, "history": history or [], "uid": "owner"}
        for update in self.graph.stream(payload, cfg, stream_mode="updates"):
            visited.extend(k for k in update if not k.startswith("__"))
        state = self.graph.get_state(cfg)
        self.state = state
        return visited, state.values, [i for t in state.tasks for i in t.interrupts]

    def writes(self):
        return [e for e in self.chat.log if e[0] == "write"]


# ───────────── modes ─────────────

def test_modes_matrix(emb):
    plain = {"decide_retrieval", "retrieve", "grade_join", "generate", "finalize"}
    expect = {
        "rag": (plain, {"multi_query", "grade_one", "refine", "check_support", "check_useful", "rewrite_query"}),
        "crag": (plain | {"multi_query", "grade_one", "refine"}, {"check_support", "check_useful"}),
        "self_rag": (plain | {"grade_one", "check_support", "check_useful", "reflect_hub"}, {"multi_query", "refine", "rewrite_query"}),
        "full": (plain | {"multi_query", "grade_one", "refine", "check_support", "check_useful", "reflect_hub"}, {"rewrite_query"}),
        "auto": (plain | {"multi_query", "grade_one", "refine", "check_support", "check_useful", "reflect_hub"}, {"rewrite_query"}),
    }
    for mode, (must, must_not) in expect.items():
        rig = Rig(emb)
        visited, values, _ = rig.run(mode=mode)
        assert must <= set(visited), (mode, must - set(visited))
        assert not (must_not & set(visited)), (mode, must_not & set(visited))
        assert values["answer"].startswith("Keep three to six months")
        assert values["mode"] == mode
    rig = Rig(emb)
    rig.run(mode="rag")
    assert rig.grade.log == []                                    # plain RAG never calls a grader or the router
    rig = Rig(emb)
    rig.run(mode="crag")
    assert not rig.grade.calls("structured", "RouteDecision")     # CRAG always retrieves: no router either
    rig = Rig(emb)
    rig.run(mode="self_rag")
    assert len(rig.grade.calls("structured", "RouteDecision")) == 1


def test_normalize_mode():
    assert [normalize_mode(m) for m in ("Self-RAG", "CRAG", "plain", "library", "off", None, "full")] == ["self_rag", "crag", "rag", "auto", "auto", "auto", "full"]


def test_output_has_the_contract_fields_and_citations(emb):
    rig = Rig(emb)
    out = rig.graph.invoke({"question": Q, "mode": "full", "history": [], "uid": "owner"}, {"configurable": {"thread_id": "t-out"}})
    assert {"answer", "citations", "trace", "unverified", "support", "useful"} <= set(out)
    assert out["support"] == "full" and out["useful"] is True and out["unverified"] == [] and out["route"] == "retrieve"
    cite = out["citations"][0]
    assert cite["n"] == 1 and cite["label"] == "GUIDE" and cite["source"].startswith("guides/") and "quote_full" not in cite
    assert [e["step"] for e in out["trace"]][:2] == ["ROUTED", "EXPANDED"] and out["trace"][-1]["step"] in ("AMOUNTS", "USEFUL")
    assert all(set(e) == {"step", "verdict", "detail"} for e in out["trace"])


def test_trace_reducer_accumulates_and_a_second_turn_starts_fresh(emb):
    rig = Rig(emb)
    _, first, _ = rig.run(thread="same")
    steps = [e["step"] for e in first["trace"]]
    assert steps.count("RETRIEVED") == 1 and {"ROUTED", "GRADED", "CRAG", "GENERATED", "SUPPORTED", "USEFUL", "AMOUNTS"} <= set(steps)
    _, second, _ = rig.run(thread="same")
    assert [e["step"] for e in second["trace"]].count("RETRIEVED") == 1 and len(second["trace"]) == len(first["trace"])


def test_grades_fan_out(emb):
    rig = Rig(emb)
    visited, values, _ = rig.run(mode="crag")
    n = len(values["documents"])
    assert n >= 4 and visited.count("grade_one") == n
    assert len(rig.grade.calls("structured", "DocGrade")) == n
    assert sorted(g["idx"] for g in values["grades"]) == list(range(n))          # one grade per passage, none lost


def test_generate_chain_masks_history_and_question(emb):
    rig = Rig(emb)
    history = [{"role": "user", "content": "my account 123456789012 is low"}, {"role": "assistant", "content": "ok"}]
    rig.run(question="How big should my emergency fund be? card 4111 1111 1111 1111", history=history)
    seen = " ".join(e[-1] for e in rig.log_all())
    assert "123456789012" not in seen and "4111 1111 1111 1111" not in seen and "9012" in seen
    assert rig.writes()[0][1].startswith("You are Rectifi's helper")


Rig.log_all = lambda self: [*self.grade.log, *self.chat.log]


# ───────────── CRAG ─────────────

def test_crag_branches(emb):
    # correct: a passage answers it, so refine and no second search
    rig = Rig(emb)
    visited, values, _ = rig.run(mode="crag")
    assert values["crag"] == "correct" and "refine" in visited and "rewrite_query" not in visited and values["retrieval_rounds"] == 1

    # ambiguous: related but not answering -> rewrite and search again; the second search finds an answer
    def partial_then_yes(t):
        if rig2.rewritten.is_set():
            return {"relevance": "yes" if "months" in passage(t) else "no"}
        return {"relevance": "partial"}

    rig2 = Rig(emb, doc=partial_then_yes)
    visited, values, _ = rig2.run(mode="crag")
    assert visited.count("retrieve") == 2 and "rewrite_query" in visited and values["crag"] == "correct"
    assert any(e["detail"] == "Asked again as: emergency fund size in months" for e in values["trace"])

    # incorrect: nothing relevant -> rewrite, search again (broader), then answer
    def no_then_yes(t):
        return {"relevance": "yes" if rig3.rewritten.is_set() and "months" in passage(t) else "no"}

    rig3 = Rig(emb, doc=no_then_yes, rewrite="three to six months of your basic expenses emergency fund")
    visited, values, _ = rig3.run(mode="crag")
    assert visited.count("retrieve") == 2 and [e["verdict"] for e in values["trace"] if e["step"] == "CRAG"] == ["INCORRECT", "CORRECT"]
    broader = [e for e in values["trace"] if e["step"] == "RETRIEVED"]
    assert [e["detail"] for e in broader] == ["first search", "broader search"]


def test_crag_incorrect_forever_stops_at_the_bound_and_says_so(emb):
    rig = Rig(emb, doc=lambda t: {"relevance": "no"})
    visited, values, _ = rig.run(mode="crag")
    assert visited.count("retrieve") == 1 + config.RAG_MAX_RETRIEVE
    assert "generate" not in visited and values["answer"] == NOT_FOUND_TEXT and values["abstained"] is True
    assert rig.chat.log == []                                      # the answer model never ran


def test_ambiguous_forever_is_refined_and_answered_after_the_bound(emb):
    rig = Rig(emb, doc=lambda t: {"relevance": "partial"})
    visited, values, _ = rig.run(mode="crag")
    assert visited.count("retrieve") == 1 + config.RAG_MAX_RETRIEVE and "refine" in visited and "generate" in visited
    assert values["crag"] == "ambiguous" and values["answer"]


def test_retrieve_bound_follows_config(emb, monkeypatch):
    monkeypatch.setattr(config, "RAG_MAX_RETRIEVE", 0)
    rig = Rig(emb, doc=lambda t: {"relevance": "no"})
    visited, values, _ = rig.run(mode="crag")
    assert visited.count("retrieve") == 1 and values["answer"] == NOT_FOUND_TEXT


def test_refine_drops_irrelevant_strips(emb):
    text = ("# Xylophone notes\nMy xylophone repair costs are explained here. Bananas are yellow fruit. "
            "Clouds drift over the quiet hills.")
    tools.rag_upsert_document("upload", "x.md", "Xylophone notes", text)
    index.reindex(emb, embed_model="bow", corpora=("upload",))
    rig = Rig(emb, doc=lambda t: {"relevance": "yes" if "xylophone" in passage(t) else "no"}, write=lambda t: "It is explained in your notes [1].")
    visited, values, _ = rig.run(question="xylophone repair", mode="crag")
    kept = [d for d in values["kept"] if "xylophone" in d.page_content.lower()]
    assert kept and kept[0].metadata["refined"] is True
    assert "Bananas" not in kept[0].page_content and "Clouds" not in kept[0].page_content and "xylophone repair" in kept[0].page_content
    assert any(e["step"] == "REFINED" for e in values["trace"])


# ───────────── Self-RAG ─────────────

def test_regen_bounded_and_strict_prompt_lists_the_claims(emb):
    rig = Rig(emb, ground=lambda t: {"supported": "partially", "unsupported_claims": ["twelve months is enough"]})
    visited, values, _ = rig.run(mode="self_rag")
    assert visited.count("generate") == 1 + config.RAG_MAX_REGENERATE
    second_prompt = rig.writes()[1][1]
    assert "twelve months is enough" in second_prompt and "Write the answer again" in second_prompt
    assert "Write the answer again" not in rig.writes()[0][1]
    assert values["answer"].startswith("Keep three") and "Not fully supported" in values["answer"] and "twelve months is enough" in values["answer"]
    out = rig.graph.get_state(rig.cfg).values
    assert out["support"] == "partial"


def test_regen_succeeds_when_the_second_try_is_supported(emb):
    calls = itertools.count()
    rig = Rig(emb, ground=lambda t: {"supported": "no" if next(calls) == 0 else "fully", "unsupported_claims": []})
    visited, values, _ = rig.run(mode="self_rag")
    assert visited.count("generate") == 2 and values["support"] == "full" and "Not fully supported" not in values["answer"]


def test_not_useful_rewrites_and_re_retrieves_bounded(emb):
    rig = Rig(emb, use=lambda t: {"useful": False, "score": 1})
    visited, values, _ = rig.run(mode="self_rag")
    assert visited.count("retrieve") == 1 + config.RAG_MAX_RETRIEVE
    assert visited.count("rewrite_query") == config.RAG_MAX_RETRIEVE
    assert values["useful"] is False and values["answer"].startswith("Keep three")
    assert values["retrieval_rounds"] == 1 + config.RAG_MAX_RETRIEVE


def test_loop_bounds_follow_config_at_run_time(emb, monkeypatch):
    monkeypatch.setattr(config, "RAG_MAX_REGENERATE", 0)
    monkeypatch.setattr(config, "RAG_MAX_RETRIEVE", 0)
    rig = Rig(emb, ground=lambda t: {"supported": "no", "unsupported_claims": []})
    visited, _, _ = rig.run(mode="self_rag")
    assert visited.count("generate") == 1
    rig = Rig(emb, use=lambda t: {"useful": False, "score": 1})
    visited, _, _ = rig.run(mode="self_rag")
    assert visited.count("retrieve") == 1


def test_self_rag_with_nothing_relevant_says_so_without_correcting(emb):
    rig = Rig(emb, doc=lambda t: {"relevance": "no"})
    visited, values, _ = rig.run(mode="self_rag")
    assert visited.count("retrieve") == 1 and "rewrite_query" not in visited and values["answer"] == NOT_FOUND_TEXT


# ───────────── the rupee check (rule 1) ─────────────

def test_rupee_check_fails_an_invented_figure_before_the_llm_grader_runs(emb):
    rig = Rig(emb, write=lambda t: "Save about ₹5,000 a month [1].")
    visited, values, _ = rig.run(mode="full")
    assert rig.grade.calls("structured", "GroundCheck") == []              # the model grader never ran
    assert visited.count("generate") == 1 + config.RAG_MAX_REGENERATE      # forced regeneration, bounded
    assert values["answer"] == WITHHELD_TEXT and values["unverified"] == ["₹5,000.00"] and values["support"] == "none"
    assert any(e["step"] == "AMOUNTS" and e["verdict"] == "NOT TRACED" for e in values["trace"])
    assert "₹5,000" not in values["answer"]


def test_regeneration_can_fix_a_bad_figure(emb):
    replies = itertools.count()
    rig = Rig(emb, write=lambda t: "Save ₹5,000 a month [1]." if next(replies) == 0 else ANSWER)
    visited, values, _ = rig.run(mode="full")
    assert visited.count("generate") == 2 and values["answer"] == ANSWER and values["unverified"] == []
    assert len(rig.grade.calls("structured", "GroundCheck")) == 1          # only the clean second answer reached the grader


def test_figures_inside_a_cited_entry_are_allowed_but_a_cited_file_is_not(emb):
    tools.rag_upsert_document("upload", "price.md", "Price list", "# Price list\nThe plan costs ₹9,999 a year, says the shop.")
    index.reindex(emb, embed_model="bow", corpora=("upload",))

    def figure(label, amount):
        def write(t):
            n = next(l for l in t.splitlines() if l.startswith("[") and label in l)[1]
            return f"It was {amount} [{n}]."
        return write

    both = lambda t: {"relevance": "yes" if ("cult fit" in passage(t) or "9,999" in passage(t)) else "no"}
    rig = Rig(emb, doc=both, write=figure("YOUR ENTRY", "₹1,499.00"))
    _, values, _ = rig.run(question="cult fit membership price", mode="full")
    assert values["unverified"] == [] and "₹1,499.00" in values["answer"] and values["citations"][0]["label"] == "YOUR ENTRY"
    rig = Rig(emb, doc=both, write=figure("YOUR FILE", "₹9,999"))
    _, values, _ = rig.run(question="plan price shop", mode="full")
    assert values["answer"] == WITHHELD_TEXT and values["unverified"] == ["₹9,999.00"]       # files are not an allowed source of figures


def test_figure_the_user_typed_is_allowed(emb):
    rig = Rig(emb, write=lambda t: "If you save ₹500 a month [1], you reach three months soon.")
    _, values, _ = rig.run(question="If I save ₹500 a month, how big should my emergency fund be?", mode="full")
    assert values["unverified"] == [] and "₹500" in values["answer"]


def test_plain_and_crag_modes_still_report_untraced_figures(emb):
    for mode in ("rag", "crag"):
        rig = Rig(emb, write=lambda t: "Save ₹5,000 [1].")
        _, values, _ = rig.run(mode=mode)
        assert values["unverified"] == ["₹5,000.00"] and "₹5,000" in values["answer"]        # shown, but flagged for the UI


# ───────────── routes ─────────────

def test_tools_route_uses_toolnode_and_read_only_tools(emb):
    tools.add_transaction("250", "debit", "today", payee="Swiggy", category="Food & Dining", source="manual")
    call = AIMessage("", tool_calls=[{"name": "spending_report", "args": {"period": "this_month"}, "id": "c1"}])
    rig = Rig(emb, chat_script=[call, AIMessage("You spent ₹250.00 this month, all on Food & Dining.")])
    visited, values, _ = rig.run(question="how much did I spend on food this month?", mode="full")
    assert visited[:2] == ["decide_retrieval", "tools_answer"] and "retrieve" not in visited
    assert rig.chat.bound == [t.name for t in READ_ONLY_TOOLS]
    assert not {"log_transaction", "log_sms", "set_budget", "recategorize", "save_commitment"} & set(rig.chat.bound)
    assert "250.00" in rig.writes()[1][1]           # the ToolMessage went back to the model
    assert values["route"] == "tools" and values["unverified"] == [] and values["citations"][0]["source"] == "tool:spending_report"
    assert [e["verdict"] for e in values["trace"] if e["step"] == "ROUTED"] == ["use your numbers"]
    assert values["support"] == "full" and not rig.grade.calls("structured", "RouteDecision")   # a fixed rule routed it


def test_a_write_tool_is_impossible_on_the_tools_route(emb):
    call = AIMessage("", tool_calls=[{"name": "log_transaction", "args": {"amount": "500", "direction": "debit"}, "id": "c1"}])
    rig = Rig(emb, chat_script=[call, AIMessage("I could not record that.")])
    rig.run(question="how much did I spend this month, and log 500 for me", mode="full")
    assert len(tools.list_transactions(limit=10)) == 1             # only the entry the fixture added
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE amount = 500").fetchone()["n"] == 0
    assert "not a valid tool" in rig.writes()[1][1]


def test_tools_answer_with_an_invented_figure_is_withheld(emb):
    tools.add_transaction("250", "debit", "today", payee="Swiggy", category="Food & Dining", source="manual")
    call = AIMessage("", tool_calls=[{"name": "spending_report", "args": {}, "id": "c1"}])
    rig = Rig(emb, chat_script=[call, AIMessage("You spent ₹9,999.00 this month.")])
    _, values, _ = rig.run(question="how much did I spend this month?", mode="full")
    assert values["answer"] == WITHHELD_TEXT and values["unverified"] == ["₹9,999.00"] and rig.grade.calls("structured", "GroundCheck") == []


def test_direct_and_decline_routes(emb):
    rig = Rig(emb, write=lambda t: "You're welcome!")
    visited, values, _ = rig.run(question="thanks!", mode="auto")
    assert "generate_direct" in visited and "retrieve" not in visited and values["answer"] == "You're welcome!" and values["support"] is None
    rig = Rig(emb, route=lambda t: {"route": "decline", "reason": "geography"})
    visited, values, _ = rig.run(question="what is the capital of France", mode="auto")
    assert visited == ["decide_retrieval", "decline", "finalize"] and values["answer"] == DECLINE_TEXT and values["abstained"] is True
    assert rig.chat.log == []


def test_router_trouble_falls_back_to_looking_it_up(emb):
    rig = Rig(emb, route=lambda t: RuntimeError("router down"))
    visited, values, _ = rig.run(mode="self_rag")
    assert "retrieve" in visited and values["route"] == "retrieve"


# ───────────── web search consent ─────────────

@pytest.fixture
def web_on(monkeypatch):
    seen = []
    monkeypatch.setattr(web, "available", lambda: True)
    monkeypatch.setattr(web, "search", lambda q: seen.append(q) or [])
    return seen


def test_web_consent_interrupt_resume_false_means_no_search(emb, web_on):
    rig = Rig(emb, doc=lambda t: {"relevance": "no"})
    visited, values, interrupts = rig.run(question="How does a SIP work?", mode="crag", thread="web-no")
    assert interrupts and interrupts[0].value["type"] == "web_search"
    assert "emergency fund size in months" in interrupts[0].value["message"] and "Nothing about your money" in interrupts[0].value["message"]
    assert "web_search" not in visited and web_on == []
    visited2, values, interrupts = rig.run(mode="crag", thread="web-no", resume=False)
    assert "web_search" not in visited2 and web_on == [] and not interrupts
    assert values["answer"] == NOT_FOUND_TEXT and any(e["step"] == "WEB" and e["verdict"] == "declined" for e in values["trace"])


def test_web_consent_resume_true_sends_only_the_rewritten_query(emb, web_on):
    rig = Rig(emb, doc=lambda t: {"relevance": "no"}, rewrite="how does a SIP work")
    rig.run(question="How does a SIP work? my card 4111 1111 1111 1111", mode="crag", thread="web-yes")
    visited, values, _ = rig.run(mode="crag", thread="web-yes", resume=True)
    assert web_on == ["how does a SIP work"] and "web_search" in visited


def test_web_is_never_offered_for_personal_questions_or_when_off(emb, web_on, monkeypatch):
    rig = Rig(emb, doc=lambda t: {"relevance": "no"})
    _, _, interrupts = rig.run(question="what did I spend on food last month?", mode="crag")
    assert not interrupts and web_on == []
    monkeypatch.setattr(web, "available", lambda: False)
    _, _, interrupts = rig.run(question="How does a SIP work?", mode="crag")
    assert not interrupts and web_on == []


def test_web_not_offered_when_a_private_passage_was_relevant(emb, web_on):
    rig = Rig(emb, doc=lambda t: {"relevance": "partial" if "cult fit" in passage(t) else "no"})
    _, values, interrupts = rig.run(question="cult fit", mode="crag")
    assert not interrupts and web_on == [] and values["crag"] == "ambiguous"
