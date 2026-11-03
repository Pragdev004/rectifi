"""Review round 1, MED: a stranger must not be able to keep a person out of their account, and a sign-in flood must not stall the app.

Design (rectifi/users.py, top): no account lock. Misses are counted per (account, client); the account gets a slowing delay, never a
refusal; recovery has its own bucket; hashing in flight is capped so a flood gets 429 at once.
"""

import threading
import time

import pytest
from starlette.testclient import TestClient

from rectifi import hooks, users, vault
from rectifi.api import auth
from rectifi.api.app import create_app

PASSWORD = "correct horse battery"
NEW_PASSWORD = "another long passphrase"
HEADERS = {"X-Rectifi-Request": "1"}
WRONG = "not the password!"


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(hooks, "after_sign_in", lambda ctx: None)
    users.reset_limiters()
    yield
    users.reset_limiters()


def make_account(name="alice"):
    token, recovery = users.signup(users.create_invite("friend")["code"], name, PASSWORD)
    users.confirm_signup(token)
    return recovery


def stranger_floods(name="alice", client="9.9.9.9", times=None):
    for _ in range(times or users.LOCK_AFTER):
        users.login(name, WRONG, client)


def test_a_stranger_who_guesses_the_name_blocks_only_his_own_client():
    make_account()
    stranger_floods()
    assert users.login("alice", PASSWORD, "9.9.9.9").status == "locked"          # his pair is blocked...
    real = users.login("alice", PASSWORD, "2.2.2.2")                              # ...the person from anywhere else signs in at once
    assert real.status == "ok"


def test_the_account_is_never_locked_however_many_clients_miss():
    make_account()
    for i in range(users.ACCOUNT_FREE_MISSES + 4):
        users.login("alice", WRONG, f"10.0.0.{i}")                                # a spread-out guesser
    ok = users.login("alice", PASSWORD, "2.2.2.2")
    assert ok.status == "ok"                                                      # slower, never refused
    assert ok.delay > 0                                                           # the account-wide delay grew
    assert ok.delay <= users.ACCOUNT_MAX_DELAY


def test_a_known_device_skips_the_account_delay_and_a_shared_address(monkeypatch):
    make_account()
    uid = users.login("alice", PASSWORD, "proxy").signed_in.uid
    stranger_floods(client="proxy")                                               # behind one proxy, every client looks the same
    assert users.login("alice", PASSWORD, "proxy").status == "locked"             # a browser without the cookie shares the stranger's block
    known = users.login("alice", PASSWORD, "proxy", device=(uid, "abcd1234"))     # a browser the person signed in with before
    assert known.status == "ok" and known.delay == 0
    for i in range(users.ACCOUNT_FREE_MISSES + 4):
        users.login("alice", WRONG, f"10.0.0.{i}")
    assert users.login("alice", PASSWORD, "elsewhere", device=(uid, "abcd1234")).delay == 0
    assert users.login("alice", PASSWORD, "elsewhere", device=(uid + 1, "abcd1234")).delay > 0   # another account's cookie proves nothing


def test_a_failed_password_never_blocks_recovery_and_the_reverse():
    recovery = make_account()
    stranger_floods(client="proxy")
    assert users.login("alice", WRONG, "proxy").status == "locked"
    new_code = users.recover("alice", recovery, NEW_PASSWORD, "proxy")            # same client: recovery still works
    assert new_code
    assert users.login("alice", NEW_PASSWORD, "elsewhere").status == "ok"
    for _ in range(users.LOCK_AFTER):
        with pytest.raises(users.AuthError):
            users.recover("alice", "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", PASSWORD + "!", "proxy")
    assert users.login("alice", NEW_PASSWORD, "fresh-client").status == "ok"     # recovery misses did not touch sign-in


def test_an_unknown_name_behaves_like_a_real_one():
    for _ in range(users.LOCK_AFTER):
        users.login("nobody-here", WRONG, "9.9.9.9")
    assert users.login("nobody-here", WRONG, "9.9.9.9").status == "locked"
    assert users.login("nobody-here", WRONG, "1.1.1.1").status == "invalid"


def test_the_block_ends_by_itself(monkeypatch):
    make_account()
    clock = [time.time()]
    monkeypatch.setattr(users, "_now", lambda: clock[0])
    stranger_floods(client="9.9.9.9")
    assert users.login("alice", PASSWORD, "9.9.9.9").status == "locked"
    clock[0] += users.LOCK_SECONDS + 5
    assert users.login("alice", PASSWORD, "9.9.9.9").status == "ok"


def test_the_login_route_sets_a_known_device_cookie_and_uses_it():
    make_account()
    client = TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 200 and auth.DEVICE_COOKIE in r.cookies
    cookie = r.cookies[auth.DEVICE_COOKIE]
    parsed = auth.read_device_cookie(cookie, auth.session_secret())
    assert parsed is not None and parsed[0] >= 1
    assert auth.read_device_cookie(cookie[:-3] + "AAA", auth.session_secret()) is None        # tampered
    # the "testclient" address is blocked by a stranger's misses; this browser (with the cookie) still gets in
    stranger = TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)
    for _ in range(users.LOCK_AFTER):
        stranger.post("/api/auth/login", json={"username": "alice", "password": WRONG})
    assert stranger.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 429
    client.cookies.delete(auth.COOKIE_NAME)
    assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200


# ───────────────────────── the flood ─────────────────────────

def test_a_sign_in_flood_gets_429_at_once_and_does_not_hold_the_threads_the_app_needs(monkeypatch):
    make_account()
    real = vault.derive_password_keys
    running, peak, guard = [0], [0], threading.Lock()

    def slow(password, salt):
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            time.sleep(0.4)
            return real(password, salt)
        finally:
            with guard:
                running[0] -= 1

    monkeypatch.setattr(vault, "derive_password_keys", slow)
    client = TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)
    codes, started = [], time.time()

    def attempt(i):
        r = client.post("/api/auth/login", json={"username": f"ghost{i}", "password": WRONG})
        codes.append(r.status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    time.sleep(0.15)                                                              # the flood is in flight
    t0 = time.time()
    status = client.get("/api/auth/status")                                       # an ordinary request meanwhile
    quick = time.time() - t0
    for t in threads:
        t.join()
    assert status.status_code == 200 and quick < 1.0
    assert peak[0] <= users.AUTH_INFLIGHT                                         # the cap on hashes in flight holds
    assert codes.count(429) >= 30 - users.AUTH_INFLIGHT - 2                       # the rest were refused without waiting
    assert time.time() - started < 6.0
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    monkeypatch.setattr(vault, "derive_password_keys", real)
    assert r.status_code in (200, 429)


def test_a_flood_of_misses_makes_new_clients_wait_but_known_devices_still_sign_in(monkeypatch):
    make_account()
    uid = users.login("alice", PASSWORD, "home").signed_in.uid
    for i in range(users.GLOBAL_MISS_LIMIT):
        users.miss_window.add(time.time())
    with pytest.raises(users.AuthError) as err:
        users.login("alice", PASSWORD, "new")
    assert err.value.status == 429 and err.value.code == "login_busy"
    assert users.login("alice", PASSWORD, "new", device=(uid, "abcd1234")).status == "ok"
