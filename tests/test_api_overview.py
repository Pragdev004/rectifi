"""GET /api/meta and /api/overview: every money string equals what tools.* computed, in Decimal."""

from datetime import date
from decimal import Decimal

import pytest
from api_support import m, make_client, p

from rectifi import insights, tools

TODAY = date(2026, 10, 21)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: TODAY)


@pytest.fixture
def client():
    return make_client()


def seed():
    acct = tools.add_account("SBI Savings", "1234")["id"]
    tools.record_balance(acct, "2026-09-30", "10000")
    tools.record_balance(acct, "2026-10-20", "16799.50")
    add = lambda *a, **k: tools.add_transaction(*a, account=acct, source="manual", **k)   # noqa: E731
    add("9000", "credit", "2026-10-01", payee="Stipend", category="Salary & Allowance")
    add("250.50", "debit", "2026-10-03", payee="Swiggy")
    add("1400", "debit", "2026-10-05", payee="Amazon")
    add("99.99", "debit", "2026-10-09", payee="Uber")
    add("1200", "debit", "2026-09-12", payee="Swiggy")
    add("4000", "credit", "2026-09-01", payee="Stipend", category="Salary & Allowance")
    tools.add_transaction("75", "debit", "2026-10-11", payee="Corner shop", source="manual")      # unlinked
    tools.set_budget("Food & Dining", "3000", "2026-10")
    tools.set_budget("Shopping", "1000", "2026-10")
    return acct


def test_meta(client):
    seed()
    body = client.get("/api/meta").json()
    assert body["today"] == "2026-10-21"
    assert body["months"][0] == "2026-10" and body["months"][1] == "2026-09" and len(body["months"]) == 12
    assert body["months"][-1] == "2025-11"
    assert body["weeks"][0] == "2026-10-19" and len(body["weeks"]) == 12 and body["weeks"][1] == "2026-10-12"
    assert {"id", "name", "kind"} <= set(body["categories"][0])
    assert body["accounts"] == [{"id": body["default_account_id"], "name": "SBI Savings", "last4": "1234"}]


def test_meta_without_accounts(client):
    assert client.get("/api/meta").json()["default_account_id"] is None


@pytest.mark.parametrize("month", ["2026-10", "2026-09", "2026-08", "2026-11"])
def test_overview_money_equals_the_tools_output(client, month):
    seed()
    body = client.get(f"/api/overview?month={month}").json()
    first, last = tools.month_bounds(month)
    s = tools.spending_summary(first, last)
    assert body["month"] == month and body["start"] == first.isoformat() and body["end"] == last.isoformat()
    for k in ("income", "spending", "saved"):
        assert body["summary"][k] == m(s[k]), k
    assert body["summary"]["count"] == s["count"] and body["summary"]["savings_rate"] == p(s["savings_rate"])
    assert [(c["category"], c["amount"], c["count"], c["share"]) for c in body["summary"]["by_category"]] == [
        (c["category"], m(c["amount"]), c["count"], p(c["share"])) for c in s["by_category"]]

    b = tools.budget_status(month)
    assert body["budgets"]["total_limit"] == m(b["total_limit"]) and body["budgets"]["total_spent"] == m(b["total_spent"])
    assert body["budgets"]["pace"] == p(b["pace"])
    assert [(c["category"], c["limit"], c["spent"], c["remaining"], c["percent"], c["state"]) for c in body["budgets"]["categories"]] == [
        (c["category"], m(c["limit"]), m(c["spent"]), m(c["remaining"]), p(c["percent"]), c["state"]) for c in b["categories"]]

    pace = tools.spending_pace(month)
    assert body["pace"]["through"] == pace["through"].isoformat() and body["pace"]["total_limit"] == m(pace["total_limit"])
    assert [(d["day"], d["spent"], d["cumulative"], d["pace"]) for d in body["pace"]["days"]] == [
        (d["day"].isoformat(), m(d["spent"]), m(d["cumulative"]), m(d["pace"])) for d in pace["days"]]

    recent = tools.list_transactions(first, last, limit=8)
    assert [(t["id"], t["amount"]) for t in body["recent"]] == [(t["id"], m(t["amount"])) for t in recent]
    assert all("upi_ref" not in t and "raw_text" not in t for t in body["recent"])


def test_overview_proofs_carry_reconcile_money(client):
    acct = seed()
    body = client.get("/api/overview?month=2026-10").json()
    r = tools.reconcile(acct)
    proof = body["proofs"][0]
    assert proof["account"]["name"] == "SBI Savings" and proof["reason"] is None
    got = proof["reconcile"]
    for k in ("opening", "credits", "debits", "expected_closing", "closing", "gap"):
        assert got[k] == m(r[k]), k
    assert got["balanced"] is r["balanced"] and got["opening_date"] == "2026-09-30" and got["closing_date"] == "2026-10-20"


def test_overview_proof_reason_when_an_account_cannot_reconcile(client):
    tools.add_account("Wallet")
    body = client.get("/api/overview").json()
    assert body["proofs"][0]["reconcile"] is None and "two balances" in body["proofs"][0]["reason"]


def test_money_strings_always_have_two_decimals_even_when_zero(client):
    body = client.get("/api/overview?month=2026-10").json()           # nothing recorded at all
    assert body["summary"]["income"] == "0.00" and body["summary"]["spending"] == "0.00" and body["summary"]["saved"] == "0.00"
    assert body["summary"]["savings_rate"] is None and body["summary"]["count"] == 0
    assert body["budgets"]["total_limit"] == "0.00" and body["recent"] == [] and body["proofs"] == []


def test_income_only_month(client):
    tools.add_transaction("5000", "credit", "2026-10-02", payee="Stipend", category="Salary & Allowance")
    body = client.get("/api/overview?month=2026-10").json()
    assert body["summary"]["income"] == "5000.00" and body["summary"]["spending"] == "0.00"
    assert body["summary"]["saved"] == "5000.00" and body["summary"]["savings_rate"] == "100.0"
    assert body["summary"]["by_category"] == []


def test_negative_saved_uses_an_ascii_minus(client):
    tools.add_transaction("100", "credit", "2026-10-02", payee="Stipend", category="Salary & Allowance")
    tools.add_transaction("350.5", "debit", "2026-10-03", payee="Amazon")
    body = client.get("/api/overview?month=2026-10").json()
    assert body["summary"]["saved"] == "-250.50" and body["summary"]["savings_rate"] == "-250.5"


def test_insights_failure_gives_200_with_the_flag(client, monkeypatch):
    seed()

    def boom(limit=3, today=None):
        raise ValueError("a rule broke")

    monkeypatch.setattr(insights, "top_insights", boom)
    r = client.get("/api/overview?month=2026-10")
    assert r.status_code == 200
    body = r.json()
    assert body["insights_error"] is True and body["top_insights"] is None and body["summary"]["count"] > 0


def test_insights_are_shaped_for_the_browser(client):
    seed()
    body = client.get("/api/overview?month=2026-10").json()
    assert body["insights_error"] is False and body["top_insights"]
    first = body["top_insights"][0]
    assert set(first) == {"key", "kind", "severity", "title", "detail", "numbers", "action_label", "action_page",
                          "is_data_quality", "can_apply", "link"}
    unlinked = next(i for i in body["top_insights"] if i["kind"] == "unlinked")
    assert unlinked["link"] == {"page": "ledger", "params": {"account": "none", "period": "all"}} and unlinked["is_data_quality"] is True
    assert all(isinstance(v, str) for i in body["top_insights"] for v in i["numbers"].values())


def test_budget_status_tick_position(client):
    seed()
    b = client.get("/api/overview?month=2026-10").json()["budgets"]
    assert (b["pace_day"], b["days_in_month"]) == (21, 31) and b["month"] == "2026-10"
    past = client.get("/api/overview?month=2026-09").json()["budgets"]
    assert (past["pace_day"], past["days_in_month"]) == (30, 30)
    future = client.get("/api/overview?month=2026-11").json()["budgets"]
    assert future["pace_day"] == 0 and Decimal(future["pace"]) == 0
