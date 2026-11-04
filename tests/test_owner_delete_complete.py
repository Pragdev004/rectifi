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


def test_a_turn_that_writes_while_it_is_being_stopped_leaves_nothing_behind():
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)
    calls = []

    def drain():                                       # what a running Ask turn does: finish its step and write one more thread
        calls.append(1)
        late = make_thread()
        with as_user(1), db.connect() as conn:
            conn.execute("INSERT INTO conversations (thread_id, title) VALUES (%s, 'late')", (late,))
            conn.execute("INSERT INTO trace_spans (run_id, root_run_id, name, run_type, started_at) "
                         "VALUES (gen_random_uuid(), gen_random_uuid(), 'late', 'chain', now())")
        mem.add_memory(get_memory().store, "owner", "fact", "late memory")

    assert account.delete_account(1, "owner-person", PASSWORD, "owner-person", drain=drain) == {"ok": True}
    assert calls == [1]
    assert_owner_has_nothing_left()
    with db.connect_system("test: still unclaimed") as conn:
        assert conn.execute("SELECT verifier IS NULL AS u FROM users WHERE id = 1").fetchone()["u"] is True


def test_the_straggler_sweep_never_touches_a_new_owners_data():
    owner = sign_up("owner-person", kind="owner")
    seed_owner(owner)

    def drain():                                       # someone claims row 1 again before the sweep runs
        with db.connect_system("test: claim") as conn:
            conn.execute("UPDATE users SET verifier = '\\x01'::bytea WHERE id = 1")
        with as_user(1), db.connect() as conn:
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('new_owner', 'true'::jsonb)")

    account.delete_account(1, "owner-person", PASSWORD, "owner-person", drain=drain)
    with as_user(1), db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app_settings WHERE key = 'new_owner'").fetchone()["n"] == 1


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

    monkeypatch.setattr(db, "_migrate_categories", boom)
    with pytest.raises(RuntimeError):
        delete(owner, "owner-person")
    monkeypatch.undo()
    assert tenant_counts(1) == before_tenant and library_counts() == before_library
    with db.connect_system("test: still claimed") as conn:
        assert conn.execute("SELECT verifier IS NOT NULL AS v FROM users WHERE id = 1").fetchone()["v"] is True
        assert conn.execute("SELECT count(*) AS n FROM user_sessions WHERE user_id = 1 AND NOT pending").fetchone()["n"] >= 1


def test_drain_turns_cancels_a_running_turn_and_waits_for_it():
    from rectifi.api import ask

    key = "11111111-1111-1111-1111-111111111111"
    turn = ask.turn_for(key)
    assert turn.lock.acquire(blocking=False)
    finished = []

    def worker():
        turn.cancelled.wait(5)
        time.sleep(0.2)
        finished.append(True)
        turn.lock.release()

    threading.Thread(target=worker).start()
    ask.drain_turns(timeout=10)
    assert finished == [True] and turn.cancelled.is_set() and not turn.lock.locked()
    ask._turns.pop(key, None)                                                     # noqa: SLF001
    ask.drain_turns(timeout=0.1)                                                  # nothing running: returns at once


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
