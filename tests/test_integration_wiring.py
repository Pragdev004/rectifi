"""Things only the integration adds: the session lock is installed by create_app, the uid is set for memory, the app start and an import
refresh the library index and resume sorting jobs, the Ledger filters by how a category was chosen."""

import pytest
from api_support import make_client
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from rectifi import tools
from rectifi.api import auth as api_auth
from rectifi.api import security
from rectifi.api.app import create_app
from rectifi.api.routes import imports, knowledge


def test_a_public_app_refuses_to_start_without_a_login(monkeypatch):
    monkeypatch.setenv("RECTIFI_PUBLIC", "1")
    monkeypatch.delenv("RECTIFI_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("RECTIFI_SESSION_SECRET", raising=False)
    with pytest.raises(api_auth.StartupRefused):
        create_app()


def test_the_local_app_starts_with_the_session_lock_installed_but_asleep():
    app = create_app()
    assert any(m.cls is security.SessionLock for m in app.user_middleware)
    assert make_client().get("/api/health").status_code == 200       # no password set: nothing is asked


def test_the_session_lock_sets_the_uid_for_memory():
    async def who(request):
        return JSONResponse({"uid": request.state.uid})

    app = Starlette(routes=[Route("/api/who", who)])
    app.add_middleware(security.SessionLock)
    assert TestClient(app).get("/api/who").json() == {"uid": "owner"}


def test_app_start_resumes_sorting_and_refreshes_the_library_index(monkeypatch):
    calls = []
    monkeypatch.delenv("RECTIFI_NO_AUTO_REINDEX", raising=False)
    monkeypatch.setattr(knowledge, "start_reindex", lambda corpora=(): calls.append(("reindex", tuple(corpora))) or True)
    monkeypatch.setattr("rectifi.agent.jobs.requeue_unfinished", lambda: calls.append(("requeue",)) or 0)
    with TestClient(create_app(), base_url="http://localhost:8501"):
        pass
    assert ("requeue",) in calls and ("reindex", ("guide", "note", "upload", "txn")) in calls


def test_tests_can_switch_the_automatic_reindex_off(monkeypatch):
    monkeypatch.setenv("RECTIFI_NO_AUTO_REINDEX", "1")
    monkeypatch.setattr(knowledge, "start_reindex", lambda *a: pytest.fail("must not start"))
    assert knowledge.auto_reindex() is False


def test_an_import_refreshes_only_the_payments_part_of_the_index(monkeypatch):
    calls = []
    monkeypatch.delenv("RECTIFI_NO_AUTO_REINDEX", raising=False)
    monkeypatch.setattr(knowledge, "start_reindex", lambda corpora=(): calls.append(tuple(corpora)) or True)
    imports.start_sorting(1)
    assert calls == [("txn",)]


def test_the_ledger_filters_by_how_the_category_was_chosen():
    a = tools.add_transaction("100", "debit", "2026-10-01", payee="Swiggy", category="Food & Dining")["transaction"]
    b = tools.add_transaction("200", "debit", "2026-10-02", payee="Zzz Unknown", category="Miscellaneous")["transaction"]
    from rectifi.db import connect

    with connect() as conn:
        conn.execute("UPDATE transactions SET category_source = 'rule' WHERE id = %s", (a["id"],))
        conn.execute("UPDATE transactions SET category_source = 'fallback' WHERE id = %s", (b["id"],))
        conn.execute("INSERT INTO transactions (txn_date, amount, direction, payee, category_id, source) "
                     "SELECT '2026-10-03', 5, 'debit', 'Old row', category_id, 'manual' FROM transactions WHERE id = %s", (a["id"],))
        conn.execute("UPDATE transactions SET category_source = NULL WHERE payee = 'Old row'")
    assert [t["payee"] for t in tools.list_transactions(source="rule")] == ["Swiggy"]
    assert [t["payee"] for t in tools.list_transactions(source="fallback")] == ["Zzz Unknown"]
    assert [t["payee"] for t in tools.list_transactions(source="earlier")] == ["Old row"]
    with pytest.raises(tools.RectifiError):
        tools.list_transactions(source="vibes")
    client = make_client()
    got = client.get("/api/transactions?source=fallback").json()
    assert [t["payee"] for t in got["items"]] == ["Zzz Unknown"]
    assert client.get("/api/transactions?source=vibes").status_code == 422
