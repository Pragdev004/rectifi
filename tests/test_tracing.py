"""Local traces: spans are written and masked, the Traces API shows runs and a span tree, LangSmith stays off unless opted in."""

import logging
import os

import pytest
from api_support import make_client
from chat_support import ndjson
from langchain_core.messages import AIMessage
from support.chat_fakes import fake_chart, fake_rag
from support.fakes import ScriptedModel, ScriptedStructured
from test_agent import call

from rectifi import config, db, tools
from rectifi.agent import tracing
from rectifi.api import ask


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    holder = {"agent": ScriptedModel(script=[])}
    monkeypatch.setattr("rectifi.agent.llm.get_llm", lambda role="chat": holder.get(role) or ScriptedStructured())
    monkeypatch.setattr("rectifi.agent.graph.get_llm", lambda role="chat": holder["agent"])
    monkeypatch.setattr("rectifi.agent.contracts.build_rag_subgraph", lambda **k: fake_rag())
    monkeypatch.setattr("rectifi.agent.contracts.build_chart_subgraph", lambda **k: fake_chart())
    ask.reset()
    yield holder
    ask.reset()


def chat(text, setup=None, script=None):
    client = make_client()
    if script is not None:
        setup["agent"].script = script
    tid = client.post("/api/chat/threads", json={}).json()["thread_id"]
    ndjson(client.post(f"/api/chat/threads/{tid}/messages", json={"text": text}))
    return client, tid


def spans(tid):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM trace_spans WHERE thread_id = %s ORDER BY started_at, id", (tid,)).fetchall()


def test_local_spans_are_written_for_every_kind_of_step(setup):
    _, tid = chat("spent 250 on swiggy", setup, [call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}), AIMessage("Saved.")])
    rows = spans(tid)
    names = {r["name"] for r in rows}
    assert {"chat", "load_context", "route", "agent", "verify_amounts", "log_transaction"} <= names
    types = {r["run_type"] for r in rows}
    assert {"chain", "llm", "tool"} <= types
    roots = [r for r in rows if r["run_id"] == r["root_run_id"]]
    assert len(roots) == 1 and roots[0]["name"] == "chat" and roots[0]["status"] == "ok" and roots[0]["ended_at"]
    assert all(r["root_run_id"] == roots[0]["run_id"] for r in rows)
    llm = next(r for r in rows if r["run_type"] == "llm")
    assert llm["ended_at"] >= llm["started_at"]


def test_previews_are_masked_and_cut_to_2kb(setup):
    text = "paid 90 from account 0012345678901 " + "filler " * 400
    _, tid = chat(text, setup, [AIMessage("ok")])
    rows = spans(tid)
    previews = [r[k] for r in rows for k in ("input_preview", "output_preview") if r[k]]
    assert previews and all(len(p) <= config.TRACE_PREVIEW_CHARS for p in previews)
    assert not any("0012345678901" in p for p in previews)
    assert any("XXXXXXXX8901" in p or "8901" in p for p in previews)


def test_an_error_is_recorded_with_its_type_not_a_secret(setup):
    class Boom(ScriptedModel):
        def _generate(self, *a, **k):
            raise RuntimeError("provider said key sk-live-1234567890 is bad")

    setup["agent"] = Boom(script=[])
    _, tid = chat("how much did I spend")
    bad = [r for r in spans(tid) if r["status"] == "error"]
    assert bad and bad[0]["error"].startswith("RuntimeError")
    root = next(r for r in spans(tid) if r["run_id"] == r["root_run_id"])
    assert root["status"] == "error"


def test_runs_api_lists_runs_with_route_latency_and_status(setup):
    client, tid = chat("what is a rainy day fund")
    body = client.get("/api/traces").json()
    (run,) = body["runs"]
    assert run["entry"] == "chat" and run["route"] == "knowledge" and run["status"] == "ok" and run["thread_id"] == tid
    assert run["latency_ms"] >= 0 and run["span_count"] >= 5
    assert body["langsmith"]["opted_in"] is False and body["langsmith"]["sending"] is False
    assert client.get("/api/traces?status=error").json()["runs"] == []
    assert len(client.get("/api/traces?entry=chat&limit=1").json()["runs"]) == 1
    assert client.get("/api/traces?status=weird").status_code == 422


def test_the_tree_nests_steps_under_their_parents(setup):
    client, _ = chat("spent 250 on swiggy", setup, [call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}), AIMessage("Saved.")])
    run_id = client.get("/api/traces").json()["runs"][0]["root_run_id"]
    tree = client.get(f"/api/traces/{run_id}").json()
    assert tree["run"]["root_run_id"] == run_id
    spans_ = tree["spans"]
    assert spans_[0]["depth"] == 0 and spans_[0]["name"] == "chat"
    depth = {}
    for s in spans_:
        depth.setdefault(s["name"], s["depth"])                      # the first (shallowest) span of each name
    assert depth["route"] == 1 and depth["agent"] == 1 and depth["log_transaction"] > depth["agent"]
    by_id = {s["run_id"]: s for s in spans_}
    for s in spans_[1:]:
        assert by_id[s["parent_run_id"]]["depth"] == s["depth"] - 1
    assert client.get("/api/traces/not-a-uuid").status_code == 404
    assert client.get("/api/traces/0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e").status_code == 404


def test_clear_empties_the_table(setup):
    client, _ = chat("what is an sip")
    assert client.delete("/api/traces").json()["deleted"] > 0
    assert client.get("/api/traces").json()["runs"] == []


def test_prune_drops_old_spans_and_caps_the_rows(setup):
    _, tid = chat("what is an sip")
    with db.connect() as conn:
        conn.execute("UPDATE trace_spans SET started_at = now() - interval '30 days'")
    assert tracing.prune(retention_days=14) > 0 and spans(tid) == []
    chat("what is a budget")
    total = len(tools.trace_runs(50)) and len(spans_all())
    assert tracing.prune(max_rows=3) == total - 3


def spans_all():
    with db.connect() as conn:
        return conn.execute("SELECT id FROM trace_spans").fetchall()


def test_a_failed_span_write_never_breaks_a_turn(setup, monkeypatch):
    def broken():
        raise RuntimeError("db gone")

    monkeypatch.setattr(tracing, "connect", broken)
    client = make_client()
    tid = client.post("/api/chat/threads", json={}).json()["thread_id"]
    events = ndjson(client.post(f"/api/chat/threads/{tid}/messages", json={"text": "what is an sip"}))
    assert events[-1]["type"] == "done"


def test_langsmith_is_forced_off_without_opt_in(monkeypatch, caplog):
    monkeypatch.delenv("RECTIFI_LANGSMITH", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    with caplog.at_level(logging.WARNING, logger="rectifi"):
        cfg = tracing.callbacks_for("chat", thread_id="t", workstream="chat", role="chat")
    assert os.environ["LANGSMITH_TRACING"] == "false" and os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert "switched off" in caplog.text
    assert cfg["run_name"] == "chat" and cfg["tags"] == ["chat", "chat"] and cfg["metadata"]["thread_id"] == "t"


def test_langsmith_is_left_alone_when_opted_in(monkeypatch):
    monkeypatch.setenv("RECTIFI_LANGSMITH", "1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls-test")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    tracing.callbacks_for("chat", thread_id="t")
    assert os.environ["LANGSMITH_TRACING"] == "true"
    body = make_client().get("/api/traces").json()["langsmith"]
    assert body == {"opted_in": True, "sending": True, "note": "Runs are sent to LangSmith."}


def test_a_chat_turn_forces_langsmith_off(setup, monkeypatch):
    monkeypatch.delenv("RECTIFI_LANGSMITH", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    chat("what is an sip")
    assert os.environ["LANGSMITH_TRACING"] == "false"
