"""Budgets, Accounts and the weekly Review API."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from api_support import make_client, m

from rectifi import tools

TODAY = date(2026, 10, 21)


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: TODAY)


@pytest.fixture
def client():
    return make_client()


# ───────────────────────── budgets ─────────────────────────

def test_budgets_empty_month(client):
    body = client.get("/api/budgets?month=2026-10").json()
    assert body["status"]["month"] == "2026-10" and body["status"]["categories"] == []
    assert body["status"]["total_limit"] == "0.00" and body["status"]["total_spent"] == "0.00"
    assert body["previous_month"] == "2026-09" and "Food & Dining" in body["expense_categories"]
    assert body["status"]["pace_day"] == 21 and body["status"]["days_in_month"] == 31


def test_set_a_limit_and_see_the_progress(client):
    tools.add_transaction("800", "debit", date(2026, 10, 5), payee="Swiggy", category="Food & Dining")
    r = client.put("/api/budgets", json={"month": "2026-10", "category": "Food & Dining", "limit": "1000"})
    assert r.status_code == 200
    row = r.json()["categories"][0]
    assert row["category"] == "Food & Dining" and row["limit"] == "1000.00" and row["spent"] == "800.00"
    assert row["remaining"] == "200.00" and row["percent"] == "80.0" and row["state"] == "near"


def test_a_null_limit_removes_it(client):
    tools.set_budget("Food & Dining", "500", "2026-10")
    r = client.put("/api/budgets", json={"month": "2026-10", "category": "Food & Dining", "limit": None})
    assert r.status_code == 200 and r.json()["categories"] == []


def test_budget_validation(client):
    assert client.put("/api/budgets", json={"month": "2026-10", "category": "Food & Dining", "limit": "abc"}).status_code == 422
    assert client.put("/api/budgets", json={"month": "2026-10", "category": "Nonsense", "limit": "5"}).status_code in (404, 422)
    assert client.put("/api/budgets", json={"month": "2026-10", "limit": "5"}).status_code == 422
    assert client.put("/api/budgets", json={"month": "2026-10", "category": "Food & Dining", "limit": 5}).status_code == 422   # a number, not text


def test_copy_budgets_forward_without_overwriting(client):
    tools.set_budget("Food & Dining", "500", "2026-09")
    tools.set_budget("Shopping", "700", "2026-09")
    tools.set_budget("Shopping", "900", "2026-10")
    assert client.post("/api/budgets/copy", json={"from": "2026-09", "to": "2026-10"}).json() == {"copied": 1}
    rows = {b["category"]: b["limit"] for b in tools.budget_status("2026-10")["categories"]}
    assert rows == {"Food & Dining": Decimal("500.00"), "Shopping": Decimal("900.00")}


# ───────────────────────── accounts ─────────────────────────

def test_accounts_empty(client):
    assert client.get("/api/accounts").json() == {"items": [], "default_account_id": None}


def test_add_account_and_list(client):
    r = client.post("/api/accounts", json={"name": "SBI Savings", "last4": "x 1234"})
    assert r.status_code == 200 and r.json()["account"]["last4"] == "1234"
    body = client.get("/api/accounts").json()
    a = body["items"][0]
    assert a["name"] == "SBI Savings" and a["transactions"] == 0 and a["reconcile"] is None and a["reason"]
    assert body["default_account_id"] == a["id"]


def test_add_account_needs_a_name(client):
    r = client.post("/api/accounts", json={"name": " "})
    assert r.status_code == 422 and "name" in r.json()["error"]["message"].lower()


def test_record_balance_and_the_check(client):
    acct = tools.add_account("Main", "1234")
    r = client.post(f"/api/accounts/{acct['id']}/balances", json={"as_of": "2026-10-01", "balance": "1000"})
    assert r.status_code == 200 and r.json()["balance"] == "1000.00" and r.json()["reconcile"] is None
    tools.add_transaction("100", "debit", date(2026, 10, 5), payee="Chai", category="Food & Dining", account="Main")
    client.post(f"/api/accounts/{acct['id']}/balances", json={"as_of": "2026-10-10", "balance": "900"})
    item = client.get("/api/accounts").json()["items"][0]
    rec = item["reconcile"]
    assert rec["balanced"] is True and rec["gap"] == "0.00" and rec["debits"] == "100.00" and rec["expected_closing"] == "900.00"
    assert item["last_snapshot"] == "2026-10-10"


def test_a_gap_is_shown_not_hidden(client):
    acct = tools.add_account("Main", "1234")
    client.post(f"/api/accounts/{acct['id']}/balances", json={"as_of": "2026-10-01", "balance": "1000"})
    client.post(f"/api/accounts/{acct['id']}/balances", json={"as_of": "2026-10-10", "balance": "800"})
    rec = client.get("/api/accounts").json()["items"][0]["reconcile"]
    assert rec["balanced"] is False and rec["gap"] == "-200.00"


def test_record_balance_validation(client):
    acct = tools.add_account("Main", "1234")
    url = f"/api/accounts/{acct['id']}/balances"
    assert client.post(url, json={"as_of": "2026-10-01"}).status_code == 422
    assert client.post(url, json={"as_of": "10/01/2026", "balance": "5"}).status_code == 422
    assert client.post(url, json={"as_of": "2026-10-01", "balance": "lots"}).status_code == 422
    assert client.post("/api/accounts/9999/balances", json={"as_of": "2026-10-01", "balance": "5"}).status_code == 404
    assert client.post("/api/accounts/abc/balances", json={"as_of": "2026-10-01", "balance": "5"}).status_code == 404


# ───────────────────────── review ─────────────────────────

def test_review_empty_week(client):
    body = client.get("/api/review").json()
    assert body["week_start"] == "2026-10-19" and body["week_end"] == "2026-10-25" and body["is_current"] is True
    assert body["spending"] == "0.00" and body["count"] == 0 and body["act_insights"] == [] and body["insights_error"] is False
    assert body["commitment"] is None and body["spending_change_pct"] is None


def test_review_numbers_match_tools(client):
    tools.add_account("Main", "1234")
    tools.add_transaction("250", "debit", date(2026, 10, 20), payee="Swiggy", category="Food & Dining", account="Main")
    tools.add_transaction("100", "debit", date(2026, 10, 14), payee="Chai", category="Food & Dining", account="Main")
    body = client.get("/api/review").json()
    r = tools.weekly_review()
    assert body["spending"] == m(r["spending"]) == "250.00" and body["prev_spending"] == "100.00"
    assert body["top_categories"][0]["category"] == "Food & Dining" and body["top_categories"][0]["share"] == "100.0"
    assert body["biggest_debits"][0]["payee"] == "Swiggy"
    last = client.get("/api/review?week=2026-10-14").json()
    assert last["week_start"] == "2026-10-12" and last["is_current"] is False and last["spending"] == "100.00"


def test_review_week_validation(client):
    assert client.get("/api/review?week=banana").status_code == 422


def test_review_survives_a_broken_insight_rule(client, monkeypatch):
    from rectifi.api.routes import review as route

    def broken():
        raise RuntimeError("rule blew up")

    monkeypatch.setattr(route.insights, "act_insights", broken)
    body = client.get("/api/review").json()
    assert body["insights_error"] is True and body["act_insights"] == [] and "week_start" in body


def test_commitment_saved_and_shown(client):
    r = client.put("/api/review/commitment", json={"text": "  No   food delivery on weekdays "})
    assert r.status_code == 200 and r.json()["commitment"] == "No food delivery on weekdays" and r.json()["week_start"] == "2026-10-19"
    assert client.get("/api/review").json()["commitment"] == "No food delivery on weekdays"
    client.put("/api/review/commitment", json={"text": "Cook twice", "week": "2026-10-12"})
    nxt = client.get("/api/review?week=2026-10-14").json()
    assert nxt["commitment"] == "Cook twice"
    assert client.get("/api/review").json()["last_commitment"] is not None


def test_commitment_validation(client):
    assert client.put("/api/review/commitment", json={"text": " "}).status_code == 422
    r = client.put("/api/review/commitment", json={"text": "x" * 281})
    assert r.status_code == 422 and "280" in r.json()["error"]["message"]
    assert client.put("/api/review/commitment", json={"text": "x" * 2000}).status_code == 422
