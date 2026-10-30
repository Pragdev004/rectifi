"""/api/knowledge: list, add, remove, status and the background re-index."""

import pytest
from api_support import make_client

from rectifi import tools
from rectifi.api.routes import knowledge
from rectifi.db import connect
from support.fakes import BowEmbeddings


@pytest.fixture
def client():
    return make_client()


@pytest.fixture(autouse=True)
def bow(monkeypatch):
    monkeypatch.setattr("rectifi.rag.embeddings.get_rag_embeddings", lambda: BowEmbeddings())
    knowledge.join_job()
    knowledge._job.update(state="idle", done=0, total=0, error=None, result=None)
    yield
    knowledge.join_job()


def wait(client):
    knowledge.join_job()
    return client.get("/api/knowledge/status").json()


def test_guides_are_listed_before_and_after_the_first_index(client):
    docs = client.get("/api/knowledge/docs").json()["docs"]
    guides = [d for d in docs if d["corpus"] == "guide"]
    assert len(guides) >= 12 and all(d["builtin"] and d["id"] is None and d["chunks"] == 0 and d["label"] == "GUIDE" for d in guides)
    assert client.post("/api/knowledge/reindex").json()["started"] is True
    st = wait(client)
    assert st["job"]["state"] == "done" and st["chunks"]["guide"] >= 12 and st["guides_on_disk"] == len(guides)
    guides = [d for d in client.get("/api/knowledge/docs").json()["docs"] if d["corpus"] == "guide"]
    assert all(d["id"] and d["chunks"] >= 1 and d["indexed"] for d in guides)


def test_status_reports_the_embedding_model_and_job(client):
    st = client.get("/api/knowledge/status").json()
    assert st["embed_model"] == "nomic-embed-text" and st["chunks_total"] == 0 and st["job"]["state"] == "idle"
    client.post("/api/knowledge/reindex")
    done = wait(client)
    assert done["job"]["state"] == "done" and done["job"]["result"]["guide"]["added"] >= 12 and done["updated_at"]
    again = client.post("/api/knowledge/reindex").json()
    assert again["started"] is True
    assert wait(client)["job"]["result"]["guide"]["added"] == 0                 # nothing changed, nothing embedded


def test_a_second_reindex_while_one_runs_is_refused(client):
    knowledge._job["state"] = "running"
    assert client.post("/api/knowledge/reindex").json()["started"] is False
    knowledge._job["state"] = "idle"


def test_a_failing_embedding_model_gives_a_plain_reason(client, monkeypatch):
    def boom():
        raise ConnectionError("connection refused")

    monkeypatch.setattr("rectifi.rag.embeddings.get_rag_embeddings", boom)
    client.post("/api/knowledge/reindex")
    job = wait(client)["job"]
    assert job["state"] == "failed" and job["error"] and "Traceback" not in job["error"]


def test_add_a_note_then_remove_it(client):
    r = client.post("/api/knowledge/docs", files={"file": ("flat rules.md", b"# Flat rules\nThe deposit comes back after notice.")})
    assert r.status_code == 201
    body = r.json()
    assert body["indexed"] is True and body["doc"]["label"] == "YOUR FILE" and body["doc"]["chunks"] >= 1 and not body["doc"]["builtin"]
    doc_id = body["doc"]["id"]
    assert [d["title"] for d in client.get("/api/knowledge/docs").json()["docs"] if d["corpus"] == "upload"] == ["Flat rules"]
    again = client.post("/api/knowledge/docs", files={"file": ("flat rules.md", b"# Flat rules\nThe deposit comes back after notice.")})
    assert again.status_code == 201 and again.json()["doc"]["id"] == doc_id          # same file name replaces, never doubles
    assert client.delete(f"/api/knowledge/docs/{doc_id}").json() == {"deleted": doc_id}
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rag_chunks WHERE corpus = 'upload'").fetchone()["n"] == 0
    assert client.delete(f"/api/knowledge/docs/{doc_id}").status_code == 404


def test_statement_looking_and_other_files_are_refused(client):
    statement = "\n".join(f"0{d}-08-2026  UPI-SHOP  {d}00.00  {d}0,500.00" for d in range(1, 8)).encode()
    for name, data in [("s.txt", statement), ("s.csv", b"a,b\n1,2"), ("s.pdf", b"%PDF-1.4"), ("empty.md", b" ")]:
        r = client.post("/api/knowledge/docs", files={"file": (name, data)})
        assert r.status_code == 422 and r.json()["error"]["field"] == "file", name
    assert "bank statement" in client.post("/api/knowledge/docs", files={"file": ("s.txt", statement)}).json()["error"]["message"]
    assert client.get("/api/knowledge/docs").json()["docs"] == [d for d in client.get("/api/knowledge/docs").json()["docs"] if d["corpus"] == "guide"]
    assert client.post("/api/knowledge/docs", json={}).status_code == 415
    assert client.post("/api/knowledge/docs", data={"x": "1"}).status_code in (415, 422)


def test_a_file_is_kept_even_when_the_embedding_model_is_down(client, monkeypatch):
    def boom():
        raise ConnectionError("down")

    monkeypatch.setattr("rectifi.rag.embeddings.get_rag_embeddings", boom)
    r = client.post("/api/knowledge/docs", files={"file": ("n.txt", b"remember the deposit")})
    assert r.status_code == 201 and r.json()["indexed"] is False and r.json()["note"] and r.json()["doc"]["chunks"] == 0
    assert tools.rag_list_documents("upload")


def test_guides_cannot_be_removed(client):
    client.post("/api/knowledge/reindex")
    wait(client)
    guide = next(d for d in client.get("/api/knowledge/docs").json()["docs"] if d["corpus"] == "guide")
    r = client.delete(f"/api/knowledge/docs/{guide['id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "builtin"
    assert client.delete("/api/knowledge/docs/abc").status_code == 404


def test_the_guard_headers_still_apply(client):
    bare = make_client(headers={})
    assert bare.get("/api/knowledge/status").status_code in (400, 403)
    assert bare.post("/api/knowledge/reindex").status_code in (400, 403)
