"""Persistence on the Postgres saver (CampusX LangGraph 11): threads, state history, time travel, fault tolerance, update_state, retries."""

import pytest
from chat_support import cfg, last_ai, make, tid, turn
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from support.chat_fakes import fake_chart
from support.fakes import ScriptedModel

from rectifi.agent.checkpoint import get_memory
from rectifi.agent.supervisor import build_supervisor, new_human


def fork_point(graph, t):
    for snap in graph.get_state_history(cfg(t)):
        if tuple(snap.next) == ("load_context",):
            return snap


def test_state_survives_a_brand_new_graph_and_pool():
    t = tid()
    turn(make(agent=[AIMessage("First.")]), t, "how much did I spend")
    from rectifi.agent.checkpoint import close_memory

    close_memory()                                          # like a restart: the pool is gone, the database is not
    fresh = make()
    assert [m.content for m in fresh.get_state(cfg(t)).values["messages"]][-1] == "First."


def test_time_travel_forks_and_keeps_the_old_branch():
    g = make(agent=[AIMessage("Answer A."), AIMessage("Answer B.")])
    t = tid()
    turn(g, t, "how much did I spend")
    before = len(list(g.get_state_history(cfg(t))))
    point = fork_point(g, t)
    assert point is not None and isinstance(point.values["messages"][-1], HumanMessage)
    out = g.invoke(None, point.config)                      # re-run from the saved moment before the answer
    assert last_ai(out).content == "Answer B."
    assert [m.content for m in g.get_state(cfg(t)).values["messages"] if isinstance(m, AIMessage)] == ["Answer B."]
    history = list(g.get_state_history(cfg(t)))
    assert len(history) > before
    seen = {m.content for s in history for m in s.values.get("messages", []) if isinstance(m, AIMessage)}
    assert {"Answer A.", "Answer B."} <= seen
    leaves = [s for s in history if not s.next]
    assert len({s.config["configurable"]["checkpoint_id"] for s in leaves}) == 2         # two finished branches


def test_replaying_an_old_checkpoint_does_not_touch_other_threads():
    g = make(agent=[AIMessage("One."), AIMessage("Two."), AIMessage("Other.")])
    a, b = tid(), tid()
    turn(g, a, "how much did I spend")
    turn(g, b, "how much did I earn")
    g.invoke(None, fork_point(g, a).config)
    assert last_ai(g.get_state(cfg(b)).values).content == "Two."            # thread b is untouched
    assert last_ai(g.get_state(cfg(a)).values).content == "Other."          # thread a got the new answer


class Flaky:
    """A chart worker that fails once, to show a turn can be finished later from where it stopped."""

    def __init__(self):
        self.calls = 0


def flaky_chart(counter: Flaky):
    def run(state):
        counter.calls += 1
        if counter.calls == 1:
            raise RuntimeError("sandbox crashed")
        return {"chart_id": 4, "ok": True, "trace": [], "error": None}

    from rectifi.agent.contracts import ChartInput, ChartOutput

    g = StateGraph(ChartInput, output_schema=ChartOutput)
    g.add_node("run", run)
    g.add_edge(START, "run")
    g.add_edge("run", END)
    return g.compile()


def test_resume_after_a_node_crash_runs_only_the_failed_step():
    counter = Flaky()
    g = make(chart=flaky_chart(counter))
    t = tid()
    with pytest.raises(RuntimeError):
        turn(g, t, "plot my food spend")
    snap = g.get_state(cfg(t))
    assert snap.next == ("chart",)                          # route, memory and trim steps are saved and done
    assert snap.values["route"] == "chart" and [m.content for m in snap.values["messages"]] == ["plot my food spend"]
    out = g.invoke(None, cfg(t))
    assert counter.calls == 2 and last_ai(out).additional_kwargs["artifact"] == {"chart_id": 4}
    assert [type(m).__name__ for m in out["messages"]] == ["HumanMessage", "AIMessage"]       # no duplicate message


def test_update_state_edits_a_thread_as_if_a_node_wrote_it():
    g = make(agent=[AIMessage("Original.")])
    t = tid()
    out = turn(g, t, "how much did I spend")
    original = last_ai(out)
    g.update_state(cfg(t), {"messages": [original.model_copy(update={"content": "Corrected."})]}, as_node="verify_amounts")
    assert last_ai(g.get_state(cfg(t)).values).content == "Corrected."
    assert len([m for m in g.get_state(cfg(t)).values["messages"] if isinstance(m, AIMessage)]) == 1      # same id: replaced, not added


def test_update_state_can_append_a_message_to_a_finished_thread():
    g = make(agent=[AIMessage("Original.")])
    t = tid()
    turn(g, t, "how much did I spend")
    g.update_state(cfg(t), {"messages": [AIMessage("Note added later.")]}, as_node="verify_amounts")
    assert [m.content for m in g.get_state(cfg(t)).values["messages"]][-1] == "Note added later."


def test_the_model_node_that_is_safe_to_repeat_has_a_retry_policy_and_the_others_do_not():
    memory = get_memory()
    g = build_supervisor(checkpointer=memory.saver, store=memory.store, chart_graph=fake_chart())
    assert g.nodes["smalltalk"].retry_policy and g.nodes["smalltalk"].retry_policy[0].max_attempts == 2
    for name in ("agent", "chart", "knowledge", "remember"):          # these write things or ask questions: never repeated by themselves
        assert not g.nodes[name].retry_policy


def test_threads_are_isolated_and_a_second_turn_extends_the_first():
    g = make(agent=[AIMessage("Was the ₹500 for food or travel?"), AIMessage("Noted."), AIMessage("Different thread.")])
    a, b = tid(), tid()
    turn(g, a, "paid 500")
    turn(g, a, "travel")
    turn(g, b, "how much did I spend")
    assert len(g.get_state(cfg(a)).values["messages"]) == 4 and len(g.get_state(cfg(b)).values["messages"]) == 2


def test_each_message_has_a_timestamp_in_order():
    g = make(agent=[AIMessage("A."), AIMessage("B.")])
    t = tid()
    turn(g, t, "how much did I spend")
    turn(g, t, "and last month")
    stamps = [m.additional_kwargs["ts"] for m in g.get_state(cfg(t)).values["messages"]]
    assert len(stamps) == 4 and stamps == sorted(stamps)


def test_new_human_messages_carry_an_id_and_a_time():
    m = new_human("hi")
    assert m.id and m.additional_kwargs["ts"].endswith("+00:00")
