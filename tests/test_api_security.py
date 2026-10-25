"""The guards in front of /api: Host, the custom header, Origin, Sec-Fetch-Site, OPTIONS, body size, response headers."""

import subprocess
import sys
from pathlib import Path

import pytest
from api_support import make_client

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    return make_client()


def test_health_needs_no_header_and_says_only_ok():
    c = make_client(headers={})
    r = c.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_a_wrong_host_is_refused_everywhere(client):
    r = client.get("/api/meta", headers={"Host": "evil.example"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_host"
    r = client.get("/ledger", headers={"Host": "evil.example"})
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/plain")
    r = client.get("/api/health", headers={"Host": "rebind.attacker.test:8501"})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["localhost:8501", "127.0.0.1", "[::1]:8501", "laptop.tail1234.ts.net", "LAPTOP.tail1234.TS.NET:443"])
def test_good_hosts_pass(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["ts.net", "evilts.net", "laptop.ts.net.evil.example", "127.0.0.1.evil.example", ""])
def test_lookalike_hosts_fail(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 400


def test_allowed_hosts_can_be_changed_by_env(monkeypatch):
    monkeypatch.setenv("RECTIFI_ALLOWED_HOSTS", "rectifi.home.test")
    c = make_client()
    assert c.get("/api/health", headers={"Host": "127.0.0.1"}).status_code == 400
    assert c.get("/api/health", headers={"Host": "rectifi.home.test"}).status_code == 200


def test_missing_custom_header_is_403():
    c = make_client(headers={})
    r = c.get("/api/meta")
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert make_client(headers={"X-Rectifi-Request": "0"}).get("/api/meta").status_code == 403


def test_cross_site_origin_on_a_write_is_403(client):
    r = client.post("/api/meta", headers={"Origin": "https://evil.example"}, json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("origin", [
    "https://laptop.tail1.ts.net", "http://127.0.0.1:5173", "http://localhost:8501", "http://[::1]:8501", "http://127.0.0.1:8501",
])
def test_good_origins_pass_the_guard(client, origin):
    # the guard lets it through to the router, which answers 405 because /api/meta only has GET
    assert client.post("/api/meta", headers={"Origin": origin}, json={}).status_code == 405


@pytest.mark.parametrize("origin", ["null", "http://evil.example", "https://127.0.0.1.evil.example", "http://laptop.ts.net", "ftp://127.0.0.1"])
def test_bad_origins_fail(client, origin):
    assert client.post("/api/meta", headers={"Origin": origin}, json={}).status_code == 403


def test_origin_from_the_env_list_is_accepted(monkeypatch):
    monkeypatch.setenv("RECTIFI_ALLOWED_ORIGINS", "https://my.proxy.example")
    c = make_client()
    assert c.post("/api/meta", headers={"Origin": "https://my.proxy.example"}, json={}).status_code == 405
    assert c.post("/api/meta", headers={"Origin": "https://other.example"}, json={}).status_code == 403


def test_origin_equal_to_host_is_accepted(client):
    r = client.post("/api/meta", headers={"Origin": "http://127.0.0.1:8501", "Host": "127.0.0.1:8501"}, json={})
    assert r.status_code == 405


def test_a_browser_write_without_origin_is_refused_but_a_plain_client_is_not(client):
    # no Origin and a Sec-Fetch-Site header: that is a browser hiding its origin
    assert client.post("/api/meta", headers={"Sec-Fetch-Site": "same-origin"}, json={}).status_code == 403
    assert client.post("/api/meta", json={}).status_code == 405        # curl / tests: neither header, so allowed through


def test_sec_fetch_site_cross_site_is_403_even_for_a_get(client):
    assert client.get("/api/meta", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/meta", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_options_is_403_and_never_carries_cors_headers(client):
    r = client.options("/api/meta", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 403
    assert not any(k.lower().startswith("access-control-") for k in r.headers)
    ok = client.get("/api/meta", headers={"Origin": "http://127.0.0.1:5173"})
    assert not any(k.lower().startswith("access-control-") for k in ok.headers)


def test_a_json_body_over_64kb_is_413(client):
    r = client.post("/api/meta", content=b"x" * (64 * 1024 + 1), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"
    assert client.post("/api/meta", content=b"{}", headers={"Content-Type": "application/json"}).status_code == 405


def test_security_headers_are_on_api_and_pages(client, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>x</title>")
    page = make_client(tmp_path).get("/ledger")
    for r in (client.get("/api/meta"), page):
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/api/meta").headers["cache-control"] == "no-store"


def test_unknown_api_path_is_json_404_and_wrong_method_is_json_405(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = client.delete("/api/meta")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"


def test_importing_the_api_loads_no_agent_or_langchain():
    code = ("import sys, rectifi.api.app\n"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('langchain', 'langchain_core', 'langgraph', 'langchain_openai') "
            "or m.startswith('rectifi.agent'))\n"
            "print(bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr


def test_rectifi_error_and_unexpected_error_shapes(client, monkeypatch):
    from rectifi import tools

    r = client.get("/api/overview?month=not-a-month")
    assert r.status_code == 422 and r.json()["error"] == {
        "code": "invalid", "message": "I couldn't read the month 'not-a-month'. Use YYYY-MM, 'this_month' or 'last_month'.", "field": None}

    def boom(*a, **k):
        raise KeyError("secret-body-text")

    monkeypatch.setattr(tools, "list_categories", boom)
    r = client.get("/api/meta")
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal"
    assert "secret-body-text" not in r.text


def test_database_down_is_503_without_the_password(client, monkeypatch):
    from rectifi.api import inputs
    from rectifi.db import DatabaseUnavailable

    def down():
        raise DatabaseUnavailable("Can't reach the database at localhost:5432/rectifi.")

    monkeypatch.setattr(inputs, "ensure_db", down)
    r = client.get("/api/meta")
    assert r.status_code == 503
    body = r.json()["error"]
    assert body["code"] == "db_unavailable" and "localhost:5432/rectifi" in body["message"] and "rectifi_dev" not in r.text


# ───────────────────────── RECTIFI_TAILSCALE_USER: lock the app to one Tailscale account ─────────────────────────

def test_without_the_env_var_there_is_no_lock(monkeypatch):
    monkeypatch.delenv("RECTIFI_TAILSCALE_USER", raising=False)
    c = make_client()
    assert c.get("/api/health", headers={"Tailscale-User-Login": "stranger@example.com"}).status_code == 200


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setenv("RECTIFI_TAILSCALE_USER", "Me@Example.com")
    return make_client()


def test_the_named_login_passes_whatever_its_case(locked):
    for login in ("me@example.com", "ME@example.com"):
        r = locked.get("/api/health", headers={"Host": "laptop.tail1234.ts.net", "Tailscale-User-Login": login})
        assert r.status_code == 200, login


def test_another_login_is_403_for_the_api_and_for_pages(locked):
    h = {"Host": "laptop.tail1234.ts.net", "Tailscale-User-Login": "friend@example.com"}
    r = locked.get("/api/meta", headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden" and "locked" in r.json()["error"]["message"]
    r = locked.get("/ledger", headers=h)
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/plain")


def test_a_tagged_device_with_an_empty_login_is_403(locked):
    r = locked.get("/api/health", headers={"Host": "laptop.tail1234.ts.net", "Tailscale-User-Login": ""})
    assert r.status_code == 403


def test_a_browser_on_the_laptop_itself_has_no_header_and_passes(locked):
    for host in ("localhost:8501", "127.0.0.1:8501", "[::1]:8501"):
        assert locked.get("/api/health", headers={"Host": host}).status_code == 200, host


def test_a_tailnet_name_with_no_login_header_is_403(locked):
    """Someone reaching the laptop by its tailnet name without Serve's header (so not through Tailscale Serve) is not let in."""
    assert locked.get("/api/health", headers={"Host": "laptop.tail1234.ts.net"}).status_code == 403


def test_the_lock_sits_in_front_of_the_api_guard(locked):
    """A wrong login is refused before anything else is looked at, even with the custom header missing."""
    c = make_client(headers={})
    r = c.get("/api/meta", headers={"Host": "laptop.tail1234.ts.net", "Tailscale-User-Login": "x@example.com"})
    assert r.status_code == 403 and "Tailscale" in r.json()["error"]["message"]
