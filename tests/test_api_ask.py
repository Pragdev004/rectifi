"""The Ask API with a scripted model: status, a streamed turn (steps, then the transcript), history, 'Not verified', 404 and 409."""

import json
import threading

import pytest
from api_support import make_client
from langchain_core.messages import AIMessage
from test_agent import ScriptedModel, call

from rectifi import tools
from rectifi.api import ask


@pytest.fixture(autouse=True)
def deepseek(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    ask.reset()
    yield
    ask.reset()


@pytest.fixture
def client():
    return make_client()


def script(monkeypatch, messages):
    model = ScriptedModel(script=list(messages))
    monkeypatch.setattr("rectifi.agent.graph.get_llm", lambda: model)
    return model


def lines(response):
    return [json.loads(x) for x in response.text.splitlines() if x.strip()]


def test_status_on_and_off(client, monkeypatch):
    body = client.get("/api/ask/status").json()
    assert body["ok"] is True and body["why"] == "" and body["model"] == "deepseek · deepseek-flash"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    body = client.get("/api/ask/status").json()
    assert body["ok"] is False and "DEEPSEEK_API_KEY" in body["why"]


def test_off_means_503_with_the_reason(client, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    tid = client.post("/api/ask/threads").json()["thread_id"]
    r = client.post(f"/api/ask/threads/{tid}/messages", json={"text": "hi"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "model_unavailable" and "DEEPSEEK_API_KEY" in r.json()["error"]["message"]
    assert client.get(f"/api/ask/threads/{tid}").status_code == 503


def test_new_thread_ids_are_uuids_and_bad_ids_are_404(client):
    tid = client.post("/api/ask/threads").json()["thread_id"]
    assert len(tid) == 36 and tid.count("-") == 4
    assert client.get("/api/ask/threads/not-a-uuid").status_code == 404
    assert client.post("/api/ask/threads/not-a-uuid/messages", json={"text": "hi"}).status_code == 404
    assert client.get("/api/ask/threads/../../etc").status_code == 404


def test_a_turn_streams_steps_then_the_transcript_and_the_row_is_saved(client, monkeypatch):
    script(monkeypatch, [
        call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}),
        AIMessage("Saved ₹250.00 at Swiggy under Food & Dining."),
    ])
    tid = client.post("/api/ask/threads").json()["thread_id"]
    r = client.post(f"/api/ask/threads/{tid}/messages", json={"text": "spent 250 on swiggy"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = lines(r)
    assert [e["type"] for e in events] == ["step", "done"]
    assert events[0] == {"type": "step", "tool": "log_transaction", "error": False}
    items = events[1]["items"]
    assert [i["role"] for i in items] == ["user", "assistant"]
    assert items[0]["text"] == "spent 250 on swiggy"
    assert items[1]["text"].startswith("Saved ₹250.00") and items[1]["unverified"] == []
    assert [s["tool"] for s in items[1]["steps"]] == ["log_transaction"] and '"added"' in items[1]["steps"][0]["output"]
    assert len(tools.list_transactions()) == 1

    history = client.get(f"/api/ask/threads/{tid}").json()["items"]
    assert history == items


def test_an_invented_figure_in_the_reply_is_flagged_not_verified(client, monkeypatch):
    script(monkeypatch, [AIMessage("You spent ₹77,777 on food this month.")])
    tid = client.post("/api/ask/threads").json()["thread_id"]
    events = lines(client.post(f"/api/ask/threads/{tid}/messages", json={"text": "how much on food?"}))
    assistant = events[-1]["items"][-1]
    assert assistant["unverified"] == ["77777.00"]
    assert client.get(f"/api/ask/threads/{tid}").json()["items"][-1]["unverified"] == ["77777.00"]


def test_the_guard_still_refuses_an_invented_amount_and_the_step_is_marked_error(client, monkeypatch):
    script(monkeypatch, [
        call("log_transaction", {"amount": "2500", "direction": "debit", "payee": "Swiggy"}),
        AIMessage("Sorry, how much exactly?"),
    ])
    tid = client.post("/api/ask/threads").json()["thread_id"]
    events = lines(client.post(f"/api/ask/threads/{tid}/messages", json={"text": "spent 250 on swiggy"}))
    assert events[0] == {"type": "step", "tool": "log_transaction", "error": True}
    step = events[-1]["items"][-1]["steps"][0]
    assert step["error"] is True and step["output"].startswith("GUARD")
    assert tools.list_transactions() == []


def test_a_second_turn_in_the_same_conversation_sees_the_first(client, monkeypatch):
    script(monkeypatch, [AIMessage("Was the ₹500 for food or travel?"), AIMessage("Noted.")])
    tid = client.post("/api/ask/threads").json()["thread_id"]
    client.post(f"/api/ask/threads/{tid}/messages", json={"text": "paid 500"})
    events = lines(client.post(f"/api/ask/threads/{tid}/messages", json={"text": "travel"}))
    assert [i["role"] for i in events[-1]["items"]] == ["user", "assistant"]            # only this turn
    full = client.get(f"/api/ask/threads/{tid}").json()["items"]
    assert [i["role"] for i in full] == ["user", "assistant", "user", "assistant"]
    assert full[1]["unverified"] == []                                                    # 500 came from the user's own message


def test_a_model_error_arrives_as_an_error_line(client, monkeypatch):
    class Boom(Exception):
        status_code = 429

    class Broken(ScriptedModel):
        def _generate(self, *a, **k):
            raise Boom("rate")

    model = Broken(script=[])
    monkeypatch.setattr("rectifi.agent.graph.get_llm", lambda: model)
    tid = client.post("/api/ask/threads").json()["thread_id"]
    events = lines(client.post(f"/api/ask/threads/{tid}/messages", json={"text": "hi"}))
    assert events[-1]["type"] == "error" and "429" in events[-1]["message"]
    assert "sk-test" not in client.get("/api/ask/status").text


def test_a_second_message_while_one_is_running_is_409(client, monkeypatch):
    gate = threading.Event()
    started = threading.Event()

    class Slow(ScriptedModel):
        def _generate(self, *a, **k):
            started.set()
            gate.wait(10)
            return super()._generate(*a, **k)

    model = Slow(script=[AIMessage("Done.")])
    monkeypatch.setattr("rectifi.agent.graph.get_llm", lambda: model)
    tid = client.post("/api/ask/threads").json()["thread_id"]
    results = {}

    def first():
        results["first"] = client.post(f"/api/ask/threads/{tid}/messages", json={"text": "hello"})

    worker = threading.Thread(target=first)
    worker.start()
    assert started.wait(10)
    second = make_client().post(f"/api/ask/threads/{tid}/messages", json={"text": "again"})
    assert second.status_code == 409 and second.json()["error"]["code"] == "conflict" and "Still answering" in second.json()["error"]["message"]
    gate.set()
    worker.join(15)
    assert results["first"].status_code == 200 and lines(results["first"])[-1]["type"] == "done"
    other = client.post("/api/ask/threads").json()["thread_id"]
    assert other != tid


def test_text_limits(client, monkeypatch):
    script(monkeypatch, [AIMessage("ok")])
    tid = client.post("/api/ask/threads").json()["thread_id"]
    assert client.post(f"/api/ask/threads/{tid}/messages", json={"text": "x" * 4001}).status_code == 422
    assert client.post(f"/api/ask/threads/{tid}/messages", json={"text": "  "}).status_code == 422
    assert client.post(f"/api/ask/threads/{tid}/messages", json={}).status_code == 422
