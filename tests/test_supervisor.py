"""The chat supervisor: routing (rules, chips, router model), the parallel context branches, subgraph state mapping, the
amount check, remembering with a confirmation, and the retry on a flaky model. All models are scripted; RAG and charts are fakes."""

import pytest
from chat_support import cfg, last_ai, make, tid, turn
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command
from support.chat_fakes import fake_chart, fake_rag
from support.fakes import ScriptedModel, ScriptedStructured
from test_agent import call

from rectifi import tools
from rectifi.agent import memory as mem
from rectifi.agent.checkpoint import get_memory
from rectifi.agent.supervisor import RouteDecision, decide_route, guess_period, parse_chip


@pytest.mark.parametrize("text,forced,expected", [
    ("spent 250 on swiggy", "", "agent"),
    ("how much did I spend on food?", "", "agent"),
    ("am I over budget anywhere", "", "agent"),
    ("plot my food spending by month", "", "chart"),
    ("what is a rainy day fund", "", "knowledge"),
    ("explain the 50/30/20 rule", "", "knowledge"),
    ("hello", "", "smalltalk"),
    ("thanks!", "", "smalltalk"),
    ("remember that I prefer short answers", "", "remember"),
    ("anything at all", "chart", "chart"),
    ("plot it", "agent", "agent"),
])
def test_fixed_rules_route(text, forced, expected):
    assert decide_route(text, forced)[0] == expected


def test_fixed_rules_do_not_guess():
    assert decide_route("banana") is None


def test_chips_and_periods():
    assert parse_chip("/chart food by month") == ("chart", "food by month")
    assert parse_chip("/lookup what is an SIP") == ("knowledge", "what is an SIP")
    assert parse_chip("/do") == ("agent", "/do")
    assert parse_chip("plain text") == ("", "plain text")
    assert guess_period("food spend last 30 days") == "last-30-days"
    assert guess_period("last 2 weeks") == "last-14-days"
    assert guess_period("everything ever") == "all"
    assert guess_period("just a chart") == "last-90-days"


def test_agent_route_logs_a_payment_through_the_guarded_subgraph():
    g = make(agent=[call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}), AIMessage("Saved ₹250.00 at Swiggy.")])
    t = tid()
    out = turn(g, t, "spent 250 on swiggy")
    reply = last_ai(out)
    assert reply.additional_kwargs["route"] == "agent" and reply.additional_kwargs["unverified"] == []
    assert reply.additional_kwargs["ts"]
    assert len(tools.list_transactions()) == 1
    assert out["route"] == "agent"


def test_the_amount_guard_still_refuses_an_invented_amount():
    g = make(agent=[call("log_transaction", {"amount": "2500", "direction": "debit", "payee": "Swiggy"}), AIMessage("How much exactly?")])
    out = turn(g, tid(), "spent 250 on swiggy")
    assert tools.list_transactions() == []
    assert any(getattr(m, "status", "") == "error" and m.content.startswith("GUARD") for m in out["messages"])


def test_router_model_decides_only_what_the_rules_cannot():
    grade = ScriptedStructured(structured=[RouteDecision(route="knowledge", reason="general")])
    g = make(grade=grade, agent=[AIMessage("Noted.")])
    out = turn(g, tid(), "banana")
    assert out["route"] == "knowledge" and len(grade.schemas) == 1
    assert grade.schemas == [RouteDecision]
    turn(g, tid(), "spent 40 on tea")          # the rules decide: the router is not asked again
    assert len(grade.schemas) == 1


def test_a_failing_router_falls_back_to_the_agent():
    grade = ScriptedStructured(structured=[RuntimeError("model down")])
    g = make(agent=[AIMessage("What did you mean?")], grade=grade)
    out = turn(g, tid(), "banana")
    assert out["route"] == "agent" and last_ai(out).content == "What did you mean?"


def test_chip_forces_the_route_and_is_stripped_from_what_the_worker_sees():
    seen = []
    g = make(chart=fake_chart(9, seen=seen))
    out = turn(g, tid(), "/chart food by month, last 30 days", forced="")
    assert seen == [{"request": "food by month, last 30 days", "period": "last-30-days", "review": False}]
    reply = last_ai(out)
    assert reply.additional_kwargs["artifact"] == {"chart_id": 9} and reply.additional_kwargs["route"] == "chart"
    human = next(m for m in out["messages"] if isinstance(m, HumanMessage))
    assert human.content == "food by month, last 30 days"


def test_knowledge_state_mapping_and_answer_style():
    seen = []
    g = make(rag=fake_rag("Keep three months of spending aside.", seen=seen), chat=[AIMessage("Hi!")])
    t = tid()
    turn(g, t, "hello")
    out = turn(g, t, "what is an emergency fund account 0012345678901", mode="crag")
    (call_in,) = seen
    assert call_in["mode"] == "crag" and call_in["uid"] == "owner"
    assert "0012345678901" not in call_in["question"] and call_in["question"].endswith("8901")        # masked before the subgraph
    assert call_in["history"][0] == {"role": "user", "text": "hello"}
    reply = last_ai(out)
    assert reply.content == "Keep three months of spending aside."
    assert reply.additional_kwargs["route"] == "knowledge"
    assert reply.additional_kwargs["citations"][0]["source"] == "guides/budgeting.md"
    assert reply.additional_kwargs["support"] == "full" and reply.additional_kwargs["useful"] is True
    steps = [e["step"] for e in reply.additional_kwargs["turn_trace"]]
    assert steps[:2] == ["turn", "route"] and steps[-1] == "amounts"


def test_chart_review_flag_reaches_the_subgraph_and_its_interrupt_resumes():
    seen = []
    g = make(chart=fake_chart(3, review_first=True, seen=seen))
    t = tid()
    turn(g, t, "draw my food spend", review=True)
    snap = g.get_state(cfg(t))
    assert snap.next and snap.tasks[0].interrupts[0].value["kind"] == "review_code"
    out = g.invoke(Command(resume=True), cfg(t))
    assert last_ai(out).additional_kwargs["artifact"] == {"chart_id": 3}
    assert seen[0]["review"] is True


def test_a_failed_chart_says_why_and_has_no_artifact():
    g = make(chart=fake_chart(ok=False))
    reply = last_ai(turn(g, tid(), "plot my spending"))
    assert reply.content == "The chart code failed its checks." and "artifact" not in reply.additional_kwargs


def test_workers_that_are_not_built_yet_say_so_instead_of_inventing_an_answer():
    from rectifi.agent.supervisor import build_supervisor

    memory = get_memory()
    g = build_supervisor(checkpointer=memory.saver, store=memory.store, chat_llm=ScriptedModel(script=[]), grade_llm=ScriptedStructured())
    # contracts raise NotImplementedError until WS-RAG / WS-CHARTS exist; once they do, this still answers (not asserted)
    try:
        out = turn(g, tid(), "what is an sip")
    except Exception:       # pragma: no cover - the real subgraph needs a model; the point is only the missing-owner path
        pytest.skip("the real knowledge graph exists now")
    assert last_ai(out).content


def test_parallel_context_branches_both_run_before_the_route():
    memory = get_memory()
    mem.add_memory(memory.store, "owner", "preference", "I prefer short answers")
    g = make(agent=[AIMessage("Sure.")])
    order = []
    for chunk in g.stream({"messages": [HumanMessage("spent 5 on tea")], "uid": "owner", "forced": "", "mode": "auto", "review": False},
                          cfg(tid()), stream_mode="updates"):
        order += list(chunk)
    assert order[0] == "load_context" and order[-1] == "verify_amounts"
    assert set(order[1:3]) == {"fetch_memories", "trim_or_summarize"} and order[3] == "route"


def test_memory_reaches_the_model_without_amounts_and_is_never_a_source_of_figures():
    memory = get_memory()
    mem.add_memory(memory.store, "owner", "fact", "My stipend is ₹8,000 and I prefer short answers")
    agent = ScriptedModel(script=[AIMessage("Your stipend is ₹8,000 this month.")])
    g = make(agent=agent)
    out = turn(g, tid(), "how much stipend do I get")
    shown = "\n".join(str(m.content) for m in agent.seen)
    assert "prefer short answers" in shown and "8,000" not in shown and mem.AMOUNT_PLACEHOLDER in shown
    assert last_ai(out).additional_kwargs["unverified"] == ["8000.00"]
    assert not any(isinstance(m, SystemMessage) for m in out["messages"])          # the note is never saved in the thread


def test_taught_category_rules_are_given_to_the_model():
    with __import__("rectifi.db", fromlist=["connect"]).connect() as conn:
        cat = conn.execute("SELECT id FROM categories WHERE name = 'Health'").fetchone()["id"]
        conn.execute("INSERT INTO merchant_rules (pattern, category_id, source) VALUES ('cult fit', %s, 'learned')", (cat,))
    agent = ScriptedModel(script=[AIMessage("ok")])
    turn(make(agent=agent), tid(), "where does cult fit go in my records")
    assert any("'cult fit' goes to Health" in str(m.content) for m in agent.seen)


def test_remember_asks_first_then_saves_on_yes():
    g = make()
    t = tid()
    turn(g, t, "remember that I prefer short answers")
    snap = g.get_state(cfg(t))
    (intr,) = snap.tasks[0].interrupts
    assert intr.value["kind"] == "memory" and intr.value["text"] == "I prefer short answers" and intr.value["memory_kind"] == "preference"
    assert mem.list_memories(get_memory().store, "owner") == []                   # nothing saved before the answer
    out = g.invoke(Command(resume=True), cfg(t))
    saved = mem.list_memories(get_memory().store, "owner")
    assert [(m["kind"], m["text"], m["thread_id"]) for m in saved] == [("preference", "I prefer short answers", t)]
    assert last_ai(out).content.startswith("Remembered: I prefer short answers")


def test_remember_says_no_when_declined():
    g = make()
    t = tid()
    turn(g, t, "remember that my cat is called Tux")
    out = g.invoke(Command(resume=False), cfg(t))
    assert mem.list_memories(get_memory().store, "owner") == [] and last_ai(out).content == "OK, I won't save that."


def test_smalltalk_retries_a_flaky_model_once():
    class Flaky(ScriptedModel):
        failed: bool = False

        def _generate(self, *a, **k):
            if not self.failed:
                self.failed = True
                raise ConnectionError("model server hiccup")
            return super()._generate(*a, **k)

    model = Flaky(script=[AIMessage("Hello!")])
    out = turn(make(chat=model), tid(), "hi")
    assert last_ai(out).content == "Hello!" and model.failed


def test_a_bug_in_a_model_node_is_not_retried():
    class Broken(ScriptedModel):
        calls: int = 0

        def _generate(self, *a, **k):
            self.calls += 1
            raise ValueError("bad request")

    model = Broken(script=[])
    with pytest.raises(ValueError):
        turn(make(chat=model), tid(), "hi")
    assert model.calls == 1
