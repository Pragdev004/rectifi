"""Review round 3, N7 (MED): junk sign-ins in parallel used to take every hash slot, so a known device with the right password or a
recovery got 429. The hash cap now has lanes: a known device (a verified signed cookie for that account), a proven recovery, and
everyone else. A stranger can only fill the lane for everyone else."""

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
GATE = threading.Event()
JUNK_HASHES: list[int] = []


def gated_keys(password, salt):
    """Junk (unknown-name) hashes wait on GATE like a slow scrypt; real accounts hash at once."""
    if salt == users._DUMMY_SALT:
        JUNK_HASHES.append(1)
        GATE.wait(10)
    digest = hashlib.sha512(salt + password.encode()).digest()
    return digest[:32], digest[32:]


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    GATE.clear()
    JUNK_HASHES.clear()
    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER", "RECTIFI_TRUSTED_PROXIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(vault, "derive_password_keys", gated_keys)
    monkeypatch.setattr(hooks, "after_sign_in", lambda ctx: None)
    users.reset_limiters()
    yield
    GATE.set()
    users.reset_limiters()


def make_account(name="alice"):
    GATE.set()
    token, recovery = users.signup(users.create_invite("friend")["code"], name, PASSWORD)
    who = users.confirm_signup(token)
    GATE.clear()
    return who.uid, recovery


class Flood:
    """About eight parallel junk sign-ins (unknown names), every one stuck in its hash until the gate opens."""

    def __init__(self, count=8):
        self.codes: list = []
        self.threads = [threading.Thread(target=self.one, args=(i,)) for i in range(count)]

    def one(self, i):
        try:
            self.codes.append(users.login(f"ghost{i}", WRONG, f"10.9.0.{i}").status)
        except users.AuthError as exc:
            self.codes.append(exc.status)

    def __enter__(self):
        for t in self.threads:
            t.start()
        deadline = time.time() + 5
        while len(JUNK_HASHES) < users.AUTH_INFLIGHT and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)                                         # the rest have been refused by now
        return self

    def __exit__(self, *exc):
        GATE.set()
        for t in self.threads:
            t.join()


def test_a_flood_fills_only_the_lane_for_everyone_else():
    make_account()
    with Flood() as flood:
        assert len(JUNK_HASHES) == users.AUTH_INFLIGHT           # the "other" lane is full ...
        assert flood.codes.count(429) == 8 - users.AUTH_INFLIGHT  # ... and the rest were refused at once
        with pytest.raises(users.AuthError) as err:               # a new client with the right password has no free lane
            users.login("alice", PASSWORD, "10.8.0.1")
        assert err.value.status == 429


def test_a_known_device_with_the_right_password_signs_in_while_the_flood_runs():
    uid, _ = make_account()
    with Flood():
        got = users.login("alice", PASSWORD, "10.8.0.1", device=(uid, "abcd1234"))
    assert got.status == "ok"


def test_another_accounts_device_cookie_does_not_open_the_device_lane():
    uid, _ = make_account()
    with Flood():
        with pytest.raises(users.AuthError) as err:
            users.login("alice", PASSWORD, "10.8.0.1", device=(uid + 1, "abcd1234"))
        assert err.value.status == 429
        with pytest.raises(users.AuthError) as err:
            users.login("alice", PASSWORD, "10.8.0.1", device=None)
        assert err.value.status == 429


def test_a_recovery_succeeds_while_the_flood_runs():
    _, recovery = make_account()
    with Flood():
        code = users.recover("alice", recovery, NEW_PASSWORD, "10.8.0.1")
    assert code and code != recovery


def test_a_full_device_lane_does_not_touch_the_other_lanes(monkeypatch):
    uid, recovery = make_account()
    held = [users._LANE_SLOTS["device"].acquire(blocking=False) for _ in range(users.AUTH_DEVICE_INFLIGHT)]
    try:
        with pytest.raises(users.AuthError):
            users.login("alice", PASSWORD, "10.8.0.1", device=(uid, "abcd1234"))
        assert users.login("alice", PASSWORD, "10.8.0.2").status == "ok"
        assert users.recover("alice", recovery, NEW_PASSWORD, "10.8.0.3")
    finally:
        for ok in held:
            if ok:
                users._LANE_SLOTS["device"].release()


def test_the_lanes_together_bound_the_hashing_threads():
    total = users.AUTH_INFLIGHT + users.AUTH_DEVICE_INFLIGHT + users.AUTH_RECOVERY_INFLIGHT
    assert sum(users._LANES.values()) == total and total <= 8      # the threadpool keeps free threads for the session check


def test_through_the_routes_a_known_browser_and_a_recovery_pass_the_flood():
    uid, recovery = make_account()
    GATE.set()
    person = TestClient(create_app(), base_url="http://127.0.0.1:8501", client=("127.0.0.1", 50000), headers=HEADERS,
                        raise_server_exceptions=False)
    assert person.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    assert auth.DEVICE_COOKIE in person.cookies
    person.cookies.delete(auth.COOKIE_NAME)
    GATE.clear()
    with Flood():
        r = person.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert r.status_code == 200
        other = TestClient(create_app(), base_url="http://127.0.0.1:8501", client=("127.0.0.1", 50001), headers=HEADERS,
                           raise_server_exceptions=False)
        r = other.post("/api/auth/recover", json={"username": "alice", "recovery_code": recovery, "new_password": NEW_PASSWORD})
        assert r.status_code == 200 and auth.DEVICE_COOKIE in r.cookies
