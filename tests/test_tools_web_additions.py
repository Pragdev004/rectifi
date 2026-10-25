"""The additive backend pieces the web front end needs: set_account, list_transactions(unlinked), spending_pace,
preview/save_pasted_sms, importer.preview_totals and graph.chat_events."""

from datetime import date
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from rectifi import importer, tools
from rectifi.agent.graph import build_graph, chat_events
from rectifi.tools import RectifiError

from test_agent import ScriptedModel, call

TODAY = date(2026, 10, 21)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: TODAY)


def add(amount="100", direction="debit", day="2026-10-10", payee="Swiggy", account=None, source="manual", **kw):
    return tools.add_transaction(amount, direction, day, payee=payee, account=account, source=source, **kw)["transaction"]


# ───────────────────────── set_account ─────────────────────────

def test_set_account_links_and_unlinks():
    acct = tools.add_account("SBI Savings", "1234")
    t = add()
    assert t["account_id"] is None
    linked = tools.set_account(t["id"], acct["id"])
    assert linked["account_id"] == acct["id"] and linked["account"] == "SBI Savings"
    assert tools.get_transaction(t["id"])["account_id"] == acct["id"]
    unlinked = tools.set_account(t["id"], None)
    assert unlinked["account_id"] is None and unlinked["account"] is None


def test_set_account_unknown_account_and_missing_transaction():
    t = add()
    with pytest.raises(RectifiError, match="No account #999"):
        tools.set_account(t["id"], 999)
    with pytest.raises(RectifiError, match="No transaction #999"):
        tools.set_account(999, None)
    assert tools.get_transaction(t["id"])["account_id"] is None


def test_set_account_refuses_a_statement_row():
    acct = tools.add_account("SBI Savings", "1234")
    other = tools.add_account("HDFC", "9999")
    t = add(account=acct["id"], source="statement")
    with pytest.raises(RectifiError, match="came from a bank statement"):
        tools.set_account(t["id"], other["id"])
    with pytest.raises(RectifiError, match="came from a bank statement"):
        tools.set_account(t["id"], None)
    assert tools.get_transaction(t["id"])["account_id"] == acct["id"]


# ───────────────────────── list_transactions(unlinked=True) ─────────────────────────

def test_list_transactions_unlinked_only():
    acct = tools.add_account("SBI Savings", "1234")
    add(payee="Linked", account=acct["id"])
    add(payee="Loose one")
    add(payee="Loose two")
    names = {t["payee"] for t in tools.list_transactions(unlinked=True)}
    assert names == {"Loose one", "Loose two"}
    assert len(tools.list_transactions()) == 3                 # the default is unchanged
    assert len(tools.list_transactions(unlinked=False)) == 3


# ───────────────────────── spending_pace ─────────────────────────

def test_spending_pace_is_decimal_and_follows_the_budget():
    tools.set_budget("Food & Dining", "3100", "2026-10")
    add("200", day="2026-10-02")
    add("300", day="2026-10-03")
    pace = tools.spending_pace("2026-10")
    assert pace["through"] == TODAY and pace["total_limit"] == Decimal("3100.00")
    assert len(pace["days"]) == 21
    day1, day2, day3, last = pace["days"][0], pace["days"][1], pace["days"][2], pace["days"][-1]
    assert day1["pace"] == Decimal("100.00")                    # 3100 / 31
    assert day3["pace"] == Decimal("300.00")
    assert [day1["cumulative"], day2["cumulative"], day3["cumulative"]] == [Decimal("0.00"), Decimal("200.00"), Decimal("500.00")]
    assert last["cumulative"] == Decimal("500.00") and last["pace"] == Decimal("2100.00")
    for row in pace["days"]:
        assert all(isinstance(row[k], Decimal) for k in ("spent", "cumulative", "pace"))


def test_spending_pace_ends_at_the_total_limit_for_a_past_month():
    tools.set_budget("Food & Dining", "1000", "2026-09")
    tools.set_budget("Shopping", "333.33", "2026-09")
    pace = tools.spending_pace("2026-09")
    assert pace["through"] == date(2026, 9, 30) and len(pace["days"]) == 30
    assert pace["days"][-1]["pace"] == pace["total_limit"] == Decimal("1333.33")


def test_spending_pace_empty_for_a_future_month_and_zero_without_budgets():
    assert tools.spending_pace("2026-11")["days"] == []
    plain = tools.spending_pace("2026-10")
    assert plain["total_limit"] == Decimal("0.00") and all(d["pace"] == Decimal("0.00") for d in plain["days"])


# ───────────────────────── pasted SMS ─────────────────────────

SMS = "Rs.250.00 debited from A/c XX1234 on 21-10-26 to VPA swiggy@icici UPI Ref No 429183746521"


def test_preview_pasted_sms_matches_the_account_by_digits():
    tools.add_account("SBI Savings", "1234")
    tools.add_account("HDFC", "5678")
    p = tools.preview_pasted_sms(SMS)
    assert p["amount"] == Decimal("250.00") and p["direction"] == "debit" and p["upi_ref"] == "429183746521"
    assert p["txn_date"] == date(2026, 10, 21) and p["payee"] == "swiggy@icici"
    assert p["account"]["name"] == "SBI Savings"
    assert tools.list_transactions() == []                      # a preview writes nothing


def test_preview_pasted_sms_unknown_digits_and_not_an_sms():
    tools.add_account("SBI Savings", "9999")
    tools.add_account("HDFC", "5678")
    assert tools.preview_pasted_sms(SMS)["account"] is None     # names digits nobody has: never guessed
    assert tools.preview_pasted_sms("Your OTP is 123456") is None
    assert tools.preview_pasted_sms("hello there") is None
    assert tools.preview_pasted_sms("") is None


def test_save_pasted_sms_and_duplicate_ref():
    acct = tools.add_account("SBI Savings", "1234")
    first = tools.save_pasted_sms(SMS)
    assert first["status"] == "added"
    t = first["transaction"]
    assert t["account_id"] == acct["id"] and t["source"] == "sms" and t["category"] == "Food & Dining"
    again = tools.save_pasted_sms(SMS)
    assert again["status"] == "duplicate"
    assert len(tools.list_transactions()) == 1


def test_save_pasted_sms_single_account_without_digits_attaches():
    acct = tools.add_account("Only one")                        # no last 4 saved
    saved = tools.save_pasted_sms(SMS)["transaction"]
    assert saved["account_id"] == acct["id"]                    # the same rule the gateway uses


def test_save_pasted_sms_refuses_a_non_transaction():
    with pytest.raises(RectifiError, match="doesn't look like a transaction SMS"):
        tools.save_pasted_sms("Your OTP is 123456")


# ───────────────────────── importer.preview_totals ─────────────────────────

def _row(day, amount, direction):
    return importer.StatementRow(date.fromisoformat(day), Decimal(amount), direction, "x", None, None, 1)


def test_preview_totals():
    rows = [_row("2026-10-01", "100.50", "debit"), _row("2026-10-03", "40.25", "debit"), _row("2026-10-09", "9000", "credit")]
    t = importer.preview_totals(rows)
    assert t == {"count": 3, "money_out": Decimal("140.75"), "money_in": Decimal("9000.00"),
                 "first": date(2026, 10, 1), "last": date(2026, 10, 9)}
    empty = importer.preview_totals([])
    assert empty["count"] == 0 and empty["first"] is None and empty["money_out"] == Decimal("0.00")


# ───────────────────────── graph.chat_events ─────────────────────────

def test_chat_events_yields_each_tool_then_done():
    model = ScriptedModel(script=[
        call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}),
        AIMessage("Saved ₹250 at Swiggy."),
    ])
    events = list(chat_events(build_graph(model), "spent 250 on swiggy", "ev1"))
    kinds = [k for k, _ in events]
    assert kinds == ["tool", "done"]
    assert isinstance(events[0][1], ToolMessage) and '"added"' in events[0][1].content
    done = events[-1][1]
    assert [type(m).__name__ for m in done] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert len(tools.list_transactions()) == 1


def test_chat_events_guard_still_refuses_an_invented_amount():
    model = ScriptedModel(script=[
        call("log_transaction", {"amount": "2500", "direction": "debit", "payee": "Swiggy"}),   # the user typed 250
        AIMessage("Sorry, how much exactly?"),
    ])
    events = list(chat_events(build_graph(model), "spent 250 on swiggy", "ev2"))
    tool_events = [m for k, m in events if k == "tool"]
    assert len(tool_events) == 1 and tool_events[0].content.startswith("GUARD") and tool_events[0].status == "error"
    assert tools.list_transactions() == []


def test_chat_events_returns_only_this_turns_messages():
    model = ScriptedModel(script=[AIMessage("Hi."), AIMessage("Hello again.")])
    graph = build_graph(model)
    list(chat_events(graph, "hello", "ev3"))
    events = list(chat_events(graph, "hello again", "ev3"))
    assert events == [("done", events[0][1])] and [m.content for m in events[0][1]] == ["hello again", "Hello again."]
