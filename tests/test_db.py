import time

import pytest

from rectifi import db


def test_db_unreachable_fails_fast(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://rectifi:topsecretpw@127.0.0.1:9/nodb")
    db.close_pool()
    started = time.monotonic()
    with pytest.raises(db.DatabaseUnavailable) as err:
        db.get_pool()
    assert time.monotonic() - started < 8
    message = str(err.value)
    assert "topsecretpw" not in message
    assert "127.0.0.1:9/nodb" in message
    assert "scripts/dev.py db" in message


def test_pool_recovers_after_failure(monkeypatch):
    """A failed start must not be remembered: once the URL is good again, it just works."""
    real = db.config.database_url()
    monkeypatch.setenv("DATABASE_URL", "postgresql://rectifi:x@127.0.0.1:9/nodb")
    with pytest.raises(db.DatabaseUnavailable):
        db.get_pool()
    monkeypatch.setenv("DATABASE_URL", real)
    with db.connect() as conn:
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1


def test_describe_target_hides_password():
    assert db.describe_target("postgresql://u:pw@db.local:5433/mine") == "db.local:5433/mine"
