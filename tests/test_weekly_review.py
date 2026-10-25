from datetime import date
from decimal import Decimal

import pytest

from rectifi import tools
from rectifi.tools import RectifiError

TODAY = date(2026, 10, 21)   # a Wednesday: this week is Mon 19 Oct to Sun 25 Oct


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: TODAY)


def add(amount, direction, day, payee=None, category=None, account=None):
    return tools.add_transaction(amount, direction, day, payee=payee, category=category, account=account, source="manual")


def seed_two_weeks():
    # last week, Mon 12 to Sun 18 Oct
    add("400", "debit", "2026-10-12", "Swiggy")
    add("600", "debit", "2026-10-14", "Swiggy")
    add("900", "debit", "2026-10-16", "Swiggy")      # after the same-weekday cut-off, must not count
    # this week so far, Mon 19 to Wed 21
    add("5000", "credit", "2026-10-19", "Stipend", "Salary & Allowance")
    add("700", "debit", "2026-10-19", "Swiggy")
    add("1500", "debit", "2026-10-20", "Amazon")
    add("300", "debit", "2026-10-21", "Corner shop")  # no rule matches: lands in "Miscellaneous"


def test_weekly_review():
    seed_two_weeks()
    tools.set_budget("Food & Dining", "3000", "2026-10")     # the MONTH so far is 2600 of 3000 = 86.7% -> near
    tools.set_budget("Shopping", "1000", "2026-10")          # 1500 of 1000        -> over
    tools.set_budget("Transport", "5000", "2026-10")         # nothing spent       -> fine, not listed
    tools.add_account("Savings", "1111")                     # one balance only    -> can't prove completeness
    tools.record_balance("Savings", "2026-10-19", "1000")
    tools.add_account("Wallet", "2222")                      # bank balance higher than the records say -> gap
    tools.record_balance("Wallet", "2026-10-19", "1000")
    tools.record_balance("Wallet", "2026-10-21", "1200")
    tools.add_account("Current", "3333")                     # balanced -> not listed
    tools.record_balance("Current", "2026-10-19", "1000")
    tools.record_balance("Current", "2026-10-21", "1000")

    r = tools.weekly_review()

    assert (r["week_start"], r["week_end"], r["through"]) == (date(2026, 10, 19), date(2026, 10, 25), TODAY)
    assert r["is_current"] and r["days_elapsed"] == 3
    assert r["income"] == Decimal("5000.00")
    assert r["spending"] == Decimal("2500.00")
    assert r["saved"] == Decimal("2500.00")
    assert r["savings_rate"] == Decimal("50.0")

    # like for like: last Mon 12 to Wed 14 = 400 + 600, not the whole week
    assert (r["prev_start"], r["prev_end"]) == (date(2026, 10, 12), date(2026, 10, 14))
    assert r["prev_spending"] == Decimal("1000.00")
    assert r["spending_change"] == Decimal("1500.00")
    assert r["spending_change_pct"] == Decimal("150.0")
    assert r["trend"] == "up"

    assert [c["category"] for c in r["top_categories"]] == ["Shopping", "Food & Dining", "Miscellaneous"]
    assert [t["amount"] for t in r["biggest_debits"]] == [Decimal("1500.00"), Decimal("700.00"), Decimal("300.00")]
    assert r["biggest_debits"][0]["payee"] == "Amazon"
    assert r["uncategorised_count"] == 1

    needing = {b["category"]: b["state"] for b in r["budgets_needing_attention"]}
    assert needing == {"Food & Dining": "near", "Shopping": "over"}

    by_name = {a["name"]: a["reasons"] for a in r["accounts_needing_attention"]}
    assert set(by_name) == {"Savings", "Wallet"}
    assert any("No two bank balances" in x for x in by_name["Savings"])
    assert any("₹200.00" in x for x in by_name["Wallet"])


def test_weekly_review_of_a_finished_week_uses_the_whole_previous_week():
    seed_two_weeks()
    r = tools.weekly_review("2026-10-18")                    # Sunday: the week of 12 to 18 Oct
    assert not r["is_current"] and r["days_elapsed"] == 7
    assert r["spending"] == Decimal("1900.00")
    assert r["prev_spending"] == Decimal("0.00") and r["trend"] == "no_prior" and r["spending_change_pct"] is None


def test_weekly_review_on_an_empty_database():
    r = tools.weekly_review()
    assert r["count"] == 0 and r["spending"] == Decimal("0.00") and r["trend"] == "flat"
    assert r["top_categories"] == [] and r["biggest_debits"] == []
    assert r["last_commitment"] is None and r["commitment"] is None
    assert [a["name"] for a in r["accounts_needing_attention"]] == ["No bank account"]     # see the zero-accounts tests below


def test_commitment_roundtrip(monkeypatch):
    saved = tools.save_commitment("  No more   than 3 delivery orders ")
    assert saved == {"week_start": date(2026, 10, 19), "commitment": "No more than 3 delivery orders"}
    assert tools.weekly_review()["commitment"] == "No more than 3 delivery orders"

    tools.save_commitment("Cook at home on weekdays")        # saving again in the same week replaces it
    assert tools.weekly_review()["commitment"] == "Cook at home on weekdays"

    monkeypatch.setattr(tools, "today", lambda: date(2026, 10, 28))   # a week later
    nxt = tools.weekly_review()
    assert nxt["last_commitment"] == "Cook at home on weekdays"
    assert nxt["commitment"] is None


@pytest.mark.parametrize("text", ["", "   ", "x" * 281])
def test_commitment_must_be_short_and_not_empty(text):
    with pytest.raises(RectifiError):
        tools.save_commitment(text)


def test_week_must_start_on_monday_in_the_database():
    from rectifi.db import connect

    with pytest.raises(Exception), connect() as conn:
        conn.execute("INSERT INTO weekly_reviews (week_start, commitment) VALUES ('2026-10-21', 'x')")


# ───────────── "All clear" must be earned: only with a real, balanced account and nothing unlinked ─────────────

def clean_account(name="Savings", last4="1111"):
    tools.add_account(name, last4)
    tools.record_balance(name, "2026-10-19", "1000")
    tools.record_balance(name, "2026-10-21", "700")      # 1000 - 300 spent on the 20th = 700, so it balances


def test_zero_accounts_is_never_all_clear():
    add("250", "debit", "2026-10-20", "Swiggy")
    r = tools.weekly_review()
    assert r["account_count"] == 0
    assert not r["all_clear"]
    [item] = r["accounts_needing_attention"]
    assert item["id"] is None
    assert "No bank account added yet" in item["reasons"][0]
    assert "can't prove nothing is missing" in item["reasons"][0]
    assert tools.weekly_review_text(r)["all_clear"] is False


def test_accounts_without_any_balance_are_not_all_clear():
    tools.add_account("Savings", "1111")
    add("250", "debit", "2026-10-20", "Swiggy", account="Savings")
    r = tools.weekly_review()
    assert r["account_count"] == 1 and not r["all_clear"]
    [item] = r["accounts_needing_attention"]
    assert item["name"] == "Savings"
    assert "No bank balance recorded yet" in item["reasons"][0]


def test_unlinked_entries_are_counted_and_block_all_clear():
    clean_account()
    add("300", "debit", "2026-10-20", "Swiggy", account="Savings")
    add("50", "debit", "2026-10-20", "Tea")                         # no account
    add("20", "debit", "2026-10-21", "Tea")                         # no account
    add("99", "debit", "2026-10-13", "Tea")                         # last week: not counted
    r = tools.weekly_review()
    assert r["unlinked_count"] == 2
    assert r["accounts_needing_attention"] == []                    # the account itself is fine
    assert not r["all_clear"]
    assert tools.weekly_review_text(r)["entries_not_linked_to_an_account"] == 2


def test_all_clear_only_in_the_truly_clean_case():
    clean_account()
    add("300", "debit", "2026-10-20", "Swiggy", account="Savings")
    r = tools.weekly_review()
    assert r["unlinked_count"] == 0 and r["accounts_needing_attention"] == []
    assert r["uncategorised_count"] == 0 and r["budgets_needing_attention"] == []
    assert r["all_clear"]

    # each single problem takes it away again
    add("10", "debit", "2026-10-21", "Corner shop", account="Savings")     # lands in "Miscellaneous": uncategorised
    assert not tools.weekly_review()["all_clear"]


def test_a_budget_near_its_limit_blocks_all_clear():
    clean_account()
    add("300", "debit", "2026-10-20", "Swiggy", account="Savings")
    tools.set_budget("Food & Dining", "320", "2026-10")
    assert not tools.weekly_review()["all_clear"]


def test_text_says_nothing_to_compare_when_previous_spending_is_zero():
    add("300", "debit", "2026-10-20", "Swiggy")
    text = tools.weekly_review_text(tools.weekly_review())
    assert text["spending_change"].startswith("nothing to compare")
    assert "+" not in text["spending_change"]


# ───────────── default account for entries ─────────────

def test_default_account_helper():
    assert tools.default_account() is None and tools.only_account_id() is None
    a = tools.add_account("Savings", "1111")
    assert tools.default_account()["name"] == "Savings" and tools.only_account_id() == a["id"]

    b = tools.add_account("Wallet", "2222")
    assert tools.only_account_id() is None                          # two accounts: no automatic choice for the agent
    assert tools.default_account()["name"] == "Savings"             # nothing used yet: first by name
    add("10", "debit", "2026-10-20", "Tea", account="Wallet")
    assert tools.default_account()["id"] == b["id"]                 # most recently used
    add("10", "debit", "2026-10-20", "Tea", account="Savings")
    assert tools.default_account()["name"] == "Savings"
    add("10", "debit", "2026-10-20", "Tea")                         # an unlinked entry does not change it
    assert tools.default_account()["name"] == "Savings"
