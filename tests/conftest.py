"""Tests run against a real Postgres database (TEST_DATABASE_URL), wiped before every test.

    TEST_DATABASE_URL=postgresql://rectifi:rectifi_dev@localhost:5432/rectifi_test pytest

Never point this at your real database: every test starts by deleting all tables.
"""

import os
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

# Read .env first, so TEST_DATABASE_URL set there is honoured.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("RECTIFI_NO_ALERT_BACKGROUND", "1")   # no alert ticker or sweep threads behind the tests; a test that wants them unsets it
os.environ.setdefault("RECTIFI_NO_AUTO_REINDEX", "1")      # app start / import must not start an embedding thread in tests

from fake_ollama import fake_ollama  # noqa: E402,F401  (the fixture: a fake Ollama server)

TEST_URL = os.getenv("TEST_DATABASE_URL", "postgresql://rectifi:rectifi_dev@localhost:5432/rectifi_test")


def _target(url: str) -> str:
    from rectifi.db import describe_target

    return describe_target(url)


def pytest_configure(config):
    config.addinivalue_line("markers", "ollama: needs a real Ollama server with models pulled; skipped unless you run `pytest -m ollama`")


def pytest_collection_modifyitems(config, items):
    """Tests marked `ollama` talk to a real Ollama. They run only when `-m` asks for them (and not as "not ollama")."""
    expr = (config.getoption("markexpr") or "").replace(" ", "")
    if "ollama" in expr and "notollama" not in expr:
        return
    skip = pytest.mark.skip(reason="needs a real Ollama server: run `pytest -m ollama`")
    for item in items:
        if "ollama" in item.keywords:
            item.add_marker(skip)


def pytest_sessionstart(session):
    """Stop at once, with a clear message, if the test database can't be used (instead of 30 s per test)."""
    from rectifi.db import describe_target  # noqa: F401  (import check)

    dbname = psycopg.conninfo.conninfo_to_dict(TEST_URL).get("dbname", "")
    if not dbname.endswith("_test"):
        pytest.exit(f"Refusing to run: the test database must end in '_test' (got {dbname!r}). "
                    "Tests delete every table, so they must never point at real data.", returncode=2)
    try:
        psycopg.connect(TEST_URL, connect_timeout=3).close()
    except psycopg.OperationalError:
        pytest.exit(
            f"Can't reach the test database at {_target(TEST_URL)}.\n"
            "Start Postgres:  python scripts/dev.py db   (Docker)\n"
            "On your own Postgres, run db/init.sql once to create rectifi_test. "
            "Check the setup with:  python scripts/dev.py doctor",
            returncode=2,
        )


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]
        if tables:
            conn.execute("DROP TABLE IF EXISTS " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")
    import sys

    from rectifi import db

    def close_chat_memory():          # the LangGraph pool points at tables that were just dropped
        module = sys.modules.get("rectifi.agent.checkpoint")
        if module is not None:
            module.close_memory()

    close_chat_memory()
    db.close_pool()
    db.init_db()
    yield
    close_chat_memory()
    db.close_pool()
