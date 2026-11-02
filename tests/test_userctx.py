"""userctx: who is asking, how it follows a thread, and that connect() uses it. Plus the ban on bare threads."""

import re
import threading
from pathlib import Path

import pytest
from support.tenants import as_user, make_user

from rectifi import db, userctx

ROOT = Path(__file__).resolve().parent.parent


def test_laptop_mode_with_nothing_bound_is_user_1_without_keys():
    ctx = userctx.current()
    assert ctx.uid == userctx.OWNER_UID == 1 and ctx.key is None and ctx.strict is False
    assert not userctx.is_bound()


def test_multiuser_mode_with_nothing_bound_raises(monkeypatch):
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    with pytest.raises(userctx.NoUser):
        userctx.current()
    with pytest.raises(userctx.NoUser):
        with db.connect():
            pass                                          # a forgotten binding fails closed instead of reading anyone's data
    with userctx.bound(userctx.UserContext(uid=1)):
        assert userctx.current().uid == 1
    with pytest.raises(userctx.NoUser):
        userctx.current()


def test_bound_always_resets_even_after_an_error():
    with pytest.raises(RuntimeError):
        with userctx.bound(userctx.UserContext(uid=7)):
            assert userctx.current().uid == 7
            raise RuntimeError("x")
    assert userctx.current().uid == 1
    with userctx.bound(userctx.UserContext(uid=2)):
        with userctx.bound(userctx.UserContext(uid=3)):
            assert userctx.current().uid == 3
        assert userctx.current().uid == 2


def test_a_bare_thread_loses_the_binding_but_spawn_keeps_it(monkeypatch):
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    seen = {}

    def work(label):
        try:
            seen[label] = userctx.current().uid
        except userctx.NoUser:
            seen[label] = "no user"

    with userctx.bound(userctx.UserContext(uid=5)):
        bare = threading.Thread(target=work, args=("bare",))
        bare.start()
        bare.join()
        spawned = userctx.spawn(work, "spawned", name="test-spawn")
        spawned.join()
    assert seen == {"bare": "no user", "spawned": 5}
    assert spawned.name == "test-spawn"


def test_connect_uses_the_bound_person():
    friend = make_user("friend")
    with as_user(friend), db.connect() as c:
        assert c.execute("SELECT current_setting('app.user_id') AS v").fetchone()["v"] == str(friend.uid)
        c.execute("INSERT INTO accounts (name) VALUES ('mine')")
    with db.connect() as c:                               # back to user 1
        assert c.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 0
    with as_user(friend), db.connect() as c:
        assert c.execute("SELECT user_id FROM accounts").fetchone()["user_id"] == friend.uid


def test_a_new_person_is_seeded_with_their_own_categories_and_rules():
    friend = make_user("friend")
    with as_user(friend), db.connect() as c:
        assert c.execute("SELECT count(*) AS n FROM categories").fetchone()["n"] == len(db.DEFAULT_CATEGORIES)
        assert c.execute("SELECT count(*) AS n FROM merchant_rules").fetchone()["n"] > 50
        assert c.execute("SELECT count(*) AS n FROM app_settings WHERE key = %s", (db.MIGRATION_MARKER,)).fetchone()["n"] == 1
    with db.connect() as c:                               # and user 1's are separate rows
        ids_1 = {r["id"] for r in c.execute("SELECT id FROM categories").fetchall()}
    with as_user(friend), db.connect() as c:
        assert ids_1.isdisjoint({r["id"] for r in c.execute("SELECT id FROM categories").fetchall()})
    db.seed_user(friend.uid)                              # safe to run again
    with as_user(friend), db.connect() as c:
        assert c.execute("SELECT count(*) AS n FROM categories").fetchone()["n"] == len(db.DEFAULT_CATEGORIES)


# Files under rectifi/ that still start a bare thread. Each workstream removes its own entries when it moves to userctx.spawn
# (WS-AI: ask.py, routes/charts.py, routes/knowledge.py, agent/jobs.py; WS-ALERTS: alerts.py). The list may only shrink.
KNOWN_BARE_THREADS: set[str] = set()


def test_no_new_bare_threads_under_rectifi():
    found = set()
    for path in (ROOT / "rectifi").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "rectifi/userctx.py":
            continue
        if re.search(r"\bthreading\.Thread\(|\bThread\(|\bTimer\(", path.read_text(encoding="utf-8")):
            found.add(rel)
    assert found <= KNOWN_BARE_THREADS, f"Start threads with userctx.spawn, not threading.Thread: {sorted(found - KNOWN_BARE_THREADS)}"
