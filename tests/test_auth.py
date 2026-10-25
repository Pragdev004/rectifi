"""Password login: scrypt, the signed cookie, the epoch, the limiter, the SessionLock, start-up refusal."""

import time

import pytest
from starlette.testclient import TestClient

from rectifi import config
from rectifi.api import auth
from rectifi.api.app import create_app
from rectifi.api.security import install_session_lock

PASSWORD = "correct horse battery"
SECRET = "s" * 48
HEADERS = {"X-Rectifi-Request": "1"}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_SESSION_SECRET", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER"):
        monkeypatch.delenv(name, raising=False)
    auth.limiter.reset_memory()
    delays = []

    async def fake_pause(seconds):
        delays.append(seconds)

    monkeypatch.setattr(auth, "pause", fake_pause)
    yield delays
    auth.limiter.reset_memory()


@pytest.fixture
def delays(clean):
    return clean


@pytest.fixture
def stored():
    return auth.hash_password(PASSWORD)


def locked_client(monkeypatch, stored, host="http://127.0.0.1:8501", **env):
    monkeypatch.setenv("RECTIFI_PASSWORD_HASH", stored)
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", SECRET)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    install_session_lock(app)
    return TestClient(app, base_url=host, headers=HEADERS, raise_server_exceptions=False)


def sign_in(client, password=PASSWORD):
    return client.post("/api/auth/login", json={"password": password})


# ── hashing ──

def test_scrypt_round_trip_and_format(stored):
    parts = stored.split("$")
    assert parts[:4] == ["scrypt", "16384", "8", "1"] and len(parts) == 6
    assert auth.verify_password(PASSWORD, stored)
    assert not auth.verify_password(PASSWORD + "x", stored)
    assert not auth.verify_password("", stored)


def test_each_hash_has_its_own_salt():
    assert auth.hash_password(PASSWORD) != auth.hash_password(PASSWORD)


@pytest.mark.parametrize("bad", ["", "plain", "scrypt$1$2$3$a$b", "scrypt$16384$8$1$!!$!!", "bcrypt$16384$8$1$AAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                                 "scrypt$999999999$8$1$AAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"])
def test_a_broken_stored_hash_never_matches_and_never_raises(bad):
    assert auth.parse_hash(bad) is None
    assert auth.verify_password(PASSWORD, bad) is False


def test_password_rules():
    assert auth.password_problem("short") is not None
    assert auth.password_problem("a" * 12) is None


# ── cookie ──

def test_cookie_round_trip_expiry_and_tamper():
    value = auth.make_session(SECRET, 3, now=1000)
    assert auth.read_session(value, SECRET, now=1001) == {"iat": 1000, "exp": 1000 + auth.SESSION_SECONDS, "epoch": 3}
    assert auth.read_session(value, SECRET, now=1000 + auth.SESSION_SECONDS) is None            # expired
    assert auth.read_session(value, "other" * 10, now=1001) is None                              # wrong secret
    body, sig = value.split(".")
    assert auth.read_session(auth.make_session(SECRET, 4, now=1000).split(".")[0] + "." + sig, SECRET, now=1001) is None
    assert auth.read_session(body + "." + sig[:-2] + "AA", SECRET, now=1001) is None
    for junk in (None, "", "a", "a.b.c", "x" * 2000, body):
        assert auth.read_session(junk, SECRET, now=1001) is None


def test_cookie_value_finds_ours_among_others():
    assert auth.cookie_value("a=1; rectifi_session=abc.def; b=2") == "abc.def"
    assert auth.cookie_value("a=1") is None


# ── off by default ──

def test_laptop_default_has_no_login():
    c = TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)
    install_session_lock(c.app)
    assert c.get("/api/auth/status").json() == {"required": False, "signed_in": True}
    assert c.get("/api/meta").status_code != 401
    assert c.post("/api/auth/login", json={"password": "whatever12345"}).json()["error"]["code"] == "auth_off"
    assert c.post("/api/auth/logout-all", json={}).status_code == 400


# ── sign in ──

def test_every_api_route_needs_a_session_except_health_and_auth(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    for path in ("/api/meta", "/api/overview", "/api/transactions", "/api/accounts", "/api/ask/status", "/api/nothing-here"):
        r = c.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "auth_required", path
    assert c.post("/api/transactions", json={}).status_code == 401
    assert c.post("/api/auth/logout-all", json={}).status_code == 401
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/api/auth/status").json() == {"required": True, "signed_in": False}


def test_static_files_stay_public(monkeypatch, stored, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>x</title>")
    monkeypatch.setenv("RECTIFI_PASSWORD_HASH", stored)
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", SECRET)
    app = create_app(tmp_path)
    install_session_lock(app)
    c = TestClient(app, base_url="http://127.0.0.1:8501", headers=HEADERS)
    assert c.get("/ledger").status_code == 200 and c.get("/login").status_code == 200


def test_login_sets_a_strict_httponly_cookie_and_unlocks_the_api(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    r = sign_in(c)
    assert r.status_code == 200 and r.json() == {"ok": True}
    cookie = r.headers["set-cookie"].lower()
    assert "rectifi_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert f"max-age={auth.SESSION_SECONDS}" in cookie and "secure" not in cookie
    assert PASSWORD not in r.text and PASSWORD not in r.headers["set-cookie"]
    assert c.get("/api/meta").status_code == 200
    assert c.get("/api/auth/status").json() == {"required": True, "signed_in": True}


def test_cookie_is_secure_in_public_mode(monkeypatch, stored):
    c = locked_client(monkeypatch, stored, RECTIFI_PUBLIC="1")
    assert "secure" in sign_in(c).headers["set-cookie"].lower()


def test_wrong_password_is_401_without_a_cookie(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    r = sign_in(c, "not the password")
    assert r.status_code == 401 and r.json()["error"] == {"code": "invalid_password", "message": "That password isn't right.", "field": "password"}
    assert "set-cookie" not in r.headers
    assert c.get("/api/meta").status_code == 401


def test_login_needs_a_password_field(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    assert c.post("/api/auth/login", json={}).status_code == 422
    assert c.post("/api/auth/login", json={"password": 5}).status_code == 422
    assert c.post("/api/auth/login", json={"password": "x" * 5000}).status_code == 401


def test_login_keeps_the_csrf_guards(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    assert c.post("/api/auth/login", json={"password": PASSWORD}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/auth/login", json={"password": PASSWORD}, headers={"X-Rectifi-Request": "0"}).status_code == 403
    assert c.options("/api/meta").status_code == 403                    # still 403, not 401


def test_tampered_cookie_is_401(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    sign_in(c)
    value = c.cookies[auth.COOKIE_NAME]
    for bad in (value[:-3] + "AAA", "x" + value, value.split(".")[0], "garbage"):
        c.cookies.clear()
        c.cookies.set(auth.COOKIE_NAME, bad)
        assert c.get("/api/meta").status_code == 401


def test_a_cookie_signed_with_another_secret_is_401(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    forged = auth.make_session("z" * 48, auth.current_epoch())
    c.cookies.set(auth.COOKIE_NAME, forged)
    assert c.get("/api/meta").status_code == 401


def test_an_expired_session_is_401(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    sign_in(c)
    real = time.time()
    monkeypatch.setattr(auth, "_now", lambda: real + auth.SESSION_SECONDS + 5)
    assert c.get("/api/meta").status_code == 401


def test_logout_clears_the_cookie(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    sign_in(c)
    r = c.post("/api/auth/logout", json={})
    assert r.status_code == 200 and "max-age=0" in r.headers["set-cookie"].lower()
    assert c.get("/api/meta").status_code == 401


def test_epoch_bump_signs_everyone_out(monkeypatch, stored):
    a = locked_client(monkeypatch, stored)
    b = locked_client(monkeypatch, stored)
    sign_in(a), sign_in(b)
    assert a.get("/api/meta").status_code == 200 and b.get("/api/meta").status_code == 200
    r = a.post("/api/auth/logout-all", json={})
    assert r.status_code == 200
    assert a.get("/api/meta").status_code == 401 and b.get("/api/meta").status_code == 401
    assert auth.current_epoch() == 1
    sign_in(a)
    assert a.get("/api/meta").status_code == 200


def test_funnel_like_request_with_a_session_is_allowed(monkeypatch, stored):
    """A public request has a *.ts.net Host and no Tailscale-User-Login header. The password is what lets it in."""
    c = locked_client(monkeypatch, stored, host="https://rectifi.tail1234.ts.net")
    assert c.get("/api/meta").status_code == 401
    assert sign_in(c).status_code == 200
    assert c.get("/api/meta").status_code == 200


def test_a_wrong_host_is_still_refused_before_the_login(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    assert c.get("/api/meta", headers={"Host": "evil.example"}).status_code == 400


# ── limiter ──

def test_five_failures_in_a_minute_add_a_pause(monkeypatch, stored, delays):
    c = locked_client(monkeypatch, stored)
    for _ in range(5):
        assert sign_in(c, "wrong password!").status_code == 401
    assert delays == []
    assert sign_in(c, "wrong password!").status_code == 401
    assert delays == [auth.DELAY_SECONDS]
    assert sign_in(c).status_code == 200                                  # right password: allowed, but also paused
    assert delays == [auth.DELAY_SECONDS, auth.DELAY_SECONDS]
    sign_in(c, "wrong password!")                                         # a success reset the count
    assert len(delays) == 2


def test_ten_failures_lock_sign_in_for_fifteen_minutes(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    clock = [1_000_000.0]
    monkeypatch.setattr(auth, "_now", lambda: clock[0])
    for i in range(9):
        assert sign_in(c, "wrong password!").status_code == 401
        clock[0] += 10
    r = sign_in(c, "wrong password!")
    assert r.status_code == 429 and r.json()["error"]["code"] == "login_locked"
    assert r.json()["error"]["message"] == "Too many tries. Wait 15 minutes."
    assert int(r.headers["retry-after"]) == 900
    clock[0] += 600
    assert sign_in(c).status_code == 429                                   # even the right password waits
    assert 0 < int(sign_in(c).headers["retry-after"]) <= 300
    clock[0] += 400
    assert sign_in(c).status_code == 200


def test_the_lock_survives_a_restart(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    for _ in range(10):
        sign_in(c, "wrong password!")
    auth.limiter.reset_memory()                                            # a new process: memory is empty
    assert sign_in(c).status_code == 429


def test_old_failures_age_out(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    clock = [5_000_000.0]
    monkeypatch.setattr(auth, "_now", lambda: clock[0])
    for _ in range(9):
        sign_in(c, "wrong password!")
    clock[0] += auth.LOCK_WINDOW + 1
    assert sign_in(c, "wrong password!").status_code == 401                # not a 10th failure: the others are old


# ── start-up ──

def test_public_mode_without_a_login_setup_refuses_to_start(monkeypatch):
    monkeypatch.setenv("RECTIFI_PUBLIC", "1")
    with pytest.raises(auth.StartupRefused, match="set-password"):
        install_session_lock(create_app())
    monkeypatch.setenv("RECTIFI_PASSWORD_HASH", auth.hash_password(PASSWORD))
    with pytest.raises(auth.StartupRefused, match="RECTIFI_SESSION_SECRET"):
        install_session_lock(create_app())
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "short")
    with pytest.raises(auth.StartupRefused):
        install_session_lock(create_app())
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", SECRET)
    install_session_lock(create_app())


def test_a_non_loopback_bind_host_counts_as_public(monkeypatch):
    monkeypatch.setenv("RECTIFI_BIND_HOST", "0.0.0.0")
    assert config.public_mode()
    with pytest.raises(auth.StartupRefused):
        install_session_lock(create_app())


def test_a_hash_without_a_secret_refuses_to_start_even_on_a_laptop(monkeypatch, stored):
    monkeypatch.setenv("RECTIFI_PASSWORD_HASH", stored)
    with pytest.raises(auth.StartupRefused):
        install_session_lock(create_app())


def test_a_broken_hash_is_refused(monkeypatch):
    monkeypatch.setenv("RECTIFI_PASSWORD_HASH", "scrypt$nonsense")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", SECRET)
    with pytest.raises(auth.StartupRefused):
        install_session_lock(create_app())


def test_the_lock_fails_closed_if_the_setup_breaks_while_running(monkeypatch, stored):
    c = locked_client(monkeypatch, stored)
    monkeypatch.delenv("RECTIFI_SESSION_SECRET")
    r = c.get("/api/meta")
    assert r.status_code == 503 and r.json()["error"]["code"] == "auth_misconfigured"


def test_installing_twice_adds_one_lock(monkeypatch, stored):
    app = create_app()
    install_session_lock(app)
    install_session_lock(app)
    assert sum(m.cls.__name__ == "SessionLock" for m in app.user_middleware) == 1
    names = [m.cls.__name__ for m in app.user_middleware]
    assert names.index("HostGuard") < names.index("SessionLock") < names.index("ApiGuard")


def test_importing_the_login_code_loads_no_langchain():
    import subprocess, sys
    code = "import sys, rectifi.api.auth, rectifi.api.routes.auth; assert not any(m.startswith('langchain') for m in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], cwd=str(__import__('pathlib').Path(__file__).parent.parent)).returncode == 0
