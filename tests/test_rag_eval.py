"""The eval runner computes its metrics correctly on a scripted run. (Real model quality is only measured with --live.)"""

import json
from collections import Counter

from langchain_core.messages import AIMessage

from rag_support import RuleModel
from rectifi import tools
from rectifi.agent.graph import READ_ONLY_NAMES
from rectifi.evals import rag as ev
from rectifi.rag import index
from rectifi.rag.graph import build_rag_subgraph
from support.fakes import BowEmbeddings


def test_question_file_has_the_planned_mix_and_valid_gold():
    qs = ev.load_questions()
    assert len(qs) == 30 and len({q["id"] for q in qs}) == 30
    assert Counter(q["kind"] for q in qs) == {"guide": 12, "data": 6, "out_of_scope": 6, "adversarial": 6}
    guides = {g["source"] for g in tools.rag_guide_files()}
    for q in qs:
        assert q["question"].strip() and "expect_abstain" in q and isinstance(q["forbid"], list) and isinstance(q["must_any"], list)
        if q["kind"] == "guide":
            assert q["gold_sources"] and set(q["gold_sources"]) <= guides
        if q["kind"] == "data":
            assert q["gold_tools"] and set(q["gold_tools"]) <= set(READ_ONLY_NAMES) and q["expect_route"] == "tools"
        if q["kind"] == "out_of_scope":
            assert q["expect_abstain"] is True and q["expect_route"] == "decline"
    assert json.loads(ev.QUESTIONS_PATH.read_text(encoding="utf-8"))["about"]


def scripted_graph():
    tools.add_transaction("250", "debit", "today", payee="Swiggy", category="Food & Dining", source="manual")
    emb = BowEmbeddings()
    index.reindex(emb, embed_model="bow", corpora=("guide",))

    def write(text):
        if text.startswith("You are Rectifi. Answer the question using the tools"):
            if "by_category" in text:
                return AIMessage("You spent ₹250.00 on food this month.")
            return AIMessage("", tool_calls=[{"name": "spending_report", "args": {}, "id": "c1"}])
        return "Keep three to six months of basic expenses [1]."

    def route(text):
        return {"route": "decline" if "capital" in text.lower() else "retrieve", "reason": "scripted"}

    grade = RuleModel(structured={
        "DocGrade": lambda t: {"relevance": "yes" if ("months" in t.split("Passage:")[-1].lower()) else "no"},
        "GroundCheck": lambda t: {"supported": "fully", "unsupported_claims": []},
        "UseCheck": lambda t: {"useful": True, "score": 5},
        "RouteDecision": route,
        "QueryList": lambda t: {"queries": ["emergency fund months"]},
    }, write="emergency fund months of expenses")
    chat = RuleModel(write=write)
    return build_rag_subgraph(llms={"grade": grade, "chat": chat}, embeddings=emb, embed_model="bow")


def test_scripted_run_computes_every_metric_for_every_mode():
    graph = scripted_graph()
    wanted = {"g02", "d01", "o01", "a01"}
    questions = [q for q in ev.load_questions() if q["id"] in wanted]
    result = ev.run_eval(graph, questions, modes=ev.MODES)
    assert set(result["summary"]) == set(ev.MODES) and len(result["records"]) == 16
    full = result["summary"]["full"]
    assert full["n"] == 4 and full["errors"] == 0
    assert full["routing"] == 1.0 and full["tool_hit"] == 1.0 and full["abstain"] == 1.0 and full["grounded"] == 1.0 and full["useful"] == 1.0
    assert full["hit@6"] == 1.0 and full["p50_s"] is not None and full["p95_s"] >= full["p50_s"]
    rag = result["summary"]["rag"]
    assert rag["routing"] is None and rag["tool_hit"] is None                      # plain RAG never routes, so these do not apply
    assert rag["abstain"] == 0.75                                                  # it answered the out-of-scope question instead of declining
    by_id = {(r["mode"], r["id"]): r for r in result["records"]}
    assert by_id[("full", "o01")]["abstained"] is True and by_id[("full", "d01")]["route"] == "tools"
    table = ev.format_report(result)
    assert "hit@6" in table and "full" in table and "prompt-based" in table


def test_judge_flags_an_untraced_figure_a_missing_word_and_a_forbidden_word():
    q = {"id": "x", "kind": "guide", "must_any": ["month"], "forbid": ["₹"], "expect_abstain": False, "expect_route": "retrieve"}
    base = {"error": None, "answer": "Three months.", "abstained": False, "unverified": [], "support": "full", "useful": True, "route": "retrieve"}
    assert ev.judge(dict(base), q, "full")["grounded"] is True
    assert ev.judge({**base, "unverified": ["₹5.00"]}, q, "full")["grounded"] is False
    assert ev.judge({**base, "answer": "Save ₹5"}, q, "full")["grounded"] is False
    assert ev.judge({**base, "answer": "Some words"}, q, "full")["useful_ok"] is False
    assert ev.judge({**base, "support": "partial"}, q, "full")["grounded"] is False
    assert ev.judge({**base, "support": "partial"}, q, "crag")["grounded"] is True        # crag has no grader to disagree
    assert ev.judge({**base, "abstained": True}, q, "full")["abstain_ok"] is False


def test_percentiles_and_a_failing_graph_is_recorded_not_raised():
    assert ev.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5 and ev.percentile([1, 2, 3, 4], 95) == 4 and ev.percentile([], 50) is None

    class Broken:
        def invoke(self, *a, **k):
            raise RuntimeError("model down")

    rec = ev.run_question(Broken(), {"id": "g", "kind": "guide", "question": "q"}, "full")
    assert rec["error"].startswith("RuntimeError") and rec["answer"] == ""


def test_cli_needs_live_flag(capsys):
    assert ev.main([]) == 2
    assert "--live" in capsys.readouterr().out
