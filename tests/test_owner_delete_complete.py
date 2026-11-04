"""Owner account delete leaves nothing behind (review round 1 LOW, round 4 gap).

Before: `delete_account` for the owner removed every tenant row, but left the LangGraph long-term memory (`store`) and the
checkpoints of any chat thread that was no longer listed in `conversations`. PRIVACY.md said "every row you own in every
table", which was not true for the owner. These tests walk EVERY public table.

Review fixes: the delete is one transaction that locks the people tables and revokes the owner's sessions FIRST; a chat turn
that was running is stopped and a last sweep clears what it wrote.
"""

import operator
import threading
import time
import uuid
from typing import Annotated, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from starlette.testclient import TestClient
from support.tenants import as_user, populate

from rectifi import account, db, hooks, sealed_fields, userctx, users
from rectifi.agent import memory as mem
from rectifi.agent.checkpoint import get_memory
from rectifi.api import auth
from rectifi.api.app import create_app

SECRET = "s" * 48
HEADERS = {"X-Rectifi-Request": "1"}
PASSWORD = "correct horse battery"
PEOPLE_TABLES = {"users", "invites", "user_sessions", "allowed_merchants"}
RESEEDED = {"categories", "merchant_rules", "app_settings"}          # seed_user puts the defaults back after an owner wipe
MIGRATIONS = {"checkpoint_migrations", "store_migrations", "vector_migrations"}      # schema versions only, no user data
LIBRARY = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "store", "store_vectors")
CLIENTS: dict[str, TestClient] = {}          # the browser each test person signed up with, by username


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", SECRET)
    monkeypatch.setattr(hooks, "after_sign_in", lambda ctx: None)
    users.reset_limiters()
    CLIENTS.clear()

    async def no_pause(seconds):
        return None

    monkeypatch.setattr(auth, "pause", no_pause)
    yield
    users.reset_limiters()


def sign_up(username: str, kind="friend"):
    client = CLIENTS[username] = TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)
    code = users.create_invite(kind)["code"]
    body = client.post("/api/auth/signup", json={"invite": code, "username": username, "password": PASSWORD}).json()
    assert client.post("/api/auth/signup/confirm", json={"signup_token": body["signup_token"]}).status_code == 200
    attempt = users.login(username, PASSWORD)
    assert attempt.status == "ok"
    with db.connect_system("test: switch the guard off") as conn:
        conn.execute("UPDATE users SET encrypted = false WHERE id = %s", (attempt.signed_in.ctx.uid,))   # populate() writes plain rows
    return attempt.signed_in.ctx


class _S(TypedDict):
    log: Annotated[list, operator.add]


def make_thread() -> str:
    """A real LangGraph run on the real Postgres saver: rows in checkpoints, checkpoint_blobs and checkpoint_writes."""
    builder = StateGraph(_S)
    builder.add_node("a", lambda s: {"log": ["a"]})
    builder.add_node("b", lambda s: {"log": ["b"]})
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    thread = str(uuid.uuid4())
    builder.compile(checkpointer=get_memory().saver).invoke({"log": ["start"]}, {"configurable": {"thread_id": thread}})
    return thread


def seed_owner(ctx) -> dict:
    """Every kind of owner data: tenant rows (populate), settings, a custom category, a long-term memory, a LISTED thread, an
    UNLISTED thread."""
    ids = populate(ctx)
    with as_user(ctx), db.connect() as conn:
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('owner_pref', '\"x\"'::jsonb)")
        conn.execute("INSERT INTO categories (name, kind) VALUES ('Owner Custom', 'expense')")
    memory = get_memory()
    mem.add_memory(memory.store, "owner", "fact", "I am a student")
    listed, unlisted = make_thread(), make_thread()
    with as_user(ctx), db.connect() as conn:
        conn.execute("INSERT INTO conversations (thread_id, title) VALUES (%s, 'listed')", (listed,))
    assert not conn_has_conversation(ctx, unlisted)
    ids.update(listed=listed, unlisted=unlisted)
    return ids


def conn_has_conversation(ctx, thread: str) -> bool:
    with as_user(ctx), db.connect() as conn:
        return conn.execute("SELECT 1 FROM conversations WHERE thread_id = %s", (thread,)).fetchone() is not None


def library_counts() -> dict:
    out = {}
    with db.connect_system("test: count the library tables") as conn:
        for table in LIBRARY:
            exists = conn.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",)).fetchone()["t"]
            out[table] = conn.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()["n"] if exists else 0
    return out


def tenant_counts(uid: int) -> dict:
    out = {}
    for table in db.TENANT_TABLES:
        with as_user(uid), db.connect() as conn:
            out[table] = conn.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()["n"]
    return out


def delete(ctx, name: str) -> None:
    """A friend deletes themselves; the owner is reset on the server (Rectifi stopped), as `dev.py reset-owner` does."""
    if ctx.uid == userctx.OWNER_UID:
        assert account.reset_owner()
    else:
        assert account.delete_account(ctx.uid, name, PASSWORD, name) == {"ok": True}


def public_tables() -> list[str]:
    with db.connect_system("test: list tables") as conn:
        return [r["tablename"] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]


def assert_owner_has_nothing_left() -> None:
    assert library_counts() == {t: 0 for t in LIBRARY}
    counts = tenant_counts(1)
    for table in set(db.TENANT_TABLES) - RESEEDED:
        assert counts[table] == 0, f"{table} still has owner rows"


def test_the_seed_really_puts_data_in_every_owner_place():
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    counts = library_counts()
    assert counts["store"] >= 1 and counts["checkpoints"] >= 2 and counts["checkpoint_writes"] >= 1 and counts["checkpoint_blobs"] >= 1
    assert all(n > 0 for n in tenant_counts(1).values())


def test_the_owner_wipe_never_touches_a_friends_rows():
    """delete_account refuses while a friend exists, so this goes one level down: the wipe itself stays inside user 1's rows."""
    owner = sign_up("owner-person", kind="owner")
    friend = sign_up("friend-person")
    seed_owner(owner)
    populate(friend, "fr")
    before_friend = tenant_counts(friend.uid)
    assert all(n > 0 for n in before_friend.values())

    before_library = library_counts()
    with pytest.raises(users.AuthError) as refused:
        delete(owner, "owner-person")
    assert refused.value.code == "owner_has_friends"
    assert library_counts() == before_library and tenant_counts(friend.uid) == before_friend and tenant_counts(1)["transactions"] > 0

    with userctx.bound(userctx.UserContext(uid=1, strict=False)):
        account._wipe_owner_rows()                                    # noqa: SLF001
    assert tenant_counts(friend.uid) == before_friend
    assert tenant_counts(1)["transactions"] == 0


def test_deleting_the_owner_leaves_no_owner_row_in_any_table():
    owner = sign_up("owner-person", kind="owner")
    friend = sign_up("friend-person")
    assert owner.uid == 1 and friend.uid != 1
    seed_owner(owner)
    delete(friend, "friend-person")                    # the owner can only go when nobody else is left
    delete(owner, "owner-person")

    tables = set(public_tables())
    assert tables, "no tables found"
    checked = []
    for table in sorted(tables):
        if table in PEOPLE_TABLES or table in MIGRATIONS:
            continue
        if table in LIBRARY:
            continue
        assert table in db.TENANT_TABLES, f"{table}: not a tenant table, a library table or a people table; decide where it belongs"
        checked.append(table)
        if table in RESEEDED:
            continue
        with as_user(1), db.connect() as conn:
            assert conn.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()["n"] == 0, f"{table} still has owner rows"
    assert set(checked) == set(db.TENANT_TABLES)

    # what seed_user puts back is only the defaults: no custom setting, no learned rule, no custom category
    with as_user(1), db.connect() as conn:
        assert {r["name"] for r in conn.execute("SELECT name FROM categories").fetchall()} == {n for n, _ in db.DEFAULT_CATEGORIES}
        assert conn.execute("SELECT count(*) AS n FROM categories WHERE name = 'Owner Custom'").fetchone()["n"] == 0
        assert [r["key"] for r in conn.execute("SELECT key FROM app_settings").fetchall()] == ["categories_v2"]
        assert conn.execute("SELECT count(*) AS n FROM merchant_rules WHERE source <> 'default'").fetchone()["n"] == 0

    # the library tables: nothing at all, in every table that exists (store, store_vectors, all checkpoint tables)
    assert library_counts() == {t: 0 for t in LIBRARY}
    with db.connect_system("test: nothing in the owner namespace") as conn:
        assert conn.execute("SELECT count(*) AS n FROM store WHERE prefix LIKE %s", ("users.owner%",)).fetchone()["n"] == 0
    assert mem.list_memories(get_memory().store, "owner") == []
    # no thread of any kind is left, listed or not
    with db.connect_system("test: threads") as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            assert conn.execute(f'SELECT count(DISTINCT thread_id) AS n FROM "{table}"').fetchone()["n"] == 0

    # the people tables: the row is reset and the owner's invite and sessions are gone
    with db.connect_system("test: people") as conn:
        row = conn.execute("SELECT username, verifier, blind_fp, public_key, ingest_salt, encrypted FROM users WHERE id = 1").fetchone()
        assert dict(row) == {"username": "owner", "verifier": None, "blind_fp": None, "public_key": None, "ingest_salt": None,
                             "encrypted": False}
        assert conn.execute("SELECT count(*) AS n FROM user_sessions WHERE user_id = 1").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM invites WHERE reserved_by = 1").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM users WHERE id <> 1").fetchone()["n"] == 0


def test_a_friend_delete_leaves_every_owner_row_and_the_library_tables_alone():
    owner = sign_up("owner-person", kind="owner")
    friend = sign_up("friend-person")
    seed_owner(owner)
    populate(friend, "fr")
    owner_before, library_before = tenant_counts(1), library_counts()
    assert library_before["store"] >= 1 and library_before["checkpoints"] >= 2

    delete(friend, "friend-person")

    assert tenant_counts(1) == owner_before
    assert library_counts() == library_before
    assert len(mem.list_memories(get_memory().store, "owner")) == 1
    with db.connect_system("test: the friend is gone") as conn:
        assert conn.execute("SELECT count(*) AS n FROM users WHERE username = 'friend-person'").fetchone()["n"] == 0


def test_the_library_table_list_matches_what_the_row_level_security_test_calls_library_tables():
    """If LangGraph adds a table, the RLS test already fails on it; this makes the delete list follow."""
    assert set(account.OWNER_LIBRARY_DATA) == set(LIBRARY)
    assert set(account.OWNER_LIBRARY_DATA) | MIGRATIONS == set(sealed_fields.LIBRARY_TABLES)
    assert userctx.OWNER_UID == 1


# ───────────── review fixes: one transaction, sessions first, no straggler survives ─────────────

def test_the_sessions_are_revoked_before_the_wipe_and_an_old_cookie_is_refused_afterwards(monkeypatch):
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    client = CLIENTS["owner-person"]
    assert client.get("/api/meta").status_code == 200
    real, seen = account._wipe_owner_rows_on, {}                                  # noqa: SLF001

    def spy(conn):
        if seen:                                       # the straggler sweep calls it again
            return real(conn)
        seen["sessions"] = conn.execute("SELECT count(*) AS n FROM user_sessions WHERE user_id = 1").fetchone()["n"]
        seen["epoch"] = conn.execute("SELECT session_epoch FROM users WHERE id = 1").fetchone()["session_epoch"]
        real(conn)

    with db.connect_system("test: epoch before") as conn:
        epoch_before = conn.execute("SELECT session_epoch FROM users WHERE id = 1").fetchone()["session_epoch"]
    monkeypatch.setattr(account, "_wipe_owner_rows_on", spy)
    delete(owner, "owner-person")
    assert seen["sessions"] == 0 and seen["epoch"] == epoch_before + 1           # revoked first, inside the same transaction
    assert client.get("/api/meta").status_code == 401
    assert client.post("/api/memory", json={"kind": "fact", "text": "late write"}).status_code == 401
    assert_owner_has_nothing_left()






def test_a_sign_up_cannot_slip_in_between_the_friend_check_and_the_wipe(monkeypatch):
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    code = users.create_invite("friend")["code"]
    real, outcome = account._wipe_owner_rows_on, {}                              # noqa: SLF001

    def joiner():
        try:
            users.signup(code, "late-friend", PASSWORD)
            outcome["done"] = True
        except Exception as exc:                       # noqa: BLE001
            outcome["error"] = type(exc).__name__

    def wipe_while_a_friend_tries_to_join(conn):
        if outcome:                                    # the straggler sweep calls it again
            return real(conn)
        thread = threading.Thread(target=joiner)
        thread.start()
        outcome["thread"] = thread
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:             # the sign-up is queued behind our table lock
            with db.connect_system("test: who waits") as other:
                if other.execute("SELECT count(*) AS n FROM pg_locks WHERE NOT granted").fetchone()["n"]:
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("the sign-up was not blocked by the delete's lock")
        assert "done" not in outcome and thread.is_alive()
        real(conn)

    monkeypatch.setattr(account, "_wipe_owner_rows_on", wipe_while_a_friend_tries_to_join)
    delete(owner, "owner-person")
    outcome["thread"].join(30)
    assert not outcome["thread"].is_alive()
    # the friend joined only after the delete committed, so the delete saw no friend and left a clean, unclaimed row 1
    assert_owner_has_nothing_left()


def test_a_friend_who_is_already_there_still_stops_the_delete_under_the_lock():
    owner = sign_up("owner-person", kind="owner")
    sign_up("friend-person")
    seed_owner(owner)
    before = library_counts()
    with pytest.raises(users.AuthError) as refused:
        delete(owner, "owner-person")
    assert refused.value.status == 409 and library_counts() == before
    assert CLIENTS["owner-person"].get("/api/meta").status_code == 200          # a refused delete does not sign the owner out


def test_a_failed_reset_leaves_the_claimed_account_with_all_its_data(monkeypatch):
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    before_tenant, before_library = tenant_counts(1), library_counts()

    def boom(conn):
        raise RuntimeError("probe: the reset failed")

    with monkeypatch.context() as m:             # undo ONLY this patch: a bare monkeypatch.undo() also drops fresh_db's DATABASE_URL
        m.setattr(db, "_migrate_categories", boom)
        with pytest.raises(RuntimeError):
            delete(owner, "owner-person")
    assert tenant_counts(1) == before_tenant and library_counts() == before_library
    with db.connect_system("test: still claimed") as conn:
        assert conn.execute("SELECT verifier IS NOT NULL AS v FROM users WHERE id = 1").fetchone()["v"] is True
        assert conn.execute("SELECT count(*) AS n FROM user_sessions WHERE user_id = 1 AND NOT pending").fetchone()["n"] >= 1




# ───────────── round 2: a late write is never inherited by the next owner ─────────────

def test_a_write_after_the_delete_is_wiped_when_the_next_owner_claims_row_1():
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    delete(owner, "owner-person")
    # a request that was still running (a Charts run, a slow Ask turn) writes AFTER delete_account returned
    late = make_thread()
    with as_user(1), db.connect() as conn:
        conn.execute("INSERT INTO conversations (thread_id, title) VALUES (%s, 'late-after-sweep')", (late,))
        conn.execute("INSERT INTO charts (title, prompt, code, kind, attempts) VALUES ('late-after-sweep', 'p', 'c', 'png', 1)")
        conn.execute("INSERT INTO trace_spans (run_id, root_run_id, name, run_type, started_at) "
                     "VALUES (gen_random_uuid(), gen_random_uuid(), 'late', 'chain', now())")
        conn.execute("INSERT INTO categories (name, kind) VALUES ('Late Category', 'expense')")
    mem.add_memory(get_memory().store, "owner", "fact", "late memory")
    assert library_counts()["checkpoints"] >= 1

    new_owner = sign_up("new-owner", kind="owner")                  # a new invite, sign-up and confirm
    client = CLIENTS["new-owner"]
    assert new_owner.uid == 1
    threads = client.get("/api/chat/threads")
    assert threads.status_code == 200 and threads.json() == {"threads": []}
    charts = client.get("/api/charts")
    assert charts.status_code == 200 and charts.json()["items"] == []
    assert_owner_has_nothing_left()
    with as_user(1), db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM categories WHERE name = 'Late Category'").fetchone()["n"] == 0
        assert {r["name"] for r in conn.execute("SELECT name FROM categories").fetchall()} == {n for n, _ in db.DEFAULT_CATEGORIES}
    assert mem.list_memories(get_memory().store, "owner") == []


def test_a_normal_laptop_data_claim_keeps_its_data():
    populate(1)                                                      # row 1 has data from the single-user days and was never deleted
    before = tenant_counts(1)
    assert before["transactions"] > 0
    with db.connect_system("test: no mark") as conn:
        assert conn.execute("SELECT sweep_state FROM users WHERE id = 1").fetchone()["sweep_state"] is None
    sign_up("first-owner", kind="owner")
    after = tenant_counts(1)
    assert after["transactions"] == before["transactions"] and after["charts"] == before["charts"]
    assert after["conversations"] == before["conversations"]


def test_an_unconfirmed_claim_that_is_rolled_back_keeps_the_deleted_mark():
    owner = sign_up("owner-person", kind="owner")
    delete(owner, "owner-person")
    code = users.create_invite("owner")["code"]
    users.signup(code, "never-confirmed", PASSWORD)                  # claims row 1 but nobody confirms
    with db.connect_system("test: expire it") as conn:
        conn.execute("UPDATE user_sessions SET expires_at = now() - interval '1 hour' WHERE user_id = 1 AND pending")
    users.purge_unconfirmed()
    with db.connect_system("test: look") as conn:
        assert conn.execute("SELECT sweep_state, verifier IS NULL AS u FROM users WHERE id = 1").fetchone() == {"sweep_state": "deleted", "u": True}


# ───────────── round 2: drains, the stop flag, the log line, and the real error ─────────────

def test_a_stop_that_arrives_while_a_turn_is_preparing_is_still_there_when_the_work_starts():
    import asyncio

    from rectifi.api import ask

    key = "22222222-2222-2222-2222-222222222222"
    seen, done = {}, threading.Event()

    def prepare():
        ask.turn_for(key).cancelled.set()              # a stop that arrives while this turn prepares (it holds the lock)
        return object()

    def work(graph, emit, turn):
        seen["cancelled"] = turn.cancelled.is_set()
        done.set()

    async def go():
        await ask.stream_turn(key, prepare, work)

    try:
        asyncio.run(go())
        assert done.wait(10)
        assert seen["cancelled"] is True               # the old code cleared the flag after prepare
    finally:
        ask._turns.pop(key, None)                                                     # noqa: SLF001






def test_tenant_scope_lets_the_real_error_through_instead_of_hiding_it_in_a_failed_transaction():
    import psycopg

    with db.connect_system("test: a failing block") as conn:
        with pytest.raises(psycopg.errors.DivisionByZero):                            # not InFailedSqlTransaction
            with db.tenant_scope(conn, userctx.UserContext(uid=1, strict=False)):
                conn.execute("SELECT 1/0")


# ───────────── fallback: the owner is deleted on the server only ─────────────

def test_the_owner_gets_403_on_the_delete_route_and_a_friend_can_still_delete_themselves():
    owner = sign_up("owner-person", kind="owner")
    friend = sign_up("friend-person")
    seed_owner(owner)
    populate(friend, "fr")
    owner_before, library_before = tenant_counts(1), library_counts()
    r = CLIENTS["owner-person"].request("DELETE", "/api/account", json={"password": PASSWORD, "confirm_username": "owner-person"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "owner_delete_on_server"
    assert "python scripts/dev.py reset-owner" in r.json()["error"]["message"]
    assert tenant_counts(1) == owner_before and library_counts() == library_before
    with pytest.raises(users.AuthError) as direct:                                     # the function itself says the same
        account.delete_account(1, "owner-person", PASSWORD, "owner-person")
    assert direct.value.status == 403

    r = CLIENTS["friend-person"].request("DELETE", "/api/account", json={"password": PASSWORD, "confirm_username": "friend-person"})
    assert r.status_code == 200
    assert all(n == 0 for n in tenant_counts(friend.uid).values())
    assert tenant_counts(1) == owner_before and library_counts() == library_before


def test_a_sweep_that_is_still_running_cannot_erase_the_deleted_mark_or_seal_for_a_gone_owner():
    from rectifi import seal_sweep

    owner = sign_up("owner-person", kind="owner")
    with db.connect_system("test: unseal") as conn:
        conn.execute("UPDATE users SET encrypted = false, sweep_state = 'pending' WHERE id = 1")
    populate(owner)
    seen = []

    def reset_in_the_middle(table, changed):
        if not seen:
            seen.append(table)
            account.reset_owner()                      # the owner is reset while the sweep is between two batches

    result = seal_sweep.run(owner, batch=1, on_batch=reset_in_the_middle)
    assert seen and result["finished"] is False
    with db.connect_system("test: the mark") as conn:
        row = conn.execute("SELECT sweep_state, encrypted FROM users WHERE id = 1").fetchone()
    assert dict(row) == {"sweep_state": "deleted", "encrypted": False}
    assert seal_sweep.should_run(1) is False
    assert account.set_sweep_state(1, "pending", owner.public_key) is False
    assert account.finish_sweep(1, owner.public_key) is False
    with db.connect_system("test: still deleted") as conn:
        assert conn.execute("SELECT sweep_state FROM users WHERE id = 1").fetchone()["sweep_state"] == "deleted"


# ───────────── dev.py reset-owner ─────────────

def load_devtool():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("rectifi_devtool_users_reset", root / "scripts" / "devtools" / "users.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Terminal:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def run_here(snippet: str, *argv: str) -> dict:
    """What the child process would print, but run in this process (against the test database)."""
    import contextlib
    import io
    import json
    import sys

    out = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["-c", *argv]
    try:
        with contextlib.redirect_stdout(out):
            try:
                exec(snippet, {"__name__": "__main__"})                                # noqa: S102
            except SystemExit:
                pass
    finally:
        sys.argv = old_argv
    data = json.loads(out.getvalue().strip().splitlines()[-1])
    if "error" in data:
        raise SystemExit(data["error"])
    return data


@pytest.fixture
def tool(monkeypatch):
    module = load_devtool()
    monkeypatch.setattr(module, "listening", lambda port: False)
    monkeypatch.setattr(module, "_child_json", run_here)
    monkeypatch.setattr("sys.stdin", _Terminal(True))
    return module


def answers(monkeypatch, *typed):
    queue = list(typed)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0))
    return queue


def test_reset_owner_refuses_without_a_terminal_and_changes_nothing(tool, monkeypatch):
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    before = tenant_counts(1)
    monkeypatch.setattr("sys.stdin", _Terminal(False))
    with pytest.raises(SystemExit) as stop:
        tool.reset_owner([])
    assert "needs a terminal" in str(stop.value)
    with pytest.raises(SystemExit):
        tool.reset_owner(["--yes"])
    assert tenant_counts(1) == before and library_counts()["store"] >= 1


@pytest.mark.parametrize("busy, label", [(8501, "web app"), (8765, "phone gateway")])
def test_reset_owner_refuses_while_the_app_or_the_gateway_is_listening(tool, monkeypatch, busy, label):
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    before = tenant_counts(1)
    monkeypatch.setattr(tool, "listening", lambda port: port == busy)
    monkeypatch.setattr(tool, "_child_json", lambda *a: pytest.fail("the database must not be touched while something is running"))
    with pytest.raises(SystemExit) as stop:
        tool.reset_owner([])
    assert label in str(stop.value) and str(busy) in str(stop.value) and "Nothing was changed" in str(stop.value)
    assert tenant_counts(1) == before


def test_reset_owner_refuses_while_another_database_connection_is_open(tool, monkeypatch):
    import psycopg

    from rectifi import config

    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    before, library = tenant_counts(1), library_counts()
    answers(monkeypatch, "owner-person", "delete")
    with psycopg.connect(config.database_url()):                  # the app, psql or a backup: anything else on this database
        with pytest.raises(SystemExit) as stop:
            tool.reset_owner([])
    assert "other connection" in str(stop.value) and "Nothing was changed" in str(stop.value)
    assert tenant_counts(1) == before and library_counts() == library


def test_other_connections_counts_a_second_session_and_not_its_own():
    import psycopg
    from psycopg.rows import dict_row

    from rectifi import config

    with psycopg.connect(config.database_url(), autocommit=True, row_factory=dict_row) as one:
        base = account.other_connections(one)
        with psycopg.connect(config.database_url(), autocommit=True):
            assert account.other_connections(one) == base + 1


def test_reset_owner_with_everything_stopped_leaves_no_owner_row_and_the_next_owner_sees_nothing(tool, monkeypatch, capsys):
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    make_thread()                                                 # one more unlisted thread, as in the review
    monkeypatch.setattr(account, "other_connections", lambda conn: 0)           # this test process has its own pools open
    answers(monkeypatch, "OWNER-person", "delete")
    tool.reset_owner([])
    printed = capsys.readouterr().out
    assert "The owner account was deleted" in printed and "transactions" in printed
    assert "late" not in printed and "student" not in printed                                      # counts only, never a value
    assert_owner_has_nothing_left()
    with db.connect_system("test: unclaimed") as conn:
        row = conn.execute("SELECT username, verifier IS NULL AS u, sweep_state FROM users WHERE id = 1").fetchone()
    assert dict(row) == {"username": "owner", "u": True, "sweep_state": "deleted"}
    # the next owner signs up and finds nothing
    sign_up("new-owner", kind="owner")
    client = CLIENTS["new-owner"]
    assert client.get("/api/chat/threads").json() == {"threads": []}
    assert client.get("/api/charts").json()["items"] == []
    assert_owner_has_nothing_left()
    assert mem.list_memories(get_memory().store, "owner") == []


@pytest.mark.parametrize("typed", [("someone-else", "delete"), ("owner-person", "yes"), ("owner-person", "")])
def test_reset_owner_deletes_nothing_unless_the_username_and_the_word_delete_are_typed(tool, monkeypatch, typed):
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    before, library = tenant_counts(1), library_counts()
    monkeypatch.setattr(account, "other_connections", lambda conn: 0)
    answers(monkeypatch, *typed)
    tool.reset_owner([])
    assert tenant_counts(1) == before and library_counts() == library


def test_reset_owner_refuses_while_a_friend_exists(tool, monkeypatch):
    owner = sign_up("owner-person", kind="owner")
    sign_up("friend-person")
    seed_owner(owner)
    before = tenant_counts(1)
    monkeypatch.setattr(account, "other_connections", lambda conn: 0)
    with pytest.raises(SystemExit) as stop:
        tool.reset_owner([])
    assert "Other people still use" in str(stop.value)
    assert tenant_counts(1) == before


def test_the_deleted_mark_is_set_by_reset_and_cleared_by_the_next_claim():
    owner = sign_up("owner-person", kind="owner")
    account.reset_owner()
    with db.connect_system("test: mark") as conn:
        assert conn.execute("SELECT sweep_state FROM users WHERE id = 1").fetchone()["sweep_state"] == "deleted"
    sign_up("new-owner", kind="owner")
    with db.connect_system("test: claimed") as conn:
        assert conn.execute("SELECT sweep_state FROM users WHERE id = 1").fetchone()["sweep_state"] == "pending"
    assert owner.uid == 1
