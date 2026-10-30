"""P4: the SMS gateway starts the alert sweep after a stored payment, and nothing about alerts can break the phone's answer."""

import subprocess
import sys

import pytest
from alerts_support import Clock, FakePush, TODAY, add_phone, rows
from starlette.testclient import TestClient

from rectifi import alerts, sms_gateway, tools, webpush

SWIGGY_SMS = "Rs.850.00 debited from A/c XX1234 on 21-10-26 to VPA swiggy@icici UPI Ref No 429183746521"
SWIGGY_OLD = "Rs.850.00 debited from A/c XX1234 on 16-10-26 to VPA swiggy@icici UPI Ref No 429183746599"


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: TODAY)
    monkeypatch.setattr(alerts, "_clock", Clock())
    push = FakePush()
    monkeypatch.setattr(webpush, "send", push)
    tools.add_account("Main", "1234")
    add_phone()
    alerts.create_rule("merchant", "Swiggy", "week", 1000)
    alerts.sweep("baseline")
    return push


@pytest.fixture
def client():
    with TestClient(sms_gateway.create_app()) as c:
        yield c


@pytest.fixture
def token():
    return tools.create_device("Test Android")["token"]


def post(client, token, text):
    return client.post("/sms", headers={"Authorization": f"Bearer {token}"}, json={"from": "AX-HDFCBK", "text": text})


def test_an_sms_today_leads_to_one_pushed_alert(client, token, world):
    reply = post(client, token, SWIGGY_SMS)
    assert reply.status_code == 200 and reply.json() == {"status": "added"}          # the answer is unchanged
    (row,) = rows()
    assert row["push_state"] == "sent" and row["dedupe_key"].endswith(":80") and len(world.calls) == 1


def test_a_duplicate_sms_makes_no_second_alert(client, token, world):
    post(client, token, SWIGGY_SMS)
    assert post(client, token, SWIGGY_SMS).json() == {"status": "duplicate"}
    assert len(rows()) == 1 and len(world.calls) == 1


def test_a_late_sms_becomes_a_bulk_summary(client, token, world):
    assert post(client, token, SWIGGY_OLD).json() == {"status": "added"}
    keys = [r["dedupe_key"] for r in rows()]
    assert any(k.startswith("bulk:") for k in keys) and len(world.calls) == 1


def test_a_push_that_raises_still_answers_200_added(client, token, world):
    world.answer = RuntimeError("push service exploded")
    reply = post(client, token, SWIGGY_SMS)
    assert reply.status_code == 200 and reply.json() == {"status": "added"}
    assert tools.list_transactions()                                                    # the payment is stored


def test_a_broken_sweep_still_answers_200(client, token, monkeypatch):
    monkeypatch.setattr(alerts, "sweep", lambda origin: (_ for _ in ()).throw(RuntimeError("db hiccup")))
    reply = post(client, token, SWIGGY_SMS)
    assert reply.status_code == 200 and reply.json() == {"status": "added"}


def test_ignored_sms_does_not_sweep(client, token, monkeypatch):
    called = []
    monkeypatch.setattr(alerts, "sweep_and_deliver", lambda origin: called.append(origin))
    assert post(client, token, "123456 is your OTP for login. Do not share it.").json() == {"status": "ignored"}
    assert called == []


def test_health_is_unchanged(client):
    assert client.get("/health").json() == {"ok": True}


def test_gateway_still_loads_no_langchain():
    code = ("import sys, rectifi.sms_gateway; "
            "bad = [m for m in sys.modules if m.split('.')[0].startswith(('langchain', 'langgraph')) or m.startswith('rectifi.agent')]; "
            "print(bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout
