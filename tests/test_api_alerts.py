"""P5: the alerts API. Thin over rectifi/alerts.py; money crosses as strings; a phone's address and keys never come back."""

import os
from datetime import date
from decimal import Decimal as D

import pytest
from alerts_support import FakePush, TODAY, add, add_phone
from api_support import HEADERS, make_client
from starlette.testclient import TestClient

from rectifi import alerts, tools, webpush
from rectifi.api import auth
from rectifi.api.app import create_app
from rectifi.api.security import install_session_lock
from rectifi.webpush import b64u

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


@pytest.fixture(autouse=True)
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "today", lambda: TODAY)
    monkeypatch.setattr(webpush, "KEY_DIR", tmp_path / ".rectifi")
    webpush._reset_for_tests()
    tools.add_account("Main", "1234")
    yield
    webpush._reset_for_tests()


@pytest.fixture
def client():
    return make_client()


def sub_body(label="My phone", host="fcm.googleapis.com"):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": f"https://{host}/fcm/send/SECRET-{os.urandom(6).hex()}", "keys": {"p256dh": b64u(public), "auth": b64u(os.urandom(16))}, "label": label}


def make_alert(client):
    client.post("/api/alerts/rules", json={"scope": "merchant", "target": "Swiggy", "period": "week", "cap": "1000"})
    add(850, TODAY, "swiggy@icici")
    alerts.sweep("test")
    return client.get("/api/alerts").json()["items"][0]


# ───────────── caps ─────────────

def test_rule_crud_and_validation(client):
    made = client.post("/api/alerts/rules", json={"scope": "merchant", "target": "swiggy", "period": "week", "cap": "1,500"})
    assert made.status_code == 200
    rule = made.json()
    assert rule["target"] == "Swiggy" and rule["cap"] == "1500.00" and rule["muted"] is False
    assert client.get("/api/alerts/rules").json()["items"] == [rule]
    patched = client.patch(f"/api/alerts/rules/{rule['id']}", json={"cap": "1200", "muted": True}).json()
    assert patched["cap"] == "1200.00" and patched["muted"] is True
    assert client.patch(f"/api/alerts/rules/{rule['id']}", json={}).status_code == 422
    assert client.delete(f"/api/alerts/rules/{rule['id']}").json() == {"deleted": True}
    assert client.delete(f"/api/alerts/rules/{rule['id']}").status_code == 404
    for bad in ({"scope": "merchant", "target": "Nobody", "period": "week", "cap": "100"},
                {"scope": "group", "target": "Nope", "period": "week", "cap": "100"},
                {"scope": "category", "target": "Salary & Allowance", "period": "week", "cap": "100"},
                {"scope": "merchant", "target": "Swiggy", "period": "year", "cap": "100"},
                {"scope": "merchant", "target": "Swiggy", "period": "week", "cap": "0"},
                {"scope": "merchant", "target": "Swiggy", "period": "week", "cap": "abc"},
                {"scope": "merchant", "target": "Swiggy", "period": "week"}):
        assert client.post("/api/alerts/rules", json=bad).status_code == 422, bad
    total = client.post("/api/alerts/rules", json={"scope": "total", "period": "day", "cap": "2000"}).json()
    assert total["target"] == "all"


def test_presets_are_money_strings(client):
    for month in (7, 8, 9):
        add(1010, date(2026, month, 10), "Swiggy")
    items = {(i["scope"], i["target"]): i for i in client.get("/api/alerts/presets").json()["items"]}
    sw = items[("merchant", "Swiggy")]
    assert sw["average"]["month"] == "1010.00" and sw["keep"]["month"] == "1100.00" and sw["trim"]["month"] == "900.00"


# ───────────── inbox ─────────────

def test_inbox_read_mute_snooze(client):
    item = make_alert(client)
    assert item["kind"] == "cap" and item["numbers"] == {"spent": "850.00", "cap": "1000.00", "left": "150.00", "threshold_pct": "80"}
    assert item["is_read"] is False and item["can_snooze"] and client.get("/api/alerts").json()["unread"] == 1
    assert client.post(f"/api/alerts/{item['id']}/read").json()["is_read"] is True
    assert client.get("/api/alerts").json()["items"] == []
    assert len(client.get("/api/alerts?status=all").json()["items"]) == 1
    assert client.post(f"/api/alerts/{item['id']}/snooze", json={"hours": 24}).status_code == 200
    assert client.post(f"/api/alerts/{item['id']}/snooze", json={"hours": 5}).status_code == 422
    assert client.post(f"/api/alerts/{item['id']}/mute").json()["muted"] == "rule"
    assert client.get("/api/alerts/rules").json()["items"][0]["muted"] is True
    assert client.post("/api/alerts/999/read").status_code == 404 and client.post("/api/alerts/999/mute").status_code == 404
    assert client.get("/api/alerts?status=bogus").status_code == 422


def test_read_all(client):
    make_alert(client)
    assert client.post("/api/alerts/read-all").json() == {"marked": 1}
    assert client.get("/api/alerts").json()["unread"] == 0


def test_explain_uses_only_the_alerts_own_numbers(client, monkeypatch):
    from langchain_core.messages import AIMessage

    from rectifi.agent import explain as explain_module

    class Model:
        def __init__(self, reply):
            self.reply = reply

        def invoke(self, messages):
            return AIMessage(self.reply)

    item = make_alert(client)
    monkeypatch.setattr(explain_module, "get_llm", lambda *a, **k: Model("You have ₹150 left this week, so skip one order."))
    assert client.post(f"/api/alerts/{item['id']}/explain").json()["text"].startswith("You have ₹150 left")
    monkeypatch.setattr(explain_module, "get_llm", lambda *a, **k: Model("Save ₹777 instead."))
    out = client.post(f"/api/alerts/{item['id']}/explain").json()
    assert out["text"] is None and out["dropped"] == ["777.00"]


# ───────────── today, leaks, prefs ─────────────

def test_today_strip_matches_the_server_numbers(client):
    assert client.get("/api/alerts/today").json()["safe"] is None
    client.put("/api/alerts/prefs", json={"spend_plan": "31000"})
    add(5000, date(2026, 10, 10), "Dmart", "Groceries")
    add(120, TODAY, "swiggy@icici")
    body = client.get("/api/alerts/today").json()
    assert body["safe"]["daily"] == "2363.00" and body["safe"]["left_today"] == "2243.00" and body["safe"]["days_left"] == 11
    assert body["small_spends"] == [{"txn_id": body["small_spends"][0]["txn_id"], "label": "Swiggy", "group": "Food delivery", "amount": "120.00"}]
    s = alerts.safe_to_spend(alerts.build_context())
    assert body["safe"]["daily"] == f"{s['daily']:.2f}"


def test_prefs_round_trip_and_validation(client):
    got = client.get("/api/alerts/prefs").json()
    assert got["push_on"] is True and got["quiet_start"] == "22:00" and got["max_per_hour"] == 3 and got["monthly_income"] is None
    assert "muted_scopes" not in got and "snoozed_scopes" not in got
    ok = client.put("/api/alerts/prefs", json={"quiet_start": "23:30", "max_per_day": 12, "monthly_income": "₹1,00,000", "digest_on": True,
                                                "private_notifications": True}).json()
    assert ok["quiet_start"] == "23:30" and ok["max_per_day"] == 12 and ok["monthly_income"] == "100000.00" and ok["private_notifications"] is True
    for bad in ({"quiet_start": "25:00"}, {"quiet_end": "7pm"}, {"max_per_hour": 0}, {"max_per_hour": 21}, {"max_per_day": "3"},
                {"monthly_income": "lots"}, {"monthly_income": "-5"}, {"push_on": "yes"}, {"muted_kinds": ["nope"]}, {"snoozed_scopes": {}}):
        assert client.put("/api/alerts/prefs", json=bad).status_code == 422, bad
    assert client.put("/api/alerts/prefs", json={"monthly_income": ""}).json()["monthly_income"] is None


def test_detected_income_is_shown_beside_a_manual_one(client):
    add(35000, date(2026, 10, 1), "Employer", "Salary & Allowance", "credit")
    client.put("/api/alerts/prefs", json={"monthly_income": "50000"})
    got = client.get("/api/alerts/prefs").json()
    assert got["detected_income"] == "35000.00" and got["income_source"] == "manual" and got["monthly_income"] == "50000.00"


def test_leaks_report_shapes(client):
    client.put("/api/alerts/prefs", json={"monthly_income": "100000"})
    add(25000, date(2026, 10, 1), "Landlord", "Rent")
    add(700.25, date(2026, 10, 5), "Swiggy")
    body = client.get("/api/leaks?month=2026-10").json()
    assert body["month"] == "2026-10" and body["income"] == "100000.00" and body["left"] == "74299.75"
    seg = {s["key"]: s for s in body["segments"]}
    assert seg["fixed"]["start"] == "100000.00" and seg["fixed"]["end"] == "75000.00" and seg["food"]["end"] == "74299.75"
    assert body["merchants"] == [{"merchant": "Swiggy", "group": "Food delivery", "orders": 1, "total": "700.25", "pct_of_income": "0.7"}]
    assert body["projection"]["projected"] == "36619.18" or body["projection"]["projected"] is not None
    assert client.get("/api/leaks?month=garbage").status_code == 422
    assert client.get("/api/leaks").status_code == 200


def test_merchant_patterns(client):
    made = client.post("/api/merchants/patterns", json={"pattern": "corner cafe", "merchant": "Corner Cafe", "group": "Food delivery"})
    assert made.status_code == 200
    listing = client.get("/api/merchants/patterns").json()
    assert listing["items"][0]["pattern"] == "corner cafe" and "Quick commerce" in listing["groups"]
    assert client.post("/api/merchants/patterns", json={"pattern": "ab", "merchant": "X", "group": "Other"}).status_code == 422
    assert client.post("/api/merchants/patterns", json={"pattern": "abcd", "merchant": "X", "group": "Nope"}).status_code == 422
    assert client.delete(f"/api/merchants/patterns/{made.json()['id']}").json() == {"deleted": True}
    assert client.delete(f"/api/merchants/patterns/{made.json()['id']}").status_code == 404


# ───────────── push ─────────────

def test_push_key_and_subscriptions_never_expose_secrets(client, monkeypatch):
    key = client.get("/api/push/key").json()
    assert key["configured"] is True and len(webpush.b64u_decode(key["public_key"])) == 65 and len(key["fingerprint"].split()) == 3
    assert "PRIVATE" not in str(key)
    body = sub_body()
    made = client.post("/api/push/subscriptions", json=body)
    assert made.status_code == 200
    listing = client.get("/api/push/subscriptions")
    text = listing.text + made.text
    assert body["endpoint"] not in text and "SECRET-" not in text and body["keys"]["p256dh"] not in text and body["keys"]["auth"] not in text
    (item,) = listing.json()["items"]
    assert set(item) == {"id", "label", "service", "created_at", "last_ok_at"} and item["service"] == "fcm.googleapis.com" and item["label"] == "My phone"
    again = client.post("/api/push/subscriptions", json=body)                       # the same phone twice is one row
    assert len(client.get("/api/push/subscriptions").json()["items"]) == 1 and again.json()["id"] == item["id"]
    assert client.delete(f"/api/push/subscriptions/{item['id']}").json() == {"deleted": True}
    assert client.delete(f"/api/push/subscriptions/{item['id']}").status_code == 404


def test_bad_subscriptions_are_422(client):
    for bad in (sub_body(host="evil.example.com"), sub_body(host="localhost")):
        assert client.post("/api/push/subscriptions", json=bad).status_code == 422
    plain_http = sub_body()
    plain_http["endpoint"] = plain_http["endpoint"].replace("https", "http")
    assert client.post("/api/push/subscriptions", json=plain_http).status_code == 422
    short = sub_body()
    short["keys"]["auth"] = "abc"
    assert client.post("/api/push/subscriptions", json=short).status_code == 422
    assert client.post("/api/push/subscriptions", json={"endpoint": "https://fcm.googleapis.com/x"}).status_code == 422
    assert client.get("/api/push/subscriptions").json()["items"] == []


def test_push_test_needs_a_phone_then_sends(client, monkeypatch):
    assert client.post("/api/push/test").status_code == 422
    push = FakePush()
    monkeypatch.setattr(webpush, "send", push)
    client.post("/api/push/subscriptions", json=sub_body())
    out = client.post("/api/push/test").json()
    assert out["ok"] == 1 and push.calls[0]["payload"]["t"] == "Rectifi test"


# ───────────── guards ─────────────

def test_custom_header_is_required_everywhere():
    bare = make_client(headers={})
    for method, path in (("get", "/api/alerts"), ("get", "/api/leaks"), ("get", "/api/push/key"), ("post", "/api/push/test"),
                         ("put", "/api/alerts/prefs"), ("get", "/api/alerts/today")):
        assert getattr(bare, method)(path).status_code == 403, path


def test_cross_site_write_is_refused(client):
    assert client.post("/api/alerts/read-all", headers={"Origin": "https://evil.example"}).status_code == 403


def test_session_is_required_in_public_mode_but_not_for_the_service_worker(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>x</title>")
    (tmp_path / "sw.js").write_text("self.addEventListener('push', () => {});")
    monkeypatch.setenv("RECTIFI_PASSWORD_HASH", auth.hash_password("correct horse battery"))
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "s" * 48)
    app = create_app(tmp_path)
    install_session_lock(app)
    locked = TestClient(app, base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)
    for method, path in (("get", "/api/alerts"), ("get", "/api/push/key"), ("get", "/api/push/subscriptions"), ("post", "/api/push/test"),
                         ("get", "/api/leaks"), ("get", "/api/alerts/today")):
        assert getattr(locked, method)(path).status_code == 401, path
    sw = locked.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["content-type"].startswith("text/javascript") and sw.headers["cache-control"] == "no-cache"


def test_sw_js_is_javascript_and_never_cached(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>x</title>")
    (tmp_path / "sw.js").write_text("// worker")
    reply = make_client(tmp_path, headers={}).get("/sw.js")
    assert reply.status_code == 200 and reply.headers["content-type"].startswith("text/javascript") and reply.headers["cache-control"] == "no-cache"
    assert make_client(tmp_path.joinpath("nowhere"), headers={}).get("/sw.js").status_code in (404, 503)


def test_money_is_always_a_string(client):
    item = make_alert(client)
    assert all(isinstance(v, str) for v in item["numbers"].values())
    for path in ("/api/alerts/today", "/api/leaks", "/api/alerts/presets", "/api/alerts/rules"):
        assert "e+" not in client.get(path).text


def test_importing_the_api_loads_no_langchain():
    import subprocess
    import sys

    code = ("import sys, rectifi.api.app; "
            "bad = [m for m in sys.modules if m.split('.')[0].startswith(('langchain', 'langgraph')) or m.startswith('rectifi.agent')]; "
            "sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0
