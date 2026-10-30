"""Conversations: the sidebar list (new, search, rename, delete), titles and previews, masked search text, and resuming a thread."""

import psycopg
import pytest
from api_support import make_client
from chat_support import ndjson
from langchain_core.messages import AIMessage
from support.chat_fakes import fake_chart, fake_rag
from support.fakes import ScriptedModel, ScriptedStructured

from rectifi import config, db, tools
from rectifi.api import ask
from rectifi.tools import RectifiError


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    holder = {"agent": ScriptedModel(script=[]), "chat": ScriptedModel(script=[])}
    monkeypatch.setattr("rectifi.agent.llm.get_llm", lambda role="chat": holder.get(role) or ScriptedStructured())
    monkeypatch.setattr("rectifi.agent.graph.get_llm", lambda role="chat": holder["agent"])
    monkeypatch.setattr("rectifi.agent.contracts.build_rag_subgraph", lambda **k: fake_rag())
    monkeypatch.setattr("rectifi.agent.contracts.build_chart_subgraph", lambda **k: fake_chart())
    ask.reset()
    yield holder
    ask.reset()


@pytest.fixture
def client():
    return make_client()


def make_thread(client, *, title=None):
    return client.post("/api/chat/threads", json={"title": title} if title else {}).json()


def say(client, tid, text):
    return ndjson(client.post(f"/api/chat/threads/{tid}/messages", json={"text": text}))[-1]


# ───────────── tools layer ─────────────

def test_create_is_idempotent_and_titles_are_checked():
    a = tools.conversation_create("t1")
    again = tools.conversation_create("t1", "Ignored")
    assert a["title"] == again["title"] == "New conversation"
    assert tools.conversation_create("t2", "  Budget   talk ")["title"] == "Budget talk"
    with pytest.raises(RectifiError):
        tools.conversation_rename("t1", "   ")
    with pytest.raises(RectifiError):
        tools.conversation_rename("nope", "x")
    assert len(tools.conversation_rename("t1", "x" * 200)["title"]) == tools.CONV_TITLE_MAX


def test_touch_names_the_chat_from_the_first_question_once_and_sets_the_preview():
    tools.conversation_touch("t1", user_text="how much did I spend on food this month and why is it so high?", assistant_text="Food was fine.")
    row = tools.conversation_get("t1")
    assert row["title"].startswith("how much did I spend on food this month") and row["title"].endswith("…") and len(row["title"]) <= 49
    assert row["preview"] == "Food was fine."
    tools.conversation_touch("t1", user_text="and travel?", assistant_text="Travel was ok.")
    assert tools.conversation_get("t1")["title"] == row["title"]            # not renamed by later turns
    tools.conversation_rename("t1", "Mine")
    tools.conversation_touch("t1", user_text="more", assistant_text="x")
    assert tools.conversation_get("t1")["title"] == "Mine"


def test_search_text_is_masked_and_capped_at_20kb():
    tools.conversation_touch("t1", user_text="my card is 4111 1111 1111 1111 and account 0012345678901", assistant_text="ok")
    with db.connect() as conn:
        text = conn.execute("SELECT search_text FROM conversations WHERE thread_id = 't1'").fetchone()["search_text"]
    assert "4111 1111 1111 1111" not in text and "0012345678901" not in text and "1111" in text
    for i in range(60):
        tools.conversation_touch("t1", user_text=f"line {i} " + "word " * 100, assistant_text="y" * 100)
    with db.connect() as conn:
        text = conn.execute("SELECT search_text FROM conversations WHERE thread_id = 't1'").fetchone()["search_text"]
    assert len(text) <= tools.CONV_SEARCH_MAX and "line 59" in text and "line 0 " not in text            # the newest text is kept


def test_search_matches_title_or_text_and_treats_wildcards_literally():
    tools.conversation_create("a", "Rent plan")
    tools.conversation_touch("b", user_text="what about 100% savings", assistant_text="sure")
    tools.conversation_touch("c", user_text="plain", assistant_text="plain")
    assert [r["thread_id"] for r in tools.conversation_list("rent")] == ["a"]
    assert [r["thread_id"] for r in tools.conversation_list("SAVINGS")] == ["b"]
    assert [r["thread_id"] for r in tools.conversation_list("100%")] == ["b"]
    assert [r["thread_id"] for r in tools.conversation_list("%")] == ["b"]
    assert tools.conversation_list("zzz") == []


def test_list_is_newest_first():
    for name in ("one", "two", "three"):
        tools.conversation_create(name)
    tools.conversation_touch("one", user_text="bump", assistant_text="x")
    assert [r["thread_id"] for r in tools.conversation_list()] == ["one", "three", "two"]


# ───────────── the API ─────────────

def test_new_search_rename_delete_through_the_api(client, setup):
    setup["agent"].script = [AIMessage("Food was low.")]
    first = make_thread(client)
    assert first["title"] == "New conversation" and first["preview"] is None
    second = make_thread(client, title="Budget planning")
    say(client, first["thread_id"], "how much did I spend on food")
    listed = client.get("/api/chat/threads").json()["threads"]
    assert [t["thread_id"] for t in listed] == [first["thread_id"], second["thread_id"]]          # the chat just used is on top
    assert listed[0]["title"] == "how much did I spend on food" and listed[0]["preview"] == "Food was low."
    assert [t["title"] for t in client.get("/api/chat/threads?q=food").json()["threads"]] == ["how much did I spend on food"]
    assert [t["title"] for t in client.get("/api/chat/threads?q=planning").json()["threads"]] == ["Budget planning"]
    renamed = client.patch(f"/api/chat/threads/{second['thread_id']}", json={"title": "Q4 budget"})
    assert renamed.status_code == 200 and renamed.json()["title"] == "Q4 budget"
    assert client.patch(f"/api/chat/threads/{second['thread_id']}", json={"title": " "}).status_code == 422
    assert client.patch("/api/chat/threads/0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e", json={"title": "x"}).status_code == 422
    assert client.get("/api/chat/threads?q=" + "x" * 101).status_code == 422


def test_delete_removes_the_row_the_checkpoints_and_the_spans(client, setup):
    setup["agent"].script = [AIMessage("ok")]
    tid = make_thread(client)["thread_id"]
    say(client, tid, "how much did I spend")
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s", (tid,)).fetchone()["n"] > 0
        assert conn.execute("SELECT count(*) AS n FROM trace_spans WHERE thread_id = %s", (tid,)).fetchone()["n"] > 0
    assert client.delete(f"/api/chat/threads/{tid}").json() == {"deleted": True}
    with psycopg.connect(config.database_url()) as raw:
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            assert raw.execute(f"SELECT count(*) FROM {table} WHERE thread_id = %s", (tid,)).fetchone()[0] == 0
        assert raw.execute("SELECT count(*) FROM trace_spans WHERE thread_id = %s", (tid,)).fetchone()[0] == 0
    assert client.get(f"/api/chat/threads/{tid}").status_code == 404
    assert client.get("/api/chat/threads").json()["threads"] == []
    assert client.delete(f"/api/chat/threads/{tid}").status_code == 404


def test_deleting_one_chat_leaves_the_others(client, setup):
    setup["agent"].script = [AIMessage("a"), AIMessage("b")]
    one, two = make_thread(client)["thread_id"], make_thread(client)["thread_id"]
    say(client, one, "how much did I spend")
    say(client, two, "how much did I earn")
    client.delete(f"/api/chat/threads/{one}")
    assert [i["text"] for i in client.get(f"/api/chat/threads/{two}").json()["items"]] == ["how much did I earn", "b"]


def test_resume_shows_the_full_history_with_times_and_survives_a_new_graph(client, setup):
    setup["agent"].script = [AIMessage("First."), AIMessage("Second.")]
    tid = make_thread(client)["thread_id"]
    say(client, tid, "how much did I spend")
    say(client, tid, "and last month?")
    ask.reset()
    body = make_client().get(f"/api/chat/threads/{tid}").json()
    assert [i["text"] for i in body["items"]] == ["how much did I spend", "First.", "and last month?", "Second."]
    stamps = [i["ts"] for i in body["items"]]
    assert all(stamps) and stamps == sorted(stamps)
    assert body["thread"]["title"] == "how much did I spend"


def test_a_message_to_a_thread_with_no_row_creates_the_row(client, setup):
    setup["agent"].script = [AIMessage("ok")]
    tid = ask.new_thread_id()
    say(client, tid, "how much did I spend")
    assert [t["thread_id"] for t in client.get("/api/chat/threads").json()["threads"]] == [tid]


def test_two_chats_do_not_see_each_other(client, setup):
    setup["agent"].script = [AIMessage("Was the ₹500 for food or travel?"), AIMessage("Saved with no context.")]
    one, two = make_thread(client)["thread_id"], make_thread(client)["thread_id"]
    say(client, one, "paid 500")
    say(client, two, "travel")
    assert len(client.get(f"/api/chat/threads/{two}").json()["items"]) == 2
