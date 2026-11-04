"""Review round 3, N4: the per-account backoff is applied BEFORE hashing for a client without a known device, so parallel guesses
from many addresses are capped by it (they used to ignore it: the delay came after the hash). A known device and the recovery
code are not affected."""

import hashlib
import threading
import time

import pytest
from starlette.testclient import TestClient

from rectifi import hooks, users, vault
from rectifi.api import auth
from rectifi.api.app import create_app

PASSWORD = "correct horse battery"
NEW_PASSWORD = "another long passphrase"
WRONG = "not the password!"
HEADERS = {"X-Rectifi-Request": "1"}
HASHES: list[int] = []
GATE = threading.Event()


def counting_keys(password, salt):
    HASHES.append(1)
    GATE.wait(10)
    digest = hashlib.sha512(salt + password.encode()).digest()
    return digest[:32], digest[32:]


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    HASHES.clear()
    GATE.set()
    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER", "RECTIFI_TRUSTED_PROXIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(vault, "derive_password_keys", counting_keys)
    monkeypatch.setattr(hooks, "after_sign_in", lambda ctx: None)
    users.reset_limiters()
    yield
    GATE.set()
    users.reset_limiters()


def make_account(name="alice"):
    token, recovery = users.signup(users.create_invite("friend")["code"], name, PASSWORD)
    who = users.confirm_signup(token)
    return who.uid, recovery


def spread_misses(count, name="alice"):
    """Misses from many different addresses (a proxy that shows each guess as a new client)."""
    for i in range(count):
        try:
            users.login(name, WRONG, f"10.0.{i // 200}.{i % 200}")
        except users.AuthError:
            pass                                                  # refused by the backoff: not a miss


def test_parallel_guesses_from_many_addresses_run_one_hash_per_backoff(monkeypatch):
    make_account()
    spread_misses(users.ACCOUNT_FREE_MISSES + 1)
    clock = [time.time() + 3600]                                  # the backoff of the last miss has long passed
    monkeypatch.setattr(users, "_now", lambda: clock[0])
    users.login_throttle._names["alice"] = [clock[0] - 5 - i for i in range(users.ACCOUNT_FREE_MISSES + 1)]   # 11 misses, latest 5 s ago
    HASHES.clear()
    GATE.clear()
    codes, errors = [], []

    def guess(i):
        try:
            codes.append(users.login("alice", WRONG, f"172.16.{i // 200}.{i % 200}").status)
        except users.AuthError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=guess, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    GATE.set()
    for t in threads:
        t.join()
    assert len(HASHES) == 1                                       # one guess got through, not min(40, hash slots)
    assert len(errors) == 39 and all(e.status == 429 and e.code == "login_wait" and e.retry_after >= 1 for e in errors)


def test_the_backoff_refuses_before_any_hash_and_a_refusal_is_not_a_miss():
    make_account()
    spread_misses(users.ACCOUNT_FREE_MISSES + 1)
    HASHES.clear()
    before = len(users.login_throttle._names["alice"])
    for i in range(20):
        with pytest.raises(users.AuthError) as err:
            users.login("alice", PASSWORD, f"192.168.1.{i}")      # even the right password waits: a new client is not told apart
        assert err.value.status == 429
    assert HASHES == [] and len(users.login_throttle._names["alice"]) == before


def test_an_unknown_name_gets_the_same_backoff_so_nothing_is_learned():
    spread_misses(users.ACCOUNT_FREE_MISSES + 1, name="nobody")
    HASHES.clear()
    with pytest.raises(users.AuthError) as err:
        users.login("nobody", WRONG, "1.2.3.4")
    assert err.value.status == 429 and err.value.code == "login_wait" and HASHES == []


def test_a_good_sign_in_after_the_wait_leaves_no_provisional_miss(monkeypatch):
    make_account()
    monkeypatch.setattr(users, "_now", lambda: time.time() + 1)
    assert users.login("alice", PASSWORD, "1.1.1.1").status == "ok"
    assert users.login_throttle._names.get("alice") == []


def test_a_known_device_is_never_held_by_the_account_backoff():
    uid, _ = make_account()
    spread_misses(users.ACCOUNT_FREE_MISSES + 6)
    got = users.login("alice", PASSWORD, "5.5.5.5", device=(uid, "abcd1234"))
    assert got.status == "ok" and got.delay == 0


def test_recovery_still_works_during_the_backoff_and_sets_the_device_cookie():
    _, recovery = make_account()
    spread_misses(users.ACCOUNT_FREE_MISSES + 6)
    browser = TestClient(create_app(), base_url="http://127.0.0.1:8501", client=("127.0.0.1", 50000), headers=HEADERS,
                         raise_server_exceptions=False)
    r = browser.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and r.json()["error"]["code"] == "login_wait"
    r = browser.post("/api/auth/recover", json={"username": "alice", "recovery_code": recovery, "new_password": NEW_PASSWORD})
    assert r.status_code == 200 and auth.DEVICE_COOKIE in r.cookies
