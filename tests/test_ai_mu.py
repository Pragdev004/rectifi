"""WS-AI (multi-user plan, D7 and D8): the owner's chat, chart, library and classify flows work end to end in multi-user mode with
sealed transactions; the library holds no ledger text there; threads keep the person; the restart requeue waits for the owner's
sign-in; a friend is refused on every AI route.

The owner signs up through the real API (an owner invite), so the session key is real and every row is really sealed. Models and
embeddings are scripted: no Ollama and no API key.
"""

import re
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from api_support import HEADERS
from charts_support import GOOD, reply as chart_reply, seed_spending
from chat_support import ndjson
from langchain_core.messages import AIMessage
from psycopg.rows import dict_row
from rag_support import RuleModel
from starlette.testclient import TestClient
from support.fakes import BowEmbeddings, ScriptedModel, ScriptedStructured
from test_agent import call

from rectifi import config, db, hooks, tools, userctx, users
from rectifi.agent import jobs
from rectifi.api import ask, auth, uploads
from rectifi.api.app import api_routes, create_app
from rectifi.api.routes import chat as chat_routes
from rectifi.api.routes import knowledge
from rectifi.charts import graph as chart_graph
from rectifi.charts import sandbox
from rectifi.importer import StatementRow, import_rows
from rectifi.rag import index

SECRET = "s" * 48
PASSWORD = "correct horse battery"
RAG_ANSWER = "Keep three to six months of basic expenses [1]."
CANARY = "ZQXCANARYNOTE"


@pytest.fixture(autouse=True)
def world(monkeypatch, tmp_path):
    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", SECRET)
    users.reset_limiters()
    called = []
    monkeypatch.setattr(hooks, "after_sign_in", lambda ctx: called.append(ctx.uid))   # these tests call the hook blocks they care about

    async def no_pause(seconds):
        return None

    monkeypatch.setattr(auth, "pause", no_pause)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("RECTIFI_TRACE_ROWS", "20000")
    holder = {"agent": ScriptedModel(script=[]), "chat": ScriptedModel(script=[]), "grade": ScriptedStructured(),
              "classify": ScriptedStructured(structured=[])}
    monkeypatch.setattr("rectifi.agent.llm.get_llm", lambda role="chat": holder.get(role) or holder["chat"])
    monkeypatch.setattr("rectifi.agent.graph.get_llm", lambda role="chat": holder["agent"])
    monkeypatch.setenv("RECTIFI_EMBED_MODEL", "bow")
    embeddings = BowEmbeddings()
    monkeypatch.setattr("rectifi.agent.llm.get_embeddings", lambda: embeddings)
    monkeypatch.setattr("rectifi.rag.embeddings.get_rag_embeddings", lambda: embeddings)
    monkeypatch.setattr(sandbox, "CACHE_DIR", tmp_path / "mpl")
    monkeypatch.setattr(chart_graph, "RETRY_INTERVAL", 0)
    holder["code"] = ScriptedModel(script=[chart_reply(GOOD)])
    holder["embeddings"] = embeddings
    ask.reset()
    uploads.reset()
    yield holder
    ask.reset()
    uploads.reset()
    users.reset_limiters()


def new_client() -> TestClient:
    return TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)


def sign_up(username: str, kind: str):
    client = new_client()
    code = users.create_invite(kind, 7)["code"]
    body = client.post("/api/auth/signup", json={"invite": code, "username": username, "password": PASSWORD}).json()
    assert client.post("/api/auth/signup/confirm", json={"signup_token": body["signup_token"]}).status_code == 200
    who = auth.resolve_user(f"{auth.COOKIE_NAME}={client.cookies[auth.COOKIE_NAME]}")
    return client, who


@pytest.fixture
def owner():
    """The owner, signed in through the real API, with every sealed column really sealed (encrypted = true)."""
    client, who = sign_up("prag", "owner")
    assert who.uid == userctx.OWNER_UID
    with db.connect_system("test: owner is encrypted") as conn:
        conn.execute("UPDATE users SET encrypted = true WHERE id = %s", (who.uid,))
    client.ctx = who.ctx
    return client


@pytest.fixture
def friend(owner):
    client, who = sign_up("friend", "friend")
    client.ctx = who.ctx
    return client


def raw(ctx, sql, params=()):
    """Rows exactly as stored (sealed values are NOT opened)."""
    with userctx.bound(ctx), db.connect() as conn:
        conn.row_factory = dict_row
        return conn.execute(sql, params).fetchall()


def seeded(owner):
    with userctx.bound(owner.ctx):
        seed_spending()


def statement(*narrations):
    return [StatementRow(date(2026, 10, 1), Decimal(str(100 + n)), "debit", text, None, None, n + 1) for n, text in enumerate(narrations)]


def an_import(owner, *narrations):
    with userctx.bound(owner.ctx):
        acc = tools.add_account("SBI", "1234")
        return import_rows(statement(*narrations), acc["id"], "x.csv").batch_id


def labels(*items):
    return {"labels": [{"i": i, "category": c, "confidence": conf, "why": "test"} for i, c, conf in items]}


def rag_grader(doc="yes"):
    return RuleModel(
        structured={"DocGrade": lambda t: {"relevance": doc, "reason": "x"},
                    "GroundCheck": lambda t: {"supported": "fully", "unsupported_claims": []},
                    "UseCheck": lambda t: {"useful": True, "score": 5},
                    "RouteDecision": lambda t: {"route": "knowledge", "reason": "a guide question"},
                    "QueryList": lambda t: {"queries": ["emergency fund months"]}},
        write=lambda t: "emergency fund size in months")


def new_thread(client):
    return client.post("/api/chat/threads", json={}).json()["thread_id"]


def send(client, tid, text, **extra):
    return client.post(f"/api/chat/threads/{tid}/messages", json={"text": text, **extra})


def wait_jobs(timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        if jobs._queue.unfinished_tasks == 0:
            return
        time.sleep(0.02)
    raise AssertionError("the worker did not finish")


def drain_queue():
    while not jobs._queue.empty():
        jobs._queue.get_nowait()
    jobs._owners.clear()


# ───────────────────────── threads keep the person ─────────────────────────

def test_the_chat_worker_thread_keeps_the_signed_in_person(owner, world):
    world["agent"].script = [call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}), AIMessage("Saved ₹250.00 at Swiggy.")]
    done = ndjson(send(owner, new_thread(owner), "spent 250 on swiggy"))[-1]
    assert done["type"] == "done", done
    assert raw(owner.ctx, "SELECT payee FROM transactions")[0]["payee"].startswith("rx1.")     # written in the worker thread, sealed
    with userctx.bound(owner.ctx):
        assert tools.list_transactions()[0]["payee"] == "Swiggy"                                 # and it opens for the owner


def test_no_thread_under_these_files_is_started_bare():
    root = Path(__file__).resolve().parents[1] / "rectifi"
    for rel in ("agent/jobs.py", "api/ask.py", "api/routes/charts.py", "api/routes/knowledge.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert not re.search(r"\bthreading\.Thread\(|\bThread\(|\bTimer\(", text), rel
        assert "userctx.spawn(" in text, rel


# ───────────────────────── chat, chart, library, classify: the owner, sealed ─────────────────────────

def test_chat_memory_stays_under_the_owner_namespace(owner):
    for state_uid, expected in ((userctx.OWNER_UID, "owner"), ("owner", "owner"), (None, "owner"), (7, "7")):
        assert chat_routes.uid_of(SimpleNamespace(state=SimpleNamespace(uid=state_uid))) == expected
    tid = new_thread(owner)
    events = ndjson(send(owner, tid, "remember that I prefer short answers"))
    assert next(e for e in events if e["type"] == "interrupt")["kind"] == "memory"
    ndjson(owner.post(f"/api/chat/threads/{tid}/resume", json={"value": True}))
    body = owner.get("/api/memory").json()
    assert [m["text"] for m in body["memories"]] == ["I prefer short answers"]


def test_a_chart_in_chat_is_drawn_from_sealed_transactions(owner):
    seeded(owner)
    assert raw(owner.ctx, "SELECT payee FROM transactions LIMIT 1")[0]["payee"].startswith("rx1.")
    reply = ndjson(send(owner, new_thread(owner), "/chart food by month"))[-1]["items"][-1]
    assert reply["route"] == "chart" and reply["artifact"]["chart_id"], reply
    with userctx.bound(owner.ctx):
        assert tools.get_chart(reply["artifact"]["chart_id"])["title"]


def test_the_charts_page_route_draws_in_its_worker_thread(owner):
    seeded(owner)
    events = ndjson(owner.post("/api/charts/draw", json={"request": "monthly food spend as a bar chart"}))
    assert events[-1]["type"] == "done", events[-1]
    assert len(owner.get("/api/charts").json()["items"]) == 1


def test_the_library_holds_guides_and_my_notes_but_no_ledger_text(owner):
    seeded(owner)
    with userctx.bound(owner.ctx):
        txn = tools.list_transactions()[0]["id"]
        tools.update_note(txn, f"{CANARY} private note")
        tools.save_commitment(f"{CANARY} commitment", date.today().isoformat())
    r = owner.post("/api/knowledge/docs", files={"file": ("mine.md", b"# My plan\nSave first, then spend what is left.\n")})
    assert r.status_code in (200, 201), r.text
    assert owner.post("/api/knowledge/reindex").json()["started"] is True
    knowledge.join_job(60)
    job = knowledge.job_snapshot()
    assert job["state"] == "done", job
    by = {c["corpus"]: c["n"] for c in raw(owner.ctx, "SELECT corpus, count(*) AS n FROM rag_chunks GROUP BY corpus")}
    assert by.get("guide", 0) > 0 and by.get("upload", 0) > 0 and "txn" not in by
    with userctx.bound(owner.ctx):
        assert tools.rag_note_rows() == []                                 # sealed notes and commitments are not copied out
    texts = raw(owner.ctx, "SELECT content, title FROM rag_documents")
    assert not any(CANARY in (d["content"] or "") + (d["title"] or "") for d in texts)
    assert not raw(owner.ctx, "SELECT 1 FROM rag_chunks WHERE content LIKE %s", (f"%{CANARY}%",))
    assert owner.get("/api/knowledge/status").json()["chunks"].get("txn", 0) == 0


def test_leftover_ledger_chunks_are_dropped_by_the_next_reindex(owner, world):
    seeded(owner)
    with userctx.bound(owner.ctx), db.connect() as conn:
        txn = conn.execute("SELECT id FROM transactions LIMIT 1").fetchone()["id"]
        conn.execute("INSERT INTO rag_chunks (corpus, txn_id, chunk_index, content, content_hash, embed_model, embedding) "
                     "VALUES ('txn', %s, 0, 'old copy', 'h', 'bow', %s)", (txn, [0.0] * 8))
    assert raw(owner.ctx, "SELECT 1 FROM rag_chunks WHERE corpus = 'txn'")
    with userctx.bound(owner.ctx):
        out = index.reindex(world["embeddings"], embed_model="bow")
    assert out["txn"] == {"added": 0, "skipped": 0, "removed": 1}
    assert raw(owner.ctx, "SELECT 1 FROM rag_chunks WHERE corpus = 'txn'") == []


def test_a_library_answer_cites_a_guide(owner, world):
    with userctx.bound(owner.ctx):
        index.reindex(world["embeddings"], embed_model=config.embed_model())
    world["grade"] = rag_grader()
    world["chat"] = ScriptedModel(script=[AIMessage(RAG_ANSWER)])
    events = ndjson(send(owner, new_thread(owner), "what is an emergency fund, and how many months should it cover?", mode="full"))
    assert events[-1]["type"] == "done", events[-1]
    reply = events[-1]["items"][-1]
    assert reply["route"] == "knowledge" and reply["citations"][0]["corpus"] == "guide"


def test_the_classify_job_runs_as_the_owner_in_the_worker_and_seals_its_reason(owner, world):
    batch = an_import(owner, "UPI/DR/111111111111/RAHUL SHARMA/HDFC/Pay", "UPI/DR/222222222222/MURUGAN AGENCIES/SBIN/Pay")
    with userctx.bound(owner.ctx):
        assert len(tools.pending_classification(batch)) == 2
    world["classify"].structured = [labels((1, "Shopping", 0.9), (2, "Shopping", 0.92))]
    r = owner.post("/api/classify/jobs", json={"batch_id": batch})
    assert r.status_code == 200, r.text
    job_id = r.json()["job"]["id"]
    wait_jobs()
    with userctx.bound(owner.ctx):
        job = tools.get_classify_job(job_id)
        assert job["status"] == "done", job
        assert {t["category"] for t in tools.batch_rows(batch)} == {"Shopping"}
    rows = raw(owner.ctx, "SELECT category_source, category_reason, payee FROM transactions WHERE import_batch_id = %s", (batch,))
    assert {r["category_source"] for r in rows} == {"llm"}
    assert all(r["category_reason"].startswith("rx1.") and r["payee"].startswith("rx1.") for r in rows)


def test_apply_classifications_and_apply_similar_seal_their_reasons(owner):
    batch = an_import(owner, "UPI/DR/111111111111/BLUE LOTUS/HDFC/Pay", "UPI/DR/222222222222/BLUE LOTUS/HDFC/Pay")
    with userctx.bound(owner.ctx):
        first, second = tools.pending_classification(batch)[:2]
        out = tools.apply_classifications([{"id": first["id"], "category": "Shopping", "confidence": 90, "reason": "sounds like a shop", "apply": True}])
        assert out["applied"] == 1
        assert tools.get_transaction(first["id"])["category_reason"] == "sounds like a shop"
        tools.set_category(first["id"], "Health")
        assert tools.apply_category_to_similar(first["id"]) == 1
    for ident in (first["id"], second["id"]):
        assert raw(owner.ctx, "SELECT category_reason FROM transactions WHERE id = %s", (ident,))[0]["category_reason"].startswith("rx1.")


# ───────────────────────── restart requeue and the sign-in hook ─────────────────────────

def test_requeue_does_nothing_with_nobody_bound_in_multiuser_mode(owner, monkeypatch):
    batch = an_import(owner, "UPI/DR/111111111111/RAHUL SHARMA/HDFC/Pay")
    with userctx.bound(owner.ctx):
        job_id = tools.create_classify_job(batch, "queued")
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    jobs.reset_for_tests()
    assert not userctx.is_bound()
    assert jobs.requeue_unfinished() == 0                      # start-up: no NoUser, and the once-per-process turn is kept
    with userctx.bound(owner.ctx):
        assert jobs.requeue_unfinished() == 1
    assert jobs._owners[job_id].uid == userctx.OWNER_UID and jobs._owners[job_id].key == owner.ctx.key
    drain_queue()


def test_the_owner_sign_in_hook_requeues_jobs_and_refreshes_the_guides(owner, friend, monkeypatch):
    batch = an_import(owner, "UPI/DR/111111111111/RAHUL SHARMA/HDFC/Pay")
    with userctx.bound(owner.ctx):
        job_id = tools.create_classify_job(batch, "queued")
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    calls = []
    monkeypatch.setattr(knowledge, "auto_reindex", lambda corpora=None: calls.append(corpora) or True)
    jobs.reset_for_tests()
    with userctx.bound(friend.ctx):
        hooks.ai_after_sign_in(friend.ctx)                     # a friend gets nothing
    assert calls == [] and jobs._queue.empty()
    with userctx.bound(owner.ctx):
        hooks.ai_after_sign_in(owner.ctx)
    assert job_id in list(jobs._queue.queue) and calls == [("guide", "note", "upload")]
    drain_queue()


def test_start_up_reindex_with_nobody_bound_does_nothing(owner):
    assert knowledge.start_reindex() is False
    assert knowledge.auto_reindex() is False


def test_laptop_mode_still_requeues_with_nobody_bound(monkeypatch):
    monkeypatch.delenv("RECTIFI_MULTIUSER")
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    jobs.reset_for_tests()
    assert jobs.requeue_unfinished() == 0 and jobs._requeued is True      # it ran (nothing to resume) and raised no NoUser
    jobs.reset_for_tests()


# ───────────────────────── a friend is refused everywhere in the AI part ─────────────────────────

OWNED = ("/api/ask", "/api/chat", "/api/charts", "/api/knowledge", "/api/memory", "/api/traces", "/api/models", "/api/classify",
         "/api/insights/explain", "/api/alerts/{alert_id}/explain")


def ai_routes():
    out = []
    for route in api_routes():
        path = route.path if route.path.startswith("/api") else "/api" + route.path
        if path.startswith(OWNED):
            for method in sorted(route.methods or []):
                if method not in ("HEAD", "OPTIONS"):
                    out.append((method, path))
    return out


def test_the_ai_routes_were_found():
    found = ai_routes()
    assert len(found) >= 35 and ("POST", "/api/charts/draw") in found and ("GET", "/api/classify/jobs/{job_id}") in found


@pytest.mark.parametrize("method,pattern", ai_routes())
def test_a_friend_gets_403_on_every_ai_route(friend, method, pattern):
    path = pattern.replace("{thread_id}", "t1").replace("{alert_id}", "1")
    for part in ("{run_id}", "{rule_id}", "{memory_id}", "{doc_id}", "{job_id}", "{batch_id}", "{chart_id:int}"):
        path = path.replace(part, "1")
    r = friend.request(method, path, json={})
    assert r.status_code == 403, (method, path, r.status_code, r.text[:200])


def test_a_friend_cannot_read_the_owners_import_rows_or_job(owner, friend, world):
    batch = an_import(owner, "UPI/DR/111111111111/RAHUL SHARMA/HDFC/Pay")
    with userctx.bound(owner.ctx):
        job_id = tools.create_classify_job(batch, "no_model", "off")
    assert friend.get(f"/api/imports/{batch}/rows").status_code == 404      # a friend may use the import page, never the owner's batch
    assert friend.get(f"/api/classify/jobs/{job_id}").status_code == 403


def test_a_friend_sees_none_of_the_owners_rows_through_the_ai_helpers(owner, friend):
    seeded(owner)
    with userctx.bound(friend.ctx):
        assert tools.list_transactions() == []
        assert tools.pending_classification() == []
        assert tools.rag_txn_rows() == []
        assert tools.unfinished_classify_jobs() == []
        assert tools.list_charts() == []
        assert tools.chart_frame(date(2020, 1, 1), date(2030, 1, 1))["transactions"] == []
