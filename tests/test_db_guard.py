"""While pytest runs, no code in this process may open a database that is not a scratch one (*_test or *_demo).

A test once called monkeypatch.undo(), which also dropped fresh_db's DATABASE_URL, and its next query went to the real
database named in .env. db.guard_target now refuses such a database before any connection is opened, on every path."""

import re
import sys
import threading
from pathlib import Path

import psycopg
import pytest

from rectifi import db

ROOT = Path(__file__).resolve().parent.parent
REAL = "postgresql://rectifi:topsecretpw@127.0.0.1:5432/rectifi"


@pytest.fixture
def opened(monkeypatch):
    """Records every attempt to open a connection or a pool. The guarded calls must leave it empty."""
    calls = []
    real_connect = psycopg.connect

    def spy_connect(*a, **k):
        calls.append(("psycopg.connect", a, k))
        return real_connect(*a, **k)

    class SpyPool:
        def __init__(self, *a, **k):
            calls.append(("ConnectionPool", a, k))
            raise AssertionError("a pool was created for a refused database")

    monkeypatch.setattr(psycopg, "connect", spy_connect)
    monkeypatch.setattr(db, "ConnectionPool", SpyPool)
    from rectifi.agent import checkpoint
    monkeypatch.setattr(checkpoint, "ConnectionPool", SpyPool)
    return calls


def point_at_real(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", REAL)


def test_connect_is_refused_before_any_connection_is_opened(monkeypatch, opened):
    point_at_real(monkeypatch)
    with pytest.raises(db.UnsafeTestDatabase) as raised:
        with db.connect() as conn:
            conn.execute("SELECT 1")
    assert opened == []
    assert "'rectifi'" in str(raised.value) and "topsecretpw" not in str(raised.value)


def test_connect_system_and_raw_connect_are_refused(monkeypatch, opened):
    point_at_real(monkeypatch)
    with pytest.raises(db.UnsafeTestDatabase):
        with db.connect_system("test: guard"):
            pass
    with pytest.raises(db.UnsafeTestDatabase):
        db.raw_connect()
    with pytest.raises(db.UnsafeTestDatabase):
        db.raw_connect(REAL)
    assert opened == []


def test_a_cached_test_pool_is_not_reused_after_the_url_changes(monkeypatch):
    with db.connect() as conn:                           # opens (or reuses) the pool for the test database
        assert conn.execute("SELECT current_database() AS d").fetchone()["d"].endswith("_test")
    point_at_real(monkeypatch)
    with pytest.raises(db.UnsafeTestDatabase):
        with db.connect() as conn:
            conn.execute("SELECT 1")


def test_chat_memory_and_the_langgraph_checkpointer_are_refused(monkeypatch, opened):
    from rectifi.agent import checkpoint, graph

    point_at_real(monkeypatch)
    with pytest.raises(db.UnsafeTestDatabase):
        checkpoint.get_memory()
    with pytest.raises(db.UnsafeTestDatabase):
        graph.postgres_checkpointer()
    assert opened == []


def test_the_api_app_and_a_background_thread_are_refused(monkeypatch, opened):
    from starlette.testclient import TestClient

    from rectifi.api import inputs
    from rectifi.api.app import create_app

    point_at_real(monkeypatch)
    monkeypatch.setattr(inputs, "_ready_url", None)
    try:                                                 # the app's start-up work or the first request: either is refused
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.get("/api/overview", headers={"X-Rectifi-Request": "1"})
        assert response.status_code != 200 and "topsecretpw" not in response.text
    except db.UnsafeTestDatabase as exc:
        assert "topsecretpw" not in str(exc)

    errors = []

    def work():
        try:
            with db.connect() as conn:
                conn.execute("SELECT 1")
        except BaseException as exc:                     # noqa: BLE001 (collected and checked below)
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(10)
    assert len(errors) == 1 and isinstance(errors[0], db.UnsafeTestDatabase)
    assert opened == []


@pytest.mark.parametrize("name", ["rectifi_test", "rectifi_demo", "anything_test"])
def test_scratch_databases_are_allowed(name):
    url = f"postgresql://u:p@localhost:5432/{name}"
    assert db.guard_target(url) == url


@pytest.mark.parametrize("name", ["rectifi", "rectifi_testing", "test_rectifi", "postgres", ""])
def test_other_names_are_refused(name):
    with pytest.raises(db.UnsafeTestDatabase):
        db.guard_target(f"postgresql://u:p@localhost:5432/{name}")


def test_each_trigger_alone_turns_the_guard_on(monkeypatch):
    real = "postgresql://u:p@localhost:5432/rectifi"
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert db.guard_target(real) == real                 # outside tests the real database is fine
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::y (call)")
    with pytest.raises(db.UnsafeTestDatabase):
        db.guard_target(real)
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    monkeypatch.setitem(sys.modules, "pytest", pytest)
    with pytest.raises(db.UnsafeTestDatabase):
        db.guard_target(real)


def test_every_raw_connection_in_the_code_goes_through_the_guard():
    """psycopg.connect only in db.raw_connect; a ConnectionPool only where its URL came from guard_target."""
    files = list((ROOT / "rectifi").rglob("*.py")) + list((ROOT / "scripts" / "devtools").glob("*.py"))
    raw, pools = [], []
    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT).as_posix()
        raw += [rel for _ in re.finditer(r"psycopg\.connect\(", text)]
        pools += [rel for _ in re.finditer(r"\bConnectionPool\(", text)]
    assert raw == ["rectifi/db.py"], raw
    assert sorted(pools) == ["rectifi/agent/checkpoint.py", "rectifi/db.py"], pools
    for rel in ("rectifi/db.py", "rectifi/agent/checkpoint.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert re.search(r"url = guard_target\(config\.database_url\(\)\)", text), rel
