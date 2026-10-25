"""Ledger and Add over HTTP: list filters, add, duplicate, inbox add, edit (category, note, account), delete, paste SMS."""

from datetime import date

import pytest
from api_support import m, make_client

from rectifi import tools

TODAY = date(2026, 10, 21)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: TODAY)


@pytest.fixture
def client():
    return make_client()


def post(client, amount="250", **kw):
    body = {"amount": amount, "direction": "debit", "date": "2026-10-20", "payee": "Swiggy", **kw}
    return client.post("/api/transactions", json=body)


def test_add_a_transaction(client):
    acct = tools.add_account("SBI Savings", "1234")["id"]
    r = post(client, "250.5", account_id=acct, note="lunch")
    assert r.status_code == 200
    body = r.json()
    t = body["transaction"]
    assert body["status"] == "added" and t["amount"] == "250.50" and t["payee"] == "Swiggy" and t["category"] == "Food & Dining"
    assert t["account_id"] == acct and t["account"] == "SBI Savings" and t["note"] == "lunch" and t["source"] == "manual"
    assert t["date"] == "2026-10-20" and "upi_ref" not in t
    stored = tools.get_transaction(t["id"])
    assert str(stored["amount"]) == "250.50"


def test_add_validation_errors_are_inline_and_plain(client):
    r = post(client, "abc")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid" and "look like an amount" in r.json()["error"]["message"] and r.json()["error"]["field"] == "amount"
    assert post(client, "0").status_code == 422
    r = client.post("/api/transactions", json={"direction": "debit"})
    assert r.status_code == 422 and r.json()["error"]["field"] == "amount"
    r = client.post("/api/transactions", json={"amount": 250, "direction": "debit"})                  # a number, not a string
    assert r.status_code == 422 and "text" in r.json()["error"]["message"]
    r = post(client, date="2027-01-01")
    assert r.status_code == 422 and "future" in r.json()["error"]["message"]
    r = post(client, date="21/10/2026")
    assert r.status_code == 422 and r.json()["error"]["field"] == "date"
    r = post(client, category="Salary & Allowance")                                                    # income category on money out
    assert r.status_code == 422 and "income category" in r.json()["error"]["message"]
    assert post(client, account_id=999).status_code == 422
    assert tools.list_transactions() == []


def test_wrong_content_type_and_bad_json(client):
    assert client.post("/api/transactions", content=b"amount=250", headers={"Content-Type": "text/plain"}).status_code == 415
    r = client.post("/api/transactions", content=b"{nope", headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid"
    assert client.post("/api/transactions", content=b"[1]", headers={"Content-Type": "application/json"}).status_code == 422


def test_chunked_body_over_the_limit_is_413_while_it_streams(client):
    def chunks():
        for _ in range(80):
            yield b"x" * 1024
    r = client.post("/api/transactions", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"


def test_duplicate_upi_reference_is_reported_not_added(client):
    tools.add_transaction("250", "debit", "2026-10-20", payee="Swiggy", upi_ref="429183746521")
    # the browser can't send a upi_ref, so use the SMS route, which parses one
    sms = "Rs.250.00 debited from A/c XX1234 on 20-10-26 to VPA swiggy@icici UPI Ref No 429183746521"
    r = client.post("/api/sms/save", json={"text": sms})
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert len(tools.list_transactions()) == 1


def test_inbox_add_takes_sms_and_reference_from_the_database_not_the_browser(client):
    tools.add_account("SBI Savings", "1234")
    device = tools.create_device("My Android")
    sms = "Rs.640.00 debited from A/c XX1234 on 20-10-26 to Mystery Shop UPI Ref No 429183746599"
    item_id = tools._to_inbox(device["id"], "AX-BANK", sms, None, "hash-1", "test")["inbox_id"]
    prefill = client.get(f"/api/inbox/{item_id}/prefill").json()
    assert prefill["inbox_id"] == item_id and prefill["raw_text"] == sms and prefill["amount"] == "640.00"
    assert prefill["upi_ref"] == "429183746599" and prefill["direction"] == "debit"
    r = post(client, "640", inbox_id=item_id, payee="Mystery Shop", upi_ref="000000000000", raw_text="forged")
    assert r.status_code == 200
    stored = next(t for t in tools.list_transactions() if t["payee"] == "Mystery Shop")
    assert stored["source"] == "sms"
    with_raw = tools.get_transaction(stored["id"])
    assert with_raw["upi_ref"] == "429183746599"                      # from the database, not the forged field
    assert tools.list_inbox("pending") == [] and len(tools.list_inbox("added")) == 1


def test_inbox_prefill_unknown_item_is_422(client):
    assert client.get("/api/inbox/999/prefill").status_code == 422


def test_patch_category_note_and_account(client):
    acct = tools.add_account("SBI Savings", "1234")["id"]
    t = post(client).json()["transaction"]
    r = client.patch(f"/api/transactions/{t['id']}", json={"category": "Shopping"})
    assert r.status_code == 200
    body = r.json()
    assert body["transaction"]["category"] == "Shopping" and body["learned_rule"] == "swiggy"
    r = client.patch(f"/api/transactions/{t['id']}", json={"note": "  team lunch "})
    assert r.json()["transaction"]["note"] == "team lunch"
    r = client.patch(f"/api/transactions/{t['id']}", json={"account_id": acct})
    assert r.json()["transaction"]["account_id"] == acct
    r = client.patch(f"/api/transactions/{t['id']}", json={"account_id": None, "note": ""})
    assert r.json()["transaction"]["account_id"] is None and r.json()["transaction"]["note"] is None
    assert tools.get_transaction(t["id"])["account_id"] is None


def test_patch_category_kind_mismatch_is_422_with_the_message(client):
    t = post(client).json()["transaction"]
    r = client.patch(f"/api/transactions/{t['id']}", json={"category": "Salary & Allowance"})
    assert r.status_code == 422
    assert "income category" in r.json()["error"]["message"] and "coming in" not in r.json()["error"]["message"].split("this money is")[0]
    assert tools.get_transaction(t["id"])["category"] == "Food & Dining"


def test_patch_refuses_to_move_a_statement_row_and_unknown_things(client):
    acct = tools.add_account("SBI Savings", "1234")["id"]
    other = tools.add_account("HDFC", "5678")["id"]
    row = tools.add_transaction("100", "debit", "2026-10-02", payee="X", account=acct, source="statement")["transaction"]
    r = client.patch(f"/api/transactions/{row['id']}", json={"account_id": other})
    assert r.status_code == 422 and "came from a bank statement" in r.json()["error"]["message"]
    assert client.patch("/api/transactions/9999", json={"note": "x"}).status_code == 404
    assert client.patch(f"/api/transactions/{row['id']}", json={}).status_code == 422
    assert client.patch(f"/api/transactions/{row['id']}", json={"account_id": 4242}).status_code == 422
    assert client.patch("/api/transactions/abc", json={"note": "x"}).status_code == 404


def test_delete(client):
    t = post(client).json()["transaction"]
    assert client.delete(f"/api/transactions/{t['id']}").json() == {"deleted": True}
    assert client.delete(f"/api/transactions/{t['id']}").json() == {"deleted": False}
    assert tools.list_transactions() == []


def seed_ledger():
    acct = tools.add_account("SBI Savings", "1234")["id"]
    other = tools.add_account("HDFC", "5678")["id"]
    tools.add_transaction("100", "debit", "2026-10-02", payee="Swiggy", account=acct)
    tools.add_transaction("200", "debit", "2026-10-03", payee="Amazon", account=other)
    tools.add_transaction("300", "debit", "2026-09-04", payee="Corner shop")
    tools.add_transaction("5000", "credit", "2026-09-01", payee="Stipend", category="Salary & Allowance", note="october advance")
    return acct, other


def test_list_filters(client):
    acct, other = seed_ledger()
    everything = client.get("/api/transactions").json()
    assert len(everything["items"]) == 4 and everything["truncated"] is False and everything["limit"] == 500
    assert [t["payee"] for t in everything["items"]] == ["Amazon", "Swiggy", "Corner shop", "Stipend"]      # newest first
    assert [t["payee"] for t in client.get("/api/transactions?period=2026-10").json()["items"]] == ["Amazon", "Swiggy"]
    assert [t["payee"] for t in client.get("/api/transactions?category=Shopping").json()["items"]] == ["Amazon"]
    assert [t["payee"] for t in client.get(f"/api/transactions?account={acct}").json()["items"]] == ["Swiggy"]
    assert [t["payee"] for t in client.get("/api/transactions?account=none").json()["items"]] == ["Corner shop", "Stipend"]
    assert [t["payee"] for t in client.get("/api/transactions?search=advance").json()["items"]] == ["Stipend"]
    assert [t["payee"] for t in client.get("/api/transactions?period=2026-09&account=none&category=Other").json()["items"]] == ["Corner shop"]
    assert client.get("/api/transactions?category=Nonsense").status_code == 422
    assert client.get("/api/transactions?account=abc").status_code == 422
    assert client.get("/api/transactions?period=soon").status_code == 422


def test_list_truncation(client):
    for day in range(1, 8):
        tools.add_transaction("10", "debit", f"2026-10-{day:02d}", payee="Chai")
    r = client.get("/api/transactions?limit=5").json()
    assert len(r["items"]) == 5 and r["truncated"] is True and r["limit"] == 5
    r = client.get("/api/transactions?limit=7").json()
    assert len(r["items"]) == 7 and r["truncated"] is False
    assert client.get("/api/transactions?limit=99999").json()["limit"] == 2000
    assert client.get("/api/transactions?limit=0").status_code == 422


SMS = "Rs.250.00 debited from A/c XX1234 on 20-10-26 to VPA swiggy@icici UPI Ref No 429183746521"


def test_sms_preview_and_save(client):
    tools.add_account("SBI Savings", "1234")
    parsed = client.post("/api/sms/preview", json={"text": SMS}).json()["parsed"]
    assert parsed["amount"] == "250.00" and parsed["direction"] == "debit" and parsed["txn_date"] == "2026-10-20"
    assert parsed["upi_ref"] == "429183746521" and parsed["account"]["name"] == "SBI Savings" and parsed["warnings"] == []
    assert tools.list_transactions() == []
    saved = client.post("/api/sms/save", json={"text": SMS}).json()
    assert saved["status"] == "added" and saved["transaction"]["category"] == "Food & Dining" and saved["transaction"]["source"] == "sms"
    assert saved["transaction"]["account"] == "SBI Savings"


def test_sms_non_transaction(client):
    assert client.post("/api/sms/preview", json={"text": "Your OTP is 123456"}).json() == {"parsed": None}
    r = client.post("/api/sms/save", json={"text": "hello"})
    assert r.status_code == 422 and "doesn't look like a transaction SMS" in r.json()["error"]["message"]
    assert client.post("/api/sms/preview", json={"text": "x" * 4001}).status_code == 422
    assert client.post("/api/sms/preview", json={}).status_code == 422


def test_a_bad_amount_names_the_amount_field(client):
    for bad in ("abc", "12x4", "--5"):
        r = client.post("/api/transactions", json={"amount": bad, "direction": "debit"})
        assert r.status_code == 422 and r.json()["error"]["field"] == "amount", bad
    assert tools.list_transactions() == []
    assert client.post("/api/transactions", json={"amount": "1,250.50", "direction": "debit"}).status_code == 200
