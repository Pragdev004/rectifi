"""The demo seeder must never touch a real database and must never double the data."""

import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "scripts" / "seed_demo.py"


def seed(url, *args):
    import os

    env = {**os.environ, "DATABASE_URL": url, "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, str(SEED), *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)


def count(url):
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


@pytest.fixture
def scratch_demo_db(fresh_db):
    """A throwaway database called rectifi_pytest_demo, dropped afterwards."""
    import os

    base = os.environ["DATABASE_URL"]
    admin = base.rsplit("/", 1)[0] + "/postgres"
    name = "rectifi_pytest_demo"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {name}")
    url = base.rsplit("/", 1)[0] + "/" + name
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_seed_refuses_a_database_not_named_demo():
    import os

    url = os.environ["DATABASE_URL"]            # ends in _test
    result = seed(url)
    assert result.returncode == 2
    assert "Refusing" in result.stderr and "_demo" in result.stderr
    assert count(url) == 0


def test_seed_runs_once_and_second_run_does_nothing(scratch_demo_db):
    first = seed(scratch_demo_db)
    assert first.returncode == 0, first.stderr
    rows = count(scratch_demo_db)
    assert rows > 40
    second = seed(scratch_demo_db)
    assert second.returncode == 0 and "Nothing to do" in second.stdout
    assert count(scratch_demo_db) == rows


def test_seed_demo_has_a_gap_and_a_review(scratch_demo_db, monkeypatch):
    assert seed(scratch_demo_db).returncode == 0
    monkeypatch.setenv("DATABASE_URL", scratch_demo_db)
    from rectifi import db, tools

    db.close_pool()
    r = tools.reconcile("Demo Savings")
    assert r["gap"] == -120
    review = tools.weekly_review()
    assert review["count"] > 0 and review["prev_spending"] > 0
    db.close_pool()
