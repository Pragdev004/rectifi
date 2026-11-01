"""/api/chat: the streamed turn (nodes, steps, trace lines, tokens, done), Stop, Regenerate, interrupts, errors and aliases."""

import json
import threading

import pytest
from api_support import make_client
from chat_support import ndjson
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from charts_support import GOOD, reply as chart_reply, seed_spending
from rag_support import RuleModel
from support.fakes import BowEmbeddings, ScriptedModel, ScriptedStructured
from test_agent import call

from rectifi import config, tools
from rectifi.api import ask
from rectifi.charts import graph as chart_graph
from rectifi.charts import sandbox
from rectifi.rag import index, web

RAG_ANSWER = "Keep three to six months of basic expenses [1]."


class Streaming(ScriptedModel):
    """A scripted model that streams: text word by word, tool calls as one chunk. `after` runs after each word (a test hook)."""

    after: object = None
    words_sent: int = 0

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.extend(messages)
        message = self.script.pop(0)
        if message.tool_calls:
            c = message.tool_calls[0]
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[
                {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": 0}]))
            return
        for word in message.content.split(" "):
            self.words_sent += 1
            yield ChatGenerationChunk(message=AIMessageChunk(content=word + " "))
            if self.after:
                self.after(self.words_sent)


def rag_grader(doc="yes"):
    """Scripted answers for the REAL knowledge graph's grader / router model."""
    return RuleModel(
        structured={"DocGrade": lambda t: {"relevance": doc, "reason": "x"},
                    "GroundCheck": lambda t: {"supported": "fully", "unsupported_claims": []},
                    "UseCheck": lambda t: {"useful": True, "score": 5},
                    "RouteDecision": lambda t: {"route": "knowledge", "reason": "a guide question"},
                    "QueryList": lambda t: {"queries": ["emergency fund months"]}},
        write=lambda t: "emergency fund size in months")


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("RECTIFI_TRACE_ROWS", "20000")
    holder = {"agent": ScriptedModel(script=[]), "chat": ScriptedModel(script=[]), "grade": ScriptedStructured()}
    monkeypatch.setattr("rectifi.agent.llm.get_llm", lambda role="chat": holder.get(role) or holder["chat"])
    monkeypatch.setattr("rectifi.agent.graph.get_llm", lambda role="chat": holder["agent"])
    # The REAL knowledge and chart subgraphs (built through agent.contracts); only models and embeddings are scripted.
    monkeypatch.setenv("RECTIFI_EMBED_MODEL", "bow")
    embeddings = BowEmbeddings()
    index.reindex(embeddings, embed_model=config.embed_model())
    monkeypatch.setattr("rectifi.agent.llm.get_embeddings", lambda: embeddings)
    monkeypatch.setattr(sandbox, "CACHE_DIR", tmp_path / "mpl")
    monkeypatch.setattr(chart_graph, "RETRY_INTERVAL", 0)
    holder["code"] = ScriptedModel(script=[chart_reply(GOOD)])
    ask.reset()
    yield holder
    ask.reset()


@pytest.fixture
def seeded(setup):
    seed_spending()


@pytest.fixture
def models(setup):
    return setup


@pytest.fixture
def client():
    return make_client()


def new_thread(client, title=None):
    return client.post("/api/chat/threads", json={"title": title} if title else {}).json()["thread_id"]


def send(client, tid, text, **extra):
    return client.post(f"/api/chat/threads/{tid}/messages", json={"text": text, **extra})


def test_status_mirrors_the_ask_status(client):
    assert client.get("/api/chat/status").json() == client.get("/api/ask/status").json()


def test_a_turn_streams_nodes_trace_steps_then_the_whole_conversation(client, models):
    models["agent"].script = [call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}), AIMessage("Saved ₹250.00 at Swiggy.")]
    tid = new_thread(client)
    r = send(client, tid, "spent 250 on swiggy")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = ndjson(r)
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "done" and "node" in kinds and "trace" in kinds and "step" in kinds
    assert {"type": "step", "tool": "log_transaction", "error": False} in events
    assert [e["event"]["step"] for e in events if e["type"] == "trace"][:1] == ["route"]
    done = events[-1]
    assert [i["role"] for i in done["items"]] == ["user", "assistant"]
    user, reply = done["items"]
    assert user["ts"] and reply["ts"] and reply["route"] == "agent" and reply["unverified"] == []
    assert [s["tool"] for s in reply["steps"]] == ["log_transaction"]
    assert done["thread"]["title"] == "spent 250 on swiggy" and done["pending_interrupt"] is None and done["alternatives"] == []
    assert len(tools.list_transactions()) == 1


def test_tokens_stream_before_the_done_line(client, models):
    models["agent"] = Streaming(script=[AIMessage("You spent nothing yet this month.")])
    tid = new_thread(client)
    events = ndjson(send(client, tid, "how much did I spend this month?"))
    tokens = [e for e in events if e["type"] == "token"]
    assert len(tokens) >= 5 and len({t["id"] for t in tokens}) == 1
    assert "".join(t["text"] for t in tokens).strip() == "You spent nothing yet this month."
    assert events.index(tokens[0]) < events.index(events[-1]) and events[-1]["type"] == "done"


def test_only_final_nodes_stream_tokens_not_the_router_or_graders(client, models):
    models["grade"] = ScriptedStructured(structured=[{"route": "smalltalk", "reason": "chat"}])
    models["chat"] = Streaming(script=[AIMessage("Glad to help.")])
    tid = new_thread(client)
    events = ndjson(send(client, tid, "banana"))
    assert "".join(e["text"] for e in events if e["type"] == "token").strip() == "Glad to help."


def test_knowledge_answers_carry_sources_and_the_check_trace(client, models):
    models["grade"] = rag_grader()
    models["chat"] = Streaming(script=[AIMessage(RAG_ANSWER)])
    tid = new_thread(client)
    events = ndjson(send(client, tid, "what is an emergency fund, and how many months should it cover?", mode="full"))
    assert events[-1]["type"] == "done", events[-1]
    reply = events[-1]["items"][-1]
    assert reply["route"] == "knowledge" and reply["text"] == RAG_ANSWER and reply["abstained"] is False
    cite = reply["citations"][0]
    assert cite["n"] == 1 and cite["label"] == "GUIDE" and cite["corpus"] == "guide" and cite["source"].startswith("guides/")
    steps = [t["step"] for t in reply["trace"]]
    assert "SUPPORTED" in steps and "USEFUL" in steps and reply["support"] == "full" and reply["useful"] is True
    assert [c["step"] for c in reply["checks"]][-1] == "amounts"
    streamed = "".join(e["text"] for e in events if e["type"] == "token")          # the answer streams from the RAG "generate" node only
    assert streamed.strip() == RAG_ANSWER


def test_chart_answers_point_at_the_saved_chart(client, seeded):
    tid = new_thread(client)
    reply = ndjson(send(client, tid, "/chart food by month"))[-1]["items"][-1]
    assert reply["route"] == "chart" and reply["artifact"]["chart_id"] and tools.get_chart(reply["artifact"]["chart_id"])["title"]


def test_an_invented_figure_is_flagged(client, models):
    models["agent"].script = [AIMessage("You spent ₹77,777 on food this month.")]
    done = ndjson(send(client, new_thread(client), "how much on food?"))[-1]
    assert done["items"][-1]["unverified"] == ["77777.00"]


def test_the_conversation_resumes_with_full_history_and_times_after_a_restart(client, models):
    models["agent"].script = [AIMessage("Was the ₹500 for food or travel?"), AIMessage("Noted.")]
    tid = new_thread(client)
    send(client, tid, "paid 500")
    send(client, tid, "travel")
    ask.reset()                                           # a new process: new graph, new pool, same database
    body = make_client().get(f"/api/chat/threads/{tid}").json()
    assert [i["role"] for i in body["items"]] == ["user", "assistant", "user", "assistant"]
    assert all(i["ts"] for i in body["items"]) and body["items"][1]["unverified"] == []
    assert body["thread"]["thread_id"] == tid and body["pending_interrupt"] is None


def test_history_opens_even_when_the_model_is_off(client, models, monkeypatch):
    models["agent"].script = [AIMessage("Hi there.")]
    tid = new_thread(client)
    send(client, tid, "how much did I spend")
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert len(client.get(f"/api/chat/threads/{tid}").json()["items"]) == 2
    r = send(client, tid, "again")
    assert r.status_code == 503 and r.json()["error"]["code"] == "model_unavailable"


def test_remember_pauses_with_a_question_and_resume_saves(client):
    tid = new_thread(client)
    events = ndjson(send(client, tid, "remember that I prefer short answers"))
    pause = next(e for e in events if e["type"] == "interrupt")
    assert pause["kind"] == "memory" and pause["payload"]["text"] == "I prefer short answers" and pause["payload"]["prompt"] == "Save this to memory?"
    done = events[-1]
    assert done["type"] == "done" and done["pending_interrupt"]["kind"] == "memory"
    reopened = client.get(f"/api/chat/threads/{tid}").json()                  # the question is still waiting after a reload
    assert reopened["pending_interrupt"]["payload"]["memory_kind"] == "preference"
    assert client.get("/api/memory").json()["memories"] == []
    after = ndjson(client.post(f"/api/chat/threads/{tid}/resume", json={"value": True}))
    assert after[-1]["pending_interrupt"] is None and after[-1]["items"][-1]["text"].startswith("Remembered: I prefer short answers")
    assert [m["text"] for m in client.get("/api/memory").json()["memories"]] == ["I prefer short answers"]
    assert client.post(f"/api/chat/threads/{tid}/resume", json={"value": True}).status_code == 409


def test_the_web_question_from_the_real_rag_graph_reaches_the_window_as_web_consent(client, models, monkeypatch):
    sent = []
    monkeypatch.setattr(web, "available", lambda: True)
    monkeypatch.setattr(web, "search", lambda q: sent.append(q) or [])
    models["grade"] = rag_grader(doc="no")
    models["chat"] = Streaming(script=[AIMessage(RAG_ANSWER)])
    tid = new_thread(client)
    events = ndjson(send(client, tid, "how does a SIP work?", mode="crag"))
    pause = next(e for e in events if e["type"] == "interrupt")
    assert pause["kind"] == "web_consent" and pause["payload"]["type"] == "web_search" and pause["payload"]["query"]
    reopened = client.get(f"/api/chat/threads/{tid}").json()
    assert reopened["pending_interrupt"]["kind"] == "web_consent"
    done = ndjson(client.post(f"/api/chat/threads/{tid}/resume", json={"value": False}))[-1]
    assert sent == [] and done["pending_interrupt"] is None
    assert done["items"][-1]["route"] == "knowledge" and any(t["step"] == "WEB" and t["verdict"] == "declined" for t in done["items"][-1]["trace"])


def test_the_chart_code_review_pause_resumes_with_a_plain_yes(client, seeded):
    tid = new_thread(client)
    events = ndjson(send(client, tid, "/chart food by month", review=True))
    pause = next(e for e in events if e["type"] == "interrupt")
    assert pause["kind"] == "review_code" and "plt." in pause["payload"]["code"]
    done = ndjson(client.post(f"/api/chat/threads/{tid}/resume", json={"value": True}))[-1]
    assert done["items"][-1]["artifact"]["chart_id"] and done["pending_interrupt"] is None


def test_stop_ends_the_answer_at_the_next_token_and_keeps_what_was_said(client, models):
    said = threading.Event()
    release = threading.Event()

    def pause(n):
        if n == 3:
            said.set()
            release.wait(10)

    model = Streaming(script=[AIMessage("one two three four five six seven eight nine ten")], after=pause)
    models["agent"] = model
    tid = new_thread(client)
    out = {}
    worker = threading.Thread(target=lambda: out.update(r=send(client, tid, "how much did I spend")))
    worker.start()
    assert said.wait(10)
    assert make_client().post(f"/api/chat/threads/{tid}/stop", json={}).json() == {"stopping": True}
    release.set()
    worker.join(15)
    done = ndjson(out["r"])[-1]
    assert done["type"] == "done" and done["stopped"] is True
    reply = done["items"][-1]
    assert reply["stopped"] is True and reply["text"].startswith("one two three") and "ten" not in reply["text"]
    assert model.words_sent < 10                                                    # the model stream was cut, not read to the end
    assert make_client().post(f"/api/chat/threads/{tid}/stop", json={}).json() == {"stopping": False}
    again = client.get(f"/api/chat/threads/{tid}").json()["items"]
    assert again[-1]["stopped"] is True


def test_stop_after_a_tool_ran_keeps_the_tool_result_in_the_conversation(client, models):
    release = threading.Event()
    seen_text = threading.Event()

    def pause(n):
        if n == 2:
            seen_text.set()
            release.wait(10)

    models["agent"] = Streaming(script=[call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}),
                                        AIMessage("Saved it and everything went fine today okay")], after=pause)
    tid = new_thread(client)
    out = {}
    worker = threading.Thread(target=lambda: out.update(r=send(client, tid, "spent 250 on swiggy")))
    worker.start()
    assert seen_text.wait(10)
    make_client().post(f"/api/chat/threads/{tid}/stop", json={})
    release.set()
    worker.join(15)
    done = ndjson(out["r"])[-1]
    assert done["stopped"] is True and len(tools.list_transactions()) == 1       # the payment was saved, once
    reply = done["items"][-1]
    assert reply["stopped"] and [s["tool"] for s in reply["steps"]] == ["log_transaction"]
    # and a new turn works on the same thread
    models["agent"] = ScriptedModel(script=[AIMessage("Anything else?")])
    ask.reset()
    assert ndjson(send(client, tid, "how much did I spend today"))[-1]["items"][-1]["text"] == "Anything else?"


def test_regenerate_forks_and_keeps_the_old_answer(client, models):
    models["agent"].script = [AIMessage("First answer."), AIMessage("Second answer.")]
    tid = new_thread(client)
    send(client, tid, "how much did I spend")
    done = ndjson(client.post(f"/api/chat/threads/{tid}/regenerate", json={}))[-1]
    assert [i["text"] for i in done["items"]] == ["how much did I spend", "Second answer."]
    assert [a["text"] for a in done["alternatives"]] == ["First answer.", "Second answer."]
    body = client.get(f"/api/chat/threads/{tid}").json()
    assert [a["text"] for a in body["alternatives"]] == ["First answer.", "Second answer."]
    graph = ask.get_supervisor()
    texts = {m.content for s in graph.get_state_history({"configurable": {"thread_id": tid}}) for m in (s.values or {}).get("messages", [])}
    assert {"First answer.", "Second answer."} <= texts                              # the old branch is still in history


def test_regenerate_with_nothing_to_answer_is_409(client):
    assert client.post(f"/api/chat/threads/{new_thread(client)}/regenerate", json={}).status_code == 409


def test_a_second_message_while_one_is_running_is_409(client, models):
    gate, started = threading.Event(), threading.Event()

    class Slow(ScriptedModel):
        def _generate(self, *a, **k):
            started.set()
            gate.wait(10)
            return super()._generate(*a, **k)

    models["agent"] = Slow(script=[AIMessage("Done.")])
    tid = new_thread(client)
    out = {}
    worker = threading.Thread(target=lambda: out.update(r=send(client, tid, "how much did I spend")))
    worker.start()
    assert started.wait(10)
    second = send(make_client(), tid, "again")
    assert second.status_code == 409 and "Still answering" in second.json()["error"]["message"]
    assert make_client().delete(f"/api/chat/threads/{tid}").status_code == 409          # can't delete while it runs
    gate.set()
    worker.join(15)
    assert ndjson(out["r"])[-1]["type"] == "done"


def test_a_model_error_arrives_as_an_error_line_and_the_thread_stays_usable(client, models):
    class Boom(Exception):
        status_code = 429

    class Broken(ScriptedModel):
        def _generate(self, *a, **k):
            raise Boom("rate")

    models["agent"] = Broken(script=[])
    tid = new_thread(client)
    events = ndjson(send(client, tid, "how much did I spend"))
    assert events[-1]["type"] == "error" and "429" in events[-1]["message"] and "sk-test" not in json.dumps(events)
    models["agent"] = ScriptedModel(script=[AIMessage("Back now.")])
    ask.reset()
    assert ndjson(send(client, tid, "how much did I spend"))[-1]["items"][-1]["text"] == "Back now."


def test_validation_and_unknown_threads(client):
    tid = new_thread(client)
    assert send(client, tid, "x" * 4001).status_code == 422
    assert send(client, tid, "  ").status_code == 422
    assert send(client, tid, "hi", route="nowhere").status_code == 422
    assert send(client, tid, "hi", mode="deep").status_code == 422
    assert client.get("/api/chat/threads/not-a-uuid").status_code == 404
    assert client.get("/api/chat/threads/0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e").status_code == 404
    assert client.post("/api/chat/threads/not-a-uuid/stop", json={}).status_code == 404
    assert client.post(f"/api/chat/threads/{tid}/resume", json={}).status_code == 422


def test_the_chat_routes_keep_the_api_guards(client):
    bare = make_client(headers={})
    assert bare.get("/api/chat/threads").status_code == 403
    assert bare.post("/api/chat/threads", json={}).status_code == 403


def test_the_old_ask_routes_still_work_beside_the_chat(client, models, monkeypatch):
    monkeypatch.setattr("rectifi.agent.graph.get_llm", lambda role="chat": ScriptedModel(script=[AIMessage("Old path.")]))
    ask.reset()
    old = client.post("/api/ask/threads").json()["thread_id"]
    events = ndjson(client.post(f"/api/ask/threads/{old}/messages", json={"text": "how much did I spend"}))
    assert [e["type"] for e in events] == ["done"] and events[0]["items"][1]["text"] == "Old path."
    assert client.get("/api/chat/threads").json()["threads"] == []                     # the alias does not touch the chat list


def test_a_stopped_answer_with_a_made_up_figure_still_says_not_verified(client, models):
    said = threading.Event()
    release = threading.Event()

    def pause(n):
        if n == 3:
            said.set()
            release.wait(10)

    models["agent"] = Streaming(script=[AIMessage("You spent ₹999 on food so far this month okay")], after=pause)
    tid = new_thread(client)
    out = {}
    worker = threading.Thread(target=lambda: out.update(r=send(client, tid, "how much did I spend")))
    worker.start()
    assert said.wait(10)
    make_client().post(f"/api/chat/threads/{tid}/stop", json={})
    release.set()
    worker.join(15)
    reply = ndjson(out["r"])[-1]["items"][-1]
    assert reply["stopped"] is True and "₹999" in reply["text"]
    assert reply["unverified"] == ["999.00"]
    assert client.get(f"/api/chat/threads/{tid}").json()["items"][-1]["unverified"] == ["999.00"]
