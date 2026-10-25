"""Short-term memory (trim, summarise) and long-term memory (the Store): limits, privacy, and "memory never supplies a number"."""

import pytest
from chat_support import cfg, last_ai, make, tid, turn
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from support.fakes import ScriptedModel

from rectifi import config
from rectifi.agent import memory as mem
from rectifi.agent.checkpoint import get_memory
from rectifi.agent.graph import unverified_amounts


def convo(turns: int, size: int = 40):
    out = []
    for i in range(turns):
        out += [HumanMessage(f"question {i} " + "x" * size, id=f"h{i}"), AIMessage(f"answer {i} " + "y" * size, id=f"a{i}")]
    return out


# ───────────── short-term memory ─────────────

def test_trim_fits_the_budget_and_starts_on_a_user_message():
    messages = convo(30, size=400)
    kept = mem.trim_for_context(messages, max_tokens=500)
    assert isinstance(kept[0], HumanMessage) and kept[-1] is messages[-1]
    assert mem.approx_tokens(kept) <= 500 and len(kept) < len(messages)


def test_trim_never_separates_a_tool_result_from_its_call():
    call = AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": "c1", "type": "tool_call"}], id="ai1")
    messages = [HumanMessage("old " * 200, id="h0"), AIMessage("older " * 200, id="a0"),
                HumanMessage("new", id="h1"), call, ToolMessage("result", tool_call_id="c1", name="t", id="t1"), AIMessage("done", id="a1")]
    kept = mem.trim_for_context(messages, max_tokens=60)
    assert [m.id for m in kept] == ["h1", "ai1", "t1", "a1"]


def test_the_newest_question_is_kept_even_when_it_alone_is_too_big():
    messages = [HumanMessage("big " * 5000, id="h0")]
    assert mem.trim_for_context(messages, max_tokens=50) == messages


def test_the_budget_follows_the_context_window(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "4096")
    assert mem.token_budget() == int(4096 * mem.CONTEXT_SHARE)


def test_short_chats_are_not_summarised():
    plan = mem.plan_window(convo(5), summary="", summarised_to=0, llm=None)
    assert plan == {"window": 10, "summary": "", "summarised_to": 0}


def test_long_chats_are_summarised_without_amounts_and_the_window_shrinks():
    messages = convo(16)
    model = ScriptedModel(script=[AIMessage("They logged lunch. Total was ₹1,250 and rent Rs. 9000.")])
    plan = mem.plan_window(messages, summary="", summarised_to=0, llm=model)
    cut = mem.summary_cut(messages)
    assert plan["summarised_to"] == cut and plan["window"] == len(messages) - cut
    assert "1,250" not in plan["summary"] and "9000" not in plan["summary"] and mem.AMOUNT_PLACEHOLDER in plan["summary"]
    again = mem.plan_window(messages, summary=plan["summary"], summarised_to=cut, llm=ScriptedModel(script=[]))   # nothing new: no call
    assert again["summary"] == plan["summary"] and again["window"] == plan["window"]


def test_summary_input_is_masked():
    model = ScriptedModel(script=[AIMessage("ok")])
    older = [HumanMessage("my account 0012345678901 is low", id="h0"), AIMessage("noted", id="a0")]
    mem.summarise(model, older)
    assert "0012345678901" not in " ".join(str(m.content) for m in model.seen)


def test_a_failed_summary_does_not_fail_the_chat():
    class Down(ScriptedModel):
        def _generate(self, *a, **k):
            raise ConnectionError("down")

    plan = mem.plan_window(convo(16), summary="", summarised_to=0, llm=Down(script=[]))
    assert plan["summary"] == "" and plan["window"] == 32


def test_the_summary_node_keeps_the_full_checkpoint():
    grade_model = ScriptedModel(script=[AIMessage("Greetings and small talk.")])
    g = make(chat=[AIMessage(f"hello {i}") for i in range(16)], grade=grade_model)
    t = tid()
    for i in range(16):
        out = turn(g, t, "hi")
    assert len(out["messages"]) == 32                                             # nothing was deleted from the thread
    assert out["summary"] == "Greetings and small talk." and out["summarised_to"] > 0
    assert out["window"] < 32
    assert len(g.get_state(cfg(t)).values["messages"]) == 32


def test_a_long_chat_still_fits_a_small_window(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "2048")
    g = make(agent=[AIMessage("ok " * 100) for _ in range(20)], grade=ScriptedModel(script=[AIMessage("sum") for _ in range(20)]))
    t = tid()
    for i in range(20):
        out = turn(g, t, "spent 5 on tea " + "word " * 120)
    shown = out["messages"][:-1][-out["window"]:]          # without the reply that was added after the window was decided
    assert mem.approx_tokens(shown) <= mem.token_budget() and out["window"] < len(out["messages"])
    assert isinstance(shown[0], HumanMessage)


# ───────────── long-term memory ─────────────

def test_store_crud_and_namespaces_are_separate():
    store = get_memory().store
    a = mem.add_memory(store, "owner", "preference", "I prefer short answers", "thread-1")
    mem.add_memory(store, "someone-else", "fact", "I live in a hostel")
    assert [m["text"] for m in mem.list_memories(store, "owner")] == ["I prefer short answers"]
    assert [m["text"] for m in mem.list_memories(store, "someone-else")] == ["I live in a hostel"]
    changed = mem.update_memory(store, "owner", a["id"], text="I prefer very short answers", kind="fact")
    assert changed["text"] == "I prefer very short answers" and changed["kind"] == "fact" and changed["created_at"] == a["created_at"]
    assert mem.update_memory(store, "someone-else", a["id"], text="nope") is None          # not theirs
    assert mem.delete_memory(store, "owner", a["id"]) is True and mem.delete_memory(store, "owner", a["id"]) is False
    assert mem.forget_all(store, "someone-else") == 1 and mem.list_memories(store, "someone-else") == []


def test_forget_all_clears_more_than_one_page():
    store = get_memory().store
    for i in range(130):
        mem.add_memory(store, "owner", "fact", f"thing number {i}")
    assert mem.forget_all(store, "owner") == 130 and mem.list_memories(store, "owner") == []


def test_input_is_checked_and_masked():
    store = get_memory().store
    with pytest.raises(ValueError):
        mem.add_memory(store, "owner", "feeling", "x y z")
    with pytest.raises(ValueError):
        mem.add_memory(store, "owner", "fact", "   ")
    with pytest.raises(ValueError):
        mem.add_memory(store, "owner", "fact", "x" * (mem.TEXT_MAX + 1))
    saved = mem.add_memory(store, "owner", "fact", "my account is 0012345678901")
    assert "0012345678901" not in saved["text"] and saved["text"].endswith("8901")


def test_amounts_are_redacted_in_every_spelling():
    text = "I get ₹8,000, sometimes Rs. 1250.50 or INR 99 or 500 rupees or 40/- a day"
    out = mem.redact_amounts(text)
    for number in ("8,000", "1250", "99", "500", "40/-"):
        assert number not in out
    assert out.count(mem.AMOUNT_PLACEHOLDER) == 5
    assert mem.redact_amounts("I prefer 2 short answers") == "I prefer 2 short answers"


def test_memory_never_supplies_rupees():
    store = get_memory().store
    mem.add_memory(store, "owner", "fact", "My rent is ₹9,500")
    lines = mem.memory_lines(mem.list_memories(store, "owner"))
    assert "9,500" not in " ".join(lines)
    # even if a reply repeats the remembered figure, the check does not accept it
    thread = [HumanMessage("how much is my rent?"), AIMessage("Your rent is ₹9,500.")]
    assert [str(v) for v in unverified_amounts(thread)] == ["9500.00"]


def test_remember_requests_are_recognised():
    assert mem.detect_remember("Remember that I prefer short answers") == ("preference", "I prefer short answers")
    assert mem.detect_remember("please remember my cat is called Tux")[1] == "my cat is called Tux"
    assert mem.detect_remember("remember that it was not Swiggy but Zomato")[0] == "correction"
    assert mem.detect_remember("remember") is None and mem.detect_remember("I remember this") is None


def test_the_remember_tool_hides_its_injected_arguments_from_the_model():
    assert set(mem.remember_fact.tool_call_schema.model_json_schema()["properties"]) == {"kind", "text"}


def test_context_text_is_empty_without_content():
    assert mem.context_text() == ""
    note = mem.context_text(memories=["- (fact) x"], rules=["- 'a' goes to B"], summary="s")
    assert "never supplies an amount" in note and "'a' goes to B" in note and "Summary" in note
