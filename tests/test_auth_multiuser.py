"""Multi-user sign-up, sign-in, sessions, recovery and the friend gate, through the real API (RECTIFI_MULTIUSER=1).

Acceptance (plan section 6, AUTH): no session before confirm; an unconfirmed user cannot sign in and is cleared after
30 minutes; a code works once and expires; a wrong or reused code gets one message; cookie tamper, wrong epoch, deleted
session and another person's uid give 401; one account's lockout leaves others alone; recovery restores decryption and the
old code stops; a password change keeps data readable; a friend gets 403 on every owner-only route; nothing secret in caplog.
"""

import logging
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from rectifi import db, hooks, userctx, users, vault
from rectifi.api import auth
from rectifi.api.app import api_routes, create_app
from rectifi.api import security

SECRET = "s" * 48
HEADERS = {"X-Rectifi-Request": "1"}
PASSWORD = "correct horse battery"
NEW_PASSWORD = "another long passphrase"


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", SECRET)
    users.reset_limiters()
    pauses = []

    async def fake_pause(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(auth, "pause", fake_pause)
    calls = []
    monkeypatch.setattr(hooks, "after_sign_in", lambda ctx: calls.append(ctx.uid))
    yield SimpleNamespace(pauses=pauses, hook_calls=calls)
    users.reset_limiters()


def new_client() -> TestClient:
    return TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)


def invite(kind="friend", days=7) -> str:
    return users.create_invite(kind, days)["code"]


def sign_up(client, username="friend", password=PASSWORD, code=None):
    return client.post("/api/auth/signup", json={"invite": code or invite(), "username": username, "password": password})


def confirmed_client(username="friend", password=PASSWORD):
    client = new_client()
    body = sign_up(client, username, password).json()
    assert client.post("/api/auth/signup/confirm", json={"signup_token": body["signup_token"]}).status_code == 200
    return client, body["recovery_code"]


def row_of(username):
    with db.connect_system("test: read a user") as conn:
        return conn.execute("SELECT * FROM users WHERE lower(username) = lower(%s)", (username,)).fetchone()


def invite_rows():
    with db.connect_system("test: read invites") as conn:
        return conn.execute("SELECT * FROM invites ORDER BY id").fetchall()


def session_rows(uid=None):
    with db.connect_system("test: read sessions") as conn:
        if uid is None:
            return conn.execute("SELECT * FROM user_sessions").fetchall()
        return conn.execute("SELECT * FROM user_sessions WHERE user_id = %s", (uid,)).fetchall()


def cookie_of(client) -> str:
    return client.cookies[auth.COOKIE_NAME]


# ───────────────────────── sign-up: two steps ─────────────────────────

def test_signup_gives_a_token_and_a_code_but_no_session_and_no_access(multiuser):
    client = new_client()
    r = sign_up(client)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"signup_token", "recovery_code"} and body["recovery_code"].count("-") == 4
    assert "set-cookie" not in r.headers
    assert client.get("/api/meta").status_code == 401
    # the person exists but is unconfirmed: they cannot sign in, and the failure looks like any other wrong password
    wrong = client.post("/api/auth/login", json={"username": "friend", "password": "not the password"})
    early = client.post("/api/auth/login", json={"username": "friend", "password": PASSWORD})
    assert early.status_code == 401 and early.json() == wrong.json()
    assert row_of("friend")["confirmed_at"] is None
    reserved = [i for i in invite_rows() if i["reserved_by"] is not None]
    assert len(reserved) == 1 and reserved[0]["used_at"] is None
    assert multiuser.hook_calls == []


def test_confirm_sets_the_cookie_uses_the_invite_and_seeds_the_person(multiuser):
    client = new_client()
    body = sign_up(client).json()
    r = client.post("/api/auth/signup/confirm", json={"signup_token": body["signup_token"]})
    assert r.status_code == 200 and r.json() == {"ok": True, "username": "friend", "is_owner": False}
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/meta").status_code == 200
    me = row_of("friend")
    assert me["confirmed_at"] is not None and me["encrypted"] is True
    assert all(i["used_at"] is not None for i in invite_rows() if i["reserved_by"] == me["id"])
    with userctx.bound(userctx.UserContext(uid=me["id"], strict=False)), db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM categories").fetchone()["n"] > 5      # their own copy of the defaults
    assert multiuser.hook_calls == [me["id"]]
    assert client.get("/api/auth/status").json() == {"required": True, "signed_in": True, "multiuser": True, "username": "friend", "is_owner": False}


def test_the_signup_token_is_spent_after_confirming():
    client = new_client()
    body = sign_up(client).json()
    assert client.post("/api/auth/signup/confirm", json={"signup_token": body["signup_token"]}).status_code == 200
    again = client.post("/api/auth/signup/confirm", json={"signup_token": body["signup_token"]})
    assert again.status_code == 400 and again.json()["error"]["code"] == "signup_expired"


def test_a_wrong_signup_token_does_not_confirm():
    client = new_client()
    body = sign_up(client).json()
    sid = body["signup_token"].split(".")[0]
    for bad in (sid + "." + "A" * 43, "nonsense", "", sid, body["signup_token"][:-3] + "AAA"):
        r = client.post("/api/auth/signup/confirm", json={"signup_token": bad or "x"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "signup_expired"
    assert row_of("friend")["confirmed_at"] is None


def test_the_database_alone_cannot_confirm_or_open_a_session():
    client = new_client()
    body = sign_up(client).json()
    k = body["signup_token"].split(".")[1]
    rows = session_rows(row_of("friend")["id"])
    assert len(rows) == 1 and rows[0]["pending"]
    assert k.encode() not in bytes(rows[0]["dek_wrapped"]) and vault_free_of(rows[0]["dek_wrapped"], k)


def vault_free_of(blob, k_text) -> bool:
    import base64
    raw = base64.urlsafe_b64decode(k_text + "=" * (-len(k_text) % 4))
    return raw not in bytes(blob)


def test_an_unconfirmed_signup_is_cleared_after_30_minutes_and_frees_the_username_and_the_invite(monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr(users, "_now", lambda: clock[0])
    client = new_client()
    code = invite()
    token = sign_up(client, "ghost", code=code).json()["signup_token"]
    assert row_of("ghost") is not None
    clock[0] += users.PENDING_SECONDS - 5
    assert row_of("ghost") is not None
    clock[0] += 10                                                # now past 30 minutes
    late = client.post("/api/auth/signup/confirm", json={"signup_token": token})
    assert late.status_code == 400 and late.json()["error"]["code"] == "signup_expired"
    assert row_of("ghost") is None                                # the row is gone
    assert all(i["reserved_by"] is None for i in invite_rows())   # the invite is free again
    again = sign_up(client, "ghost", code=code)                   # same name, same invite
    assert again.status_code == 201
    assert client.post("/api/auth/signup/confirm", json={"signup_token": again.json()["signup_token"]}).status_code == 200


def test_an_unconfirmed_person_who_never_comes_back_is_cleared_on_the_next_sign_in_attempt(monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr(users, "_now", lambda: clock[0])
    client = new_client()
    sign_up(client, "ghost")
    clock[0] += users.PENDING_SECONDS + 1
    client.post("/api/auth/login", json={"username": "somebody", "password": "whatever1234"})
    assert row_of("ghost") is None and session_rows() == []


# ───────────────────────── invites ─────────────────────────

def test_an_invite_works_once_and_a_wrong_expired_or_reused_code_gets_one_message(monkeypatch):
    client = new_client()
    good = invite()
    assert sign_up(client, "first", code=good).status_code == 201
    messages = set()
    for code in (good, "ABCD-EFGH-JKLM-NPQR-STUV", "x"):                         # reused (reserved), unknown, malformed
        r = sign_up(client, "second", code=code)
        assert r.status_code == 400
        messages.add((r.json()["error"]["code"], r.json()["error"]["message"]))
    expired = users.create_invite("friend", 1)["code"]
    monkeypatch.setattr(users, "_now", lambda: __import__("time").time() + 2 * 86400)
    r = sign_up(client, "third", code=expired)
    messages.add((r.json()["error"]["code"], r.json()["error"]["message"]))
    assert messages == {("invalid_invite", users.MESSAGES["invalid_invite"])}


def test_a_confirmed_invite_cannot_be_used_again():
    code = invite()
    client = new_client()
    token = sign_up(client, "first", code=code).json()["signup_token"]
    client.post("/api/auth/signup/confirm", json={"signup_token": token})
    r = sign_up(new_client(), "second", code=code)
    assert r.status_code == 400 and r.json()["error"]["message"] == users.MESSAGES["invalid_invite"]


def test_a_revoked_invite_cannot_be_confirmed():
    code_info = users.create_invite("friend", 7)
    client = new_client()
    token = sign_up(client, "revoked", code=code_info["code"]).json()["signup_token"]
    assert users.revoke_invite(code_info["id"]) is True
    r = client.post("/api/auth/signup/confirm", json={"signup_token": token})
    assert r.status_code == 400 and client.get("/api/meta").status_code == 401


def test_a_username_that_is_taken_is_refused_and_the_invite_stays_free():
    confirmed_client("taken")
    code = invite()
    r = sign_up(new_client(), "TAKEN", code=code)                                  # case-insensitive
    assert r.status_code == 409 and r.json()["error"]["code"] == "username_taken"
    assert all(i["reserved_by"] is None for i in invite_rows() if i["used_at"] is None)
    assert sign_up(new_client(), "other-name", code=code).status_code == 201


@pytest.mark.parametrize("username, password", [("ab", PASSWORD), ("bad name!", PASSWORD), ("fine-name", "short"), ("", PASSWORD)])
def test_signup_checks_the_username_and_password(username, password):
    r = sign_up(new_client(), username, password)
    assert r.status_code == 422


def test_there_is_a_global_signup_limit(monkeypatch):
    monkeypatch.setattr(users, "SIGNUP_LIMIT", 3)
    client = new_client()
    codes = [sign_up(client, f"person{i}", code="ABCD-EFGH-JKLM-NPQR-STUV").status_code for i in range(3)]
    assert codes == [400, 400, 400]
    r = sign_up(client, "person9")
    assert r.status_code == 429 and r.json()["error"]["code"] == "signup_limited"


def test_signup_is_off_without_multiuser(monkeypatch):
    monkeypatch.delenv("RECTIFI_MULTIUSER")
    monkeypatch.setenv("RECTIFI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    client = TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    r = client.post("/api/auth/signup", json={"invite": "x", "username": "abc", "password": PASSWORD})
    assert r.status_code == 400 and r.json()["error"]["code"] == "signup_off"


# ───────────────────────── sign-in, sessions ─────────────────────────

def test_login_logout_and_the_session_row():
    client, _ = confirmed_client()
    uid = row_of("friend")["id"]
    client.post("/api/auth/logout", json={})
    assert client.get("/api/meta").status_code == 401
    assert session_rows(uid) == []                                    # logout deleted the row
    r = client.post("/api/auth/login", json={"username": "FRIEND", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["username"] == "friend"
    assert client.get("/api/meta").status_code == 200 and len(session_rows(uid)) == 1


def test_logging_out_removes_the_row_so_the_old_cookie_is_dead():
    client, _ = confirmed_client()
    stolen = cookie_of(client)
    client.post("/api/auth/logout", json={})
    thief = new_client()
    thief.cookies.set(auth.COOKIE_NAME, stolen)
    assert thief.get("/api/meta").status_code == 401


def test_logout_everywhere_signs_out_every_device():
    a, _ = confirmed_client()
    b = new_client()
    assert b.post("/api/auth/login", json={"username": "friend", "password": PASSWORD}).status_code == 200
    assert a.post("/api/auth/logout-all", json={}).status_code == 200
    assert a.get("/api/meta").status_code == 401 and b.get("/api/meta").status_code == 401
    assert b.post("/api/auth/login", json={"username": "friend", "password": PASSWORD}).status_code == 200


def test_cookie_tamper_wrong_epoch_deleted_session_and_another_persons_uid_all_give_401():
    a, _ = confirmed_client("alice")
    b, _ = confirmed_client("bobby")
    good = auth.read_user_session(cookie_of(a), SECRET)
    b_uid = row_of("bobby")["id"]

    def forged(**changes):
        data = {"uid": good["uid"], "sid": good["sid"], "k": good["k"], "epoch": good["epoch"], **changes}
        who = SimpleNamespace(uid=data["uid"], sid=data["sid"], epoch=data["epoch"], k=auth._b64d(data["k"]))
        return auth.make_user_session(SECRET, who)

    probe = new_client()

    def status_with(value):
        probe.cookies.clear()
        probe.cookies.set(auth.COOKIE_NAME, value)
        return probe.get("/api/meta").status_code

    assert status_with(forged()) == 200                                   # the forging helper itself is right
    value = cookie_of(a)
    assert status_with(value[:-3] + "AAA") == 401                         # tampered signature
    assert status_with(forged(epoch=good["epoch"] + 1)) == 401           # wrong epoch
    assert status_with(forged(uid=b_uid)) == 401                         # signed correctly, but alice's session under bobby's id
    assert status_with(forged(k=auth._b64e(b"\x01" * 32))) == 401        # a secret that does not open the wrapped key
    assert status_with(forged(sid="00000000-0000-4000-8000-000000000000")) == 401
    assert status_with(forged(sid="not-a-uuid")) == 401
    assert status_with(auth.make_session(SECRET, good["epoch"])) == 401  # a single-password cookie has no uid
    with db.connect_system("test: delete the session") as conn:
        conn.execute("DELETE FROM user_sessions WHERE id = %s", (good["sid"],))
    assert status_with(value) == 401                                      # deleted session
    assert b.get("/api/meta").status_code == 200                          # bobby is untouched


def test_an_expired_session_row_is_401():
    client, _ = confirmed_client()
    with db.connect_system("test: expire the session") as conn:
        conn.execute("UPDATE user_sessions SET expires_at = now() - interval '1 minute'")
    assert client.get("/api/meta").status_code == 401


def test_a_pending_session_row_is_not_a_session():
    """The sign-up token's session row must never work as a cookie."""
    client = new_client()
    body = sign_up(client).json()
    sid, k = body["signup_token"].split(".")
    uid = row_of("friend")["id"]
    who = SimpleNamespace(uid=uid, sid=sid, epoch=0, k=auth._b64d(k))
    client.cookies.set(auth.COOKIE_NAME, auth.make_user_session(SECRET, who))
    assert client.get("/api/meta").status_code == 401


def test_status_when_signed_out_in_multiuser_mode():
    assert new_client().get("/api/auth/status").json() == {"required": True, "signed_in": False, "multiuser": True, "username": None, "is_owner": False}


def test_multiuser_without_a_session_secret_refuses_to_start(monkeypatch):
    monkeypatch.delenv("RECTIFI_SESSION_SECRET")
    with pytest.raises(auth.StartupRefused, match="RECTIFI_SESSION_SECRET"):
        create_app()


def test_the_single_password_hash_is_not_needed_and_not_used_in_multiuser_mode(monkeypatch):
    monkeypatch.setenv("RECTIFI_PASSWORD_HASH", auth.hash_password("some other password"))
    client, _ = confirmed_client()
    assert client.get("/api/meta").status_code == 200
    r = new_client().post("/api/auth/login", json={"username": "friend", "password": "some other password"})
    assert r.status_code == 401


# ───────────────────────── the limiter, same message ─────────────────────────

def test_one_accounts_lockout_leaves_others_alone(multiuser):
    a, _ = confirmed_client("alice")
    confirmed_client("bobby")
    probe = new_client()
    for _ in range(users.LOCK_AFTER - 1):
        assert probe.post("/api/auth/login", json={"username": "alice", "password": "wrong password!"}).status_code == 401
    r = probe.post("/api/auth/login", json={"username": "alice", "password": "wrong password!"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "login_locked" and int(r.headers["retry-after"]) > 0
    assert probe.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 429     # even the right one waits
    assert probe.post("/api/auth/login", json={"username": "bobby", "password": PASSWORD}).status_code == 200      # bobby is fine
    assert a.get("/api/meta").status_code == 200                                                                  # alice's open session too


def test_the_lock_ends_and_a_pause_starts_after_five_misses(monkeypatch, multiuser):
    confirmed_client("alice")
    clock = [time_now()]
    monkeypatch.setattr(users, "_now", lambda: clock[0])
    probe = new_client()
    for _ in range(users.DELAY_AFTER):
        probe.post("/api/auth/login", json={"username": "alice", "password": "wrong password!"})
        clock[0] += 1
    assert multiuser.pauses == []
    probe.post("/api/auth/login", json={"username": "alice", "password": "wrong password!"})
    assert multiuser.pauses == [users.DELAY_SECONDS]
    for _ in range(users.LOCK_AFTER):
        probe.post("/api/auth/login", json={"username": "alice", "password": "wrong password!"})
    assert probe.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 429
    clock[0] += users.LOCK_SECONDS + 5
    assert probe.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200


def time_now() -> float:
    import time
    return time.time()


def test_an_unknown_username_gets_the_same_answer_and_the_same_work(monkeypatch):
    confirmed_client("alice")
    work = []
    real = vault.derive_password_keys
    monkeypatch.setattr(vault, "derive_password_keys", lambda pw, salt: work.append(1) or real(pw, salt))
    probe = new_client()
    known = probe.post("/api/auth/login", json={"username": "alice", "password": "wrong password!"})
    unknown = probe.post("/api/auth/login", json={"username": "nobody-here", "password": "wrong password!"})
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json()) == (401, {"error": {
        "code": "invalid_login", "message": users.MESSAGES["invalid_login"], "field": None}})
    assert work == [1, 1]                                                   # one scrypt each


def test_an_unknown_username_locks_like_a_real_one():
    probe = new_client()
    for _ in range(users.LOCK_AFTER - 1):
        probe.post("/api/auth/login", json={"username": "nobody-here", "password": "wrong password!"})
    r = probe.post("/api/auth/login", json={"username": "nobody-here", "password": "wrong password!"})
    assert r.status_code == 429


# ───────────────────────── password change, recovery ─────────────────────────

def seal_for(client, text="my secret payee"):
    """Seal a value with this person's data key (as a session would) and return (envelope, a function that reopens it after sign-in)."""
    who = auth.resolve_user(f"{auth.COOKIE_NAME}={cookie_of(client)}")
    return vault.seal(text, who.ctx), who.ctx.key


def test_a_password_change_keeps_the_data_readable_and_signs_other_devices_out():
    a, _ = confirmed_client()
    b = new_client()
    b.post("/api/auth/login", json={"username": "friend", "password": PASSWORD})
    envelope, key_before = seal_for(a)
    r = a.post("/api/auth/change-password", json={"current": PASSWORD, "new": NEW_PASSWORD})
    assert r.status_code == 200
    assert a.get("/api/meta").status_code == 200                               # this device stays signed in
    assert b.get("/api/meta").status_code == 401                               # the other one does not
    assert new_client().post("/api/auth/login", json={"username": "friend", "password": PASSWORD}).status_code == 401
    fresh = new_client()
    assert fresh.post("/api/auth/login", json={"username": "friend", "password": NEW_PASSWORD}).status_code == 200
    who = auth.resolve_user(f"{auth.COOKIE_NAME}={cookie_of(fresh)}")
    assert who.ctx.key == key_before and vault.open_value(envelope, who.ctx) == "my secret payee"


def test_a_password_change_needs_the_current_password():
    a, _ = confirmed_client()
    r = a.post("/api/auth/change-password", json={"current": "not mine at all", "new": NEW_PASSWORD})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_password"
    assert a.post("/api/auth/change-password", json={"current": PASSWORD, "new": "short"}).status_code == 422
    assert new_client().post("/api/auth/login", json={"username": "friend", "password": PASSWORD}).status_code == 200


def test_recovery_restores_decryption_and_the_old_code_stops_working():
    a, code = confirmed_client()
    envelope, key_before = seal_for(a)
    probe = new_client()
    r = probe.post("/api/auth/recover", json={"username": "friend", "recovery_code": code, "new_password": NEW_PASSWORD})
    assert r.status_code == 200
    new_code = r.json()["recovery_code"]
    assert new_code != code and new_code.count("-") == 4
    assert a.get("/api/meta").status_code == 401                                # signed out everywhere
    assert probe.post("/api/auth/login", json={"username": "friend", "password": PASSWORD}).status_code == 401
    assert probe.post("/api/auth/login", json={"username": "friend", "password": NEW_PASSWORD}).status_code == 200
    who = auth.resolve_user(f"{auth.COOKIE_NAME}={cookie_of(probe)}")
    assert who.ctx.key == key_before and vault.open_value(envelope, who.ctx) == "my secret payee"
    # the old code is dead; the answer is the same as for an unknown person
    stale = probe.post("/api/auth/recover", json={"username": "friend", "recovery_code": code, "new_password": "yet another passphrase"})
    unknown = probe.post("/api/auth/recover", json={"username": "nobody-here", "recovery_code": code, "new_password": "yet another passphrase"})
    assert stale.status_code == unknown.status_code == 401 and stale.json() == unknown.json()
    # the new one works
    assert probe.post("/api/auth/recover", json={"username": "friend", "recovery_code": new_code, "new_password": "yet another passphrase"}).status_code == 200


def test_recovery_code_is_accepted_however_it_is_typed():
    _, code = confirmed_client()
    r = new_client().post("/api/auth/recover", json={"username": "FRIEND", "recovery_code": code.lower().replace("-", " "), "new_password": NEW_PASSWORD})
    assert r.status_code == 200


def test_a_wrong_recovery_code_counts_against_that_account_only():
    confirmed_client("alice")
    confirmed_client("bobby")
    probe = new_client()
    for _ in range(users.LOCK_AFTER):
        probe.post("/api/auth/recover", json={"username": "alice", "recovery_code": "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "new_password": NEW_PASSWORD})
    assert probe.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 429
    assert probe.post("/api/auth/login", json={"username": "bobby", "password": PASSWORD}).status_code == 200


def test_a_new_recovery_code_replaces_the_old_one_and_needs_the_password():
    a, code = confirmed_client()
    assert a.post("/api/auth/recovery-code", json={"password": "wrong password!"}).status_code == 401
    r = a.post("/api/auth/recovery-code", json={"password": PASSWORD})
    assert r.status_code == 200
    new_code = r.json()["recovery_code"]
    probe = new_client()
    assert probe.post("/api/auth/recover", json={"username": "friend", "recovery_code": code, "new_password": NEW_PASSWORD}).status_code == 401
    assert probe.post("/api/auth/recover", json={"username": "friend", "recovery_code": new_code, "new_password": NEW_PASSWORD}).status_code == 200


def test_password_routes_are_closed_without_a_session():
    client = new_client()
    for path, body in (("/api/auth/change-password", {"current": PASSWORD, "new": NEW_PASSWORD}), ("/api/auth/recovery-code", {"password": PASSWORD}),
                       ("/api/auth/logout-all", {})):
        assert client.post(path, json=body).status_code == 401, path


# ───────────────────────── owner claim (D9) ─────────────────────────

def test_an_owner_invite_sets_the_password_on_user_one_and_keeps_the_existing_data():
    with userctx.bound(userctx.UserContext(uid=1, strict=False)), db.connect() as conn:
        conn.execute("INSERT INTO accounts (name, last4) VALUES ('Old HDFC', '4321')")
    client = new_client()
    code = invite("owner")
    token = sign_up(client, "prag", code=code).json()["signup_token"]
    assert client.get("/api/meta").status_code == 401
    assert client.post("/api/auth/login", json={"username": "prag", "password": PASSWORD}).status_code == 401      # not until confirmed
    r = client.post("/api/auth/signup/confirm", json={"signup_token": token})
    assert r.status_code == 200 and r.json() == {"ok": True, "username": "prag", "is_owner": True}
    me = row_of("prag")
    assert me["id"] == 1 and me["encrypted"] is False and me["sweep_state"] == "pending"       # the sweep is WS-ACCOUNT's
    assert client.get("/api/auth/status").json()["is_owner"] is True
    with userctx.bound(userctx.UserContext(uid=1, strict=False)), db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM accounts WHERE name = 'Old HDFC'").fetchone()["n"] == 1
    again = new_client()
    assert again.post("/api/auth/login", json={"username": "prag", "password": PASSWORD}).status_code == 200
    with pytest.raises(users.AuthError):
        users.create_invite("owner")                                                           # already claimed


def test_an_abandoned_owner_claim_is_rolled_back_and_the_invite_freed(monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr(users, "_now", lambda: clock[0])
    client = new_client()
    code = invite("owner")
    sign_up(client, "prag", code=code)
    assert row_of("prag")["verifier"] is not None
    clock[0] += users.PENDING_SECONDS + 1
    users.purge_unconfirmed()
    me = row_of("owner")
    assert me["id"] == 1 and me["verifier"] is None and me["dek_by_password"] is None and me["confirmed_at"] is None
    assert all(i["reserved_by"] is None for i in invite_rows())
    assert sign_up(client, "prag2", code=code).status_code == 201


def test_a_friend_invite_cannot_claim_the_owner_and_an_owner_invite_cannot_make_a_friend():
    # the kind decides the path; the friend path never writes user 1
    confirmed_client("friend")
    assert row_of("owner")["verifier"] is None and row_of("friend")["id"] != 1


# ───────────────────────── the friend gate (D7) ─────────────────────────

OWNER_ONLY_PREFIXES = ("/api/ask", "/api/chat", "/api/charts", "/api/knowledge", "/api/memory", "/api/traces", "/api/models", "/api/classify",
                       "/api/insights/explain")


def concrete(path: str) -> str:
    import re
    return re.sub(r"\{[^}]+\}", "1", "/api" + path)


def every_route():
    out = []
    for route in api_routes():
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            if concrete(route.path) not in security.SESSION_EXEMPT:             # open to everyone, signed in or not
                out.append((method, concrete(route.path)))
    return out


def is_owner_only(path: str) -> bool:
    return path.startswith(OWNER_ONLY_PREFIXES) or path.endswith("/explain")


def test_the_friend_list_matches_what_the_routes_really_are():
    """Every real route is either owner-only (AI) or on the friend list. A new route that is neither shows up here."""
    wrong = [(m, p) for m, p in every_route() if security.friend_allowed(m, p) == is_owner_only(p)]
    assert wrong == []
    assert len(every_route()) > 60


def test_a_friend_gets_403_on_every_owner_only_route_and_the_owner_does_not():
    friend, _ = confirmed_client("friend")
    owner_info = users.create_invite("owner")
    owner = new_client()
    token = sign_up(owner, "prag", code=owner_info["code"]).json()["signup_token"]
    owner.post("/api/auth/signup/confirm", json={"signup_token": token})
    blocked = [(m, p) for m, p in every_route() if is_owner_only(p)]
    assert len(blocked) > 25
    for method, path in blocked:
        r = friend.request(method, path, json={} if method != "GET" else None)
        assert r.status_code == 403, (method, path, r.status_code)
        assert r.json()["error"]["code"] == "forbidden"
    for method, path in (("GET", "/api/ask/status"), ("GET", "/api/models/status"), ("GET", "/api/memory")):
        assert owner.request(method, path).status_code != 403, path
    for method, path in (("GET", "/api/meta"), ("GET", "/api/overview"), ("GET", "/api/alerts"), ("GET", "/api/accounts")):
        assert friend.request(method, path).status_code == 200, path


def test_a_route_added_at_run_time_is_closed_to_friends_and_open_to_the_owner():
    async def new_thing(request: Request):
        return JSONResponse({"uid": request.state.uid})

    app = create_app()
    mount = next(r for r in app.routes if isinstance(r, Mount) and r.path == "/api")
    mount.routes.append(Route("/brand-new-thing", new_thing, methods=["GET", "POST"]))
    owner_info = users.create_invite("owner")
    owner = TestClient(app, base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)
    token = sign_up(owner, "prag", code=owner_info["code"]).json()["signup_token"]
    owner.post("/api/auth/signup/confirm", json={"signup_token": token})
    friend = TestClient(app, base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)
    token = sign_up(friend, "friend").json()["signup_token"]
    friend.post("/api/auth/signup/confirm", json={"signup_token": token})
    assert friend.get("/api/brand-new-thing").status_code == 403 and friend.post("/api/brand-new-thing", json={}).status_code == 403
    assert owner.get("/api/brand-new-thing").json() == {"uid": 1}                     # the owner's real integer id
    assert TestClient(app, base_url="http://127.0.0.1:8501", headers=HEADERS).get("/api/brand-new-thing").status_code == 401


def test_the_request_is_bound_to_the_real_person_and_sees_only_their_rows():
    seen = {}

    async def whoami(request: Request):
        ctx = userctx.current()
        seen["bound"] = (ctx.uid, ctx.key is not None, request.state.uid)
        return JSONResponse({"ok": True})

    app = create_app()
    next(r for r in app.routes if isinstance(r, Mount) and r.path == "/api").routes.append(Route("/owner-whoami", whoami, methods=["GET"]))
    client = TestClient(app, base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)
    code = invite("owner")
    client.post("/api/auth/signup/confirm", json={"signup_token": sign_up(client, "prag", code=code).json()["signup_token"]})
    assert client.get("/api/owner-whoami").status_code == 200
    assert seen["bound"] == (1, True, 1)
    with pytest.raises(userctx.NoUser):
        userctx.current()                                                             # nothing stays bound after the request


def test_the_ledger_a_friend_reads_is_only_theirs():
    a, _ = confirmed_client("alice")
    b, _ = confirmed_client("bobby")
    made = a.post("/api/accounts", json={"name": "Alice only", "last4": "1111"})
    assert made.status_code in (200, 201), made.text
    assert "Alice only" in a.get("/api/accounts").text
    assert "Alice only" not in b.get("/api/accounts").text


# ───────────────────────── start-up ─────────────────────────

def test_multiuser_refuses_to_start_on_a_superuser_database_role(monkeypatch):
    monkeypatch.setattr(db, "role_problems", lambda conn=None: ["The database login 'x' is a superuser, so row level security is skipped."])
    with pytest.raises(db.UnsafeDatabaseRole):
        create_app()
    monkeypatch.delenv("RECTIFI_MULTIUSER")
    create_app()                                                                      # laptop mode only warns (doctor shows it)


# ───────────────────────── nothing secret in the logs ─────────────────────────

def test_nothing_secret_reaches_the_logs(caplog):
    caplog.set_level(logging.DEBUG)
    secrets_seen = []
    code = invite()
    client = new_client()
    body = sign_up(client, "friend", code=code).json()
    token, recovery = body["signup_token"], body["recovery_code"]
    client.post("/api/auth/signup/confirm", json={"signup_token": token})
    cookie = cookie_of(client)
    client.post("/api/auth/change-password", json={"current": PASSWORD, "new": NEW_PASSWORD})
    client.post("/api/auth/login", json={"username": "friend", "password": "wrong password!"})
    client.post("/api/auth/recovery-code", json={"password": NEW_PASSWORD})
    probe = new_client()
    probe.post("/api/auth/recover", json={"username": "friend", "recovery_code": recovery, "new_password": "third long passphrase"})
    data = auth.read_user_session(cookie, SECRET)
    secrets_seen += [PASSWORD, NEW_PASSWORD, "third long passphrase", "wrong password!", code, token, token.split(".")[1], recovery, cookie, data["k"],
                     recovery.replace("-", ""), SECRET]
    text = caplog.text
    for secret in secrets_seen:
        assert secret not in text, "a secret reached the logs"
