"""The Phone page API: status, pairing (the token appears once), the setup file, test message, revoke, the inbox."""

from datetime import datetime, timezone

import pytest
from api_support import make_client

from rectifi import tools
from rectifi.api.routes import phone

NAME = "laptop.tail1234.ts.net"


@pytest.fixture
def client():
    return make_client()


def tailscale_on(monkeypatch, published=True):
    monkeypatch.setattr(phone, "tailscale_info", lambda: {"installed": True, "name": NAME, "app_published": published, "gateway_published": published})


def tailscale_off(monkeypatch, installed=False):
    monkeypatch.setattr(phone, "tailscale_info", lambda: {"installed": installed, "name": None, "app_published": False, "gateway_published": False})


def test_status_without_tailscale(client, monkeypatch):
    tailscale_off(monkeypatch)
    body = client.get("/api/phone/status").json()
    assert body["tailscale"] == {"installed": False, "name": None, "app_published": False, "gateway_published": False}
    assert body["app_url"] is None and body["gateway_url"] is None and body["health_url"] is None
    assert body["gateway"]["host"] == "127.0.0.1" and body["gateway"]["open"] is False and body["lan_addresses"] == []
    assert body["gateway"]["up"] is False and body["tailscale_login"] is None


def test_status_with_tailscale_and_the_login_header(client, monkeypatch):
    tailscale_on(monkeypatch)
    body = client.get("/api/phone/status", headers={"Tailscale-User-Login": "prag@example.com"}).json()
    assert body["app_url"] == f"https://{NAME}/" and body["gateway_url"] == f"https://{NAME}:8443/sms"
    assert body["health_url"] == f"https://{NAME}:8443/health" and body["tailscale_login"] == "prag@example.com"
    assert body["tailscale"]["app_published"] is True


def test_lan_addresses_only_when_the_host_is_opened_on_purpose(client, monkeypatch):
    tailscale_off(monkeypatch)
    monkeypatch.setenv("RECTIFI_SMS_HOST", "0.0.0.0")
    monkeypatch.setattr(phone.sms_gateway, "private_ipv4s", lambda: [{"ip": "192.168.1.9", "label": "Local network"}])
    body = client.get("/api/phone/status").json()
    assert body["gateway"]["open"] is True and body["lan_addresses"] == [{"ip": "192.168.1.9", "label": "Local network"}]
    assert body["gateway_url"].startswith("http://192.168.1.9:") and body["gateway_url"].endswith("/sms")


def test_pairing_returns_the_token_once_and_never_again(client, monkeypatch):
    tailscale_on(monkeypatch)
    r = client.post("/api/phone/devices", json={"name": "My Android"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    token = body["token"]
    assert token.startswith("rf_") and body["device"]["name"] == "My Android" and set(body["device"]) == {"id", "name", "created_at"}
    assert body["setup_unavailable_reason"] is None
    assert body["manual"]["webhook_url"] == f"https://{NAME}:8443/sms"
    assert body["manual"]["headers_json"] == '{"Authorization": "Bearer ' + token + '"}'
    assert "%text%" in body["manual"]["template"] and body["manual"]["text_regex"] and body["manual"]["sender_regex"]
    for later in (client.get("/api/phone/devices"), client.get("/api/phone/status"), client.get("/api/phone/inbox"), client.get("/api/meta")):
        assert token not in later.text
    listed = client.get("/api/phone/devices").json()
    assert [d["name"] for d in listed] == ["My Android"] and set(listed[0]) == {"id", "name", "created_at", "last_seen_at", "revoked_at"}
    assert tools.authenticate_device(token) is not None


def test_the_setup_file_has_the_url_and_bearer(client, monkeypatch):
    import json

    tailscale_on(monkeypatch)
    body = client.post("/api/phone/devices", json={"name": "Pixel"}).json()
    f = body["setup_file"]
    assert f["filename"] == "rectifi-sms-setup.json"
    assert f"https://{NAME}:8443/sms" in f["content"] and "Bearer " + body["token"] in f["content"]
    rules = json.loads(f["content"])
    assert isinstance(rules, list) and rules[0]["key"] == f"rectifi-{body['device']['id']}"


def test_no_gateway_address_means_no_file_and_a_reason(client, monkeypatch):
    tailscale_off(monkeypatch)
    body = client.post("/api/phone/devices", json={"name": "Pixel"}).json()
    assert body["setup_file"] is None and "Tailscale" in body["setup_unavailable_reason"]
    assert body["manual"]["webhook_url"] is None and body["token"].startswith("rf_")


def test_pairing_validation(client, monkeypatch):
    tailscale_off(monkeypatch)
    r = client.post("/api/phone/devices", json={"name": "  "})
    assert r.status_code == 422 and "Give the phone a name" in r.json()["error"]["message"]
    r = client.post("/api/phone/devices", json={"name": "x" * 61})
    assert r.status_code == 422
    assert tools.list_devices() == []


def test_revoke(client, monkeypatch):
    tailscale_off(monkeypatch)
    body = client.post("/api/phone/devices", json={"name": "Pixel"}).json()
    device_id = body["device"]["id"]
    assert client.post(f"/api/phone/devices/{device_id}/revoke").json() == {"revoked": True}
    assert client.post(f"/api/phone/devices/{device_id}/revoke").json() == {"revoked": False}
    assert client.get("/api/phone/devices").json()[0]["revoked_at"] is not None
    assert tools.authenticate_device(body["token"]) is None


def test_test_message_with_the_gateway_down(client, monkeypatch):
    tailscale_off(monkeypatch)
    body = client.post("/api/phone/devices", json={"name": "Pixel"}).json()
    r = client.post("/api/phone/test", json={"token": body["token"]})
    assert r.status_code == 200
    assert r.json()["status"] is None and "not reachable" in r.json()["said"]


def test_test_message_passes_the_token_to_the_gateway(client, monkeypatch):
    seen = {}

    def fake(token, port):
        seen["token"], seen["port"] = token, port
        return 200, "ignored"

    monkeypatch.setattr(phone.sms_gateway, "send_test_sms", fake)
    r = client.post("/api/phone/test", json={"token": "rf_abc"})
    assert r.json() == {"status": 200, "said": "ignored"} and seen == {"token": "rf_abc", "port": 8765}
    assert client.post("/api/phone/test", json={}).status_code == 422


def test_inbox_list_and_dismiss(client, monkeypatch):
    tailscale_off(monkeypatch)
    device = tools.create_device("Pixel")
    item = tools._to_inbox(device["id"], "AX-BANK", "Rs.50 debited somewhere", datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),
                           "hash-a", "not sure")["inbox_id"]
    rows = client.get("/api/phone/inbox").json()
    assert len(rows) == 1 and rows[0]["id"] == item and rows[0]["raw_text"] == "Rs.50 debited somewhere"
    assert rows[0]["device"] == "Pixel" and rows[0]["reason"] == "not sure" and rows[0]["sent_at"].startswith("2026-10-20")
    assert client.post(f"/api/phone/inbox/{item}/dismiss").json() == {"dismissed": True}
    assert client.post(f"/api/phone/inbox/{item}/dismiss").json() == {"dismissed": False}
    assert client.get("/api/phone/inbox").json() == []
