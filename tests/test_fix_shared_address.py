"""Review round 2, item 3 and N2 (MED): behind Funnel, Docker or Serve every request arrives from one address. A stranger must not
be able to keep a real person out. Probes P2 and P3 of the re-review, plus the trusted-proxy rule and the device cookie."""

import hashlib
import ipaddress
import threading
import time

import pytest
from starlette.testclient import TestClient

from rectifi import config, hooks, users, vault
from rectifi.api import auth
from rectifi.api.app import create_app

PASSWORD = "correct horse battery"
NEW_PASSWORD = "another long passphrase"
HEADERS = {"X-Rectifi-Request": "1"}
WRONG = "not the password!"
PROXY = "127.0.0.1"


PAUSES: list[float] = []


def fast_keys(password, salt):
    """Same shape as vault.derive_password_keys, without the slow hash (these tests are about counting, not about scrypt)."""
    digest = hashlib.sha512(salt + password.encode()).digest()
    return digest[:32], digest[32:]


async def noted_pause(seconds):
    PAUSES.append(seconds)


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    PAUSES.clear()
    monkeypatch.setattr(vault, "derive_password_keys", fast_keys)
    monkeypatch.setattr(auth, "pause", noted_pause)                                       # record the delay instead of sleeping it
    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER", "RECTIFI_TRUSTED_PROXIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(hooks, "after_sign_in", lambda ctx: None)
    users.reset_limiters()
    yield
    users.reset_limiters()


def miss(*args):
    """A wrong try; once the account backoff is on, the try is refused (429) instead of counted."""
    try:
        return users.login(*args)
    except users.AuthError:
        return None


def make_account(name="alice"):
    token, recovery = users.signup(users.create_invite("friend")["code"], name, PASSWORD)
    users.confirm_signup(token)
    return recovery


def browser(peer=PROXY, **headers):
    return TestClient(create_app(), base_url="http://127.0.0.1:8501", client=(peer, 50000),
                      headers={**HEADERS, **headers}, raise_server_exceptions=False)


def login(client, password=PASSWORD, name="alice"):
    return client.post("/api/auth/login", json={"username": name, "password": password})


# ------------------------- P2: the shared address -------------------------

def test_p2_a_known_device_signs_in_at_once_after_a_strangers_misses_from_the_same_address():
    make_account()
    person = browser()
    assert login(person).status_code == 200 and auth.DEVICE_COOKIE in person.cookies      # first sign-in makes it a known device
    person.cookies.delete(auth.COOKIE_NAME)                                               # signed out, device cookie kept
    stranger = browser()
    for _ in range(users.LOCK_AFTER):
        assert login(stranger, WRONG).status_code in (401, 429)
    assert login(stranger, PASSWORD).status_code == 429                                   # the stranger's pair is blocked ...
    PAUSES.clear()
    r = login(person)                                                                     # ... the person is not, and is not delayed
    assert r.status_code == 200 and PAUSES == []


def test_p2_a_new_browser_with_the_recovery_code_gets_in_and_becomes_a_known_device():
    recovery = make_account()
    stranger = browser()
    for _ in range(users.LOCK_AFTER):
        login(stranger, WRONG)
    newcomer = browser()                                                                  # new browser, no device cookie, same address
    assert login(newcomer).status_code == 429                                             # held out by the stranger's block (the residual)
    r = newcomer.post("/api/auth/recover", json={"username": "alice", "recovery_code": recovery, "new_password": NEW_PASSWORD})
    assert r.status_code == 200 and auth.DEVICE_COOKIE in r.cookies                       # recovery works and marks this browser
    parsed = auth.read_device_cookie(r.cookies[auth.DEVICE_COOKIE], auth.session_secret())
    assert parsed is not None and parsed[0] == users.uid_of("alice")
    assert login(newcomer, NEW_PASSWORD).status_code == 200                               # now signs in with the new password, at once


def test_every_good_sign_up_confirm_login_and_recovery_sets_the_device_cookie():
    client = browser()
    token, recovery = users.signup(users.create_invite("friend")["code"], "bob", PASSWORD)
    r = client.post("/api/auth/signup/confirm", json={"signup_token": token})
    assert r.status_code == 200 and auth.DEVICE_COOKIE in r.cookies
    other = browser()
    assert auth.DEVICE_COOKIE in login(other, name="bob").cookies
    third = browser()
    r = third.post("/api/auth/recover", json={"username": "BOB", "recovery_code": recovery, "new_password": NEW_PASSWORD})
    assert r.status_code == 200 and auth.DEVICE_COOKIE in r.cookies
    wrong = browser().post("/api/auth/recover", json={"username": "bob", "recovery_code": "AAAAA-AAAAA-AAAAA-AAAAA-AAAAA",
                                                      "new_password": NEW_PASSWORD})
    assert wrong.status_code == 401 and auth.DEVICE_COOKIE not in wrong.cookies          # a failed recovery marks nobody


def test_a_known_device_is_not_slowed_by_an_account_wide_backlog():
    make_account()
    uid = users.login("alice", PASSWORD, PROXY).signed_in.uid
    for i in range(users.ACCOUNT_FREE_MISSES + 6):
        miss("alice", WRONG, f"10.0.0.{i}")
    assert users.login("alice", PASSWORD, PROXY, device=(uid, "abcd1234")).delay == 0
    with pytest.raises(users.AuthError) as err:                                           # a new browser waits (429 before any hash)
        users.login("alice", PASSWORD, PROXY)
    assert err.value.status == 429 and err.value.retry_after > 0


# ------------------------- P3: no global refusal -------------------------

def test_p3_junk_misses_do_not_refuse_new_clients_and_the_only_global_429_is_the_hash_cap(monkeypatch):
    make_account()
    for i in range(300):                                                                  # a stranger's junk, far past the old budget
        users.login(f"ghost{i}", WRONG, f"10.2.{i // 250}.{i % 250}")
    assert users.login("alice", PASSWORD, "brand-new-address").status == "ok"
    # the one remaining global 429: too many hashes at once
    gate, started = threading.Event(), []

    def slow(password, salt):
        started.append(1)
        gate.wait(5)
        return fast_keys(password, salt)

    monkeypatch.setattr(vault, "derive_password_keys", slow)
    codes = []

    def attempt(i):
        try:
            codes.append(users.login("alice", WRONG, f"10.3.0.{i}").status)
        except users.AuthError as exc:
            codes.append(exc.status)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(users.AUTH_INFLIGHT + 4)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    refused = [c for c in codes if c == 429]
    gate.set()
    for t in threads:
        t.join()
    assert len(refused) == 4 and len(started) <= users.AUTH_INFLIGHT


# ------------------------- brute force stays bounded -------------------------

def test_brute_force_is_still_blocked_per_account_and_client_pair():
    make_account()
    for _ in range(users.LOCK_AFTER):
        users.login("alice", WRONG, "7.7.7.7")
    assert users.login("alice", PASSWORD, "7.7.7.7").status == "locked"                   # even the right password: that pair is blocked
    assert users.login("alice", PASSWORD, "8.8.8.8").status == "ok"
    for _ in range(users.LOCK_AFTER):                                                     # unknown clients also meet the account backoff
        miss("alice", WRONG, "6.6.6.6")
    with pytest.raises(users.AuthError) as err:
        users.login("alice", WRONG, "5.5.5.5")
    assert err.value.status == 429


# ------------------------- the trusted proxy rule -------------------------

NETS = (ipaddress.ip_network("127.0.0.1/32"), ipaddress.ip_network("172.16.0.0/12"))


def test_default_trusts_no_proxy_and_ignores_the_header():
    assert config.trusted_proxies() == ()
    assert auth.client_address("127.0.0.1", "9.9.9.9", ()) == "127.0.0.1"


def test_a_forwarded_header_is_honoured_only_from_a_trusted_peer():
    assert auth.client_address("127.0.0.1", "203.0.113.5", NETS) == "203.0.113.5"
    assert auth.client_address("198.51.100.9", "203.0.113.5", NETS) == "198.51.100.9"       # untrusted peer: header ignored
    assert auth.client_address("127.0.0.1", None, NETS) == "127.0.0.1"                      # nothing forwarded: the peer
    assert auth.client_address("127.0.0.1", "not-an-address", NETS) == "127.0.0.1"


def test_a_client_cannot_spoof_by_writing_to_the_left_of_the_list():
    assert auth.client_address("127.0.0.1", "6.6.6.6, 203.0.113.5", NETS) == "203.0.113.5"            # the proxy appended the real one
    assert auth.client_address("172.17.0.1", "6.6.6.6, 203.0.113.5, 127.0.0.1", NETS) == "203.0.113.5"  # trusted hops are skipped
    assert auth.client_address("127.0.0.1", "127.0.0.1", NETS) == "127.0.0.1"                          # only trusted hops: the peer


def test_ipv6_clients_are_counted_by_their_slash_64():
    a = auth.client_address("127.0.0.1", "2001:db8:1:2::1", NETS)
    b = auth.client_address("127.0.0.1", "2001:db8:1:2:ffff::9", NETS)
    assert a == b and a.startswith("2001:db8:1:2:")


def test_typos_in_the_setting_trust_nothing(monkeypatch):
    monkeypatch.setenv("RECTIFI_TRUSTED_PROXIES", "localhost, 999.1.1.1, , 10.0.0.0/8")
    assert [str(n) for n in config.trusted_proxies()] == ["10.0.0.0/8"]


def test_routes_count_the_forwarded_client_only_when_the_proxy_is_trusted(monkeypatch):
    make_account()
    hdr = "X-Forwarded-For"
    # default (nobody trusted): the header is ignored, so everyone shares the proxy address
    stranger, person = browser(**{hdr: "9.9.9.9"}), browser(**{hdr: "2.2.2.2"})
    for _ in range(users.LOCK_AFTER):
        login(stranger, WRONG)
    assert login(person).status_code == 429
    # trusted proxy: the stranger has his own address, the person has another
    users.reset_limiters()
    monkeypatch.setenv("RECTIFI_TRUSTED_PROXIES", "127.0.0.1")
    stranger, person = browser(**{hdr: "9.9.9.9"}), browser(**{hdr: "2.2.2.2"})
    for _ in range(users.LOCK_AFTER):
        login(stranger, WRONG)
    assert login(stranger).status_code == 429
    assert login(person).status_code == 200
    # the same header from a peer that is NOT trusted does nothing
    users.reset_limiters()
    liar_a, liar_b = browser(peer="198.51.100.9", **{hdr: "9.9.9.9"}), browser(peer="198.51.100.9", **{hdr: "2.2.2.2"})
    for _ in range(users.LOCK_AFTER):
        login(liar_a, WRONG)
    assert login(liar_b).status_code == 429
