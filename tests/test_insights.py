"""Insights: every rule, with a fixed "today". The rules are deterministic, so the same data gives the same list.

Today is pinned to Wednesday 21 October 2026 (day 21 of 31) unless a test says otherwise.
"""

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from rectifi import insights as ins
from rectifi import tools
from rectifi.insights import Insight, insights
from rectifi.money import fmt_inr
from rectifi.parsing import AMOUNT

D = Decimal
TODAY = date(2026, 10, 21)


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: TODAY)


def add(amount, when, payee, category=None, direction="debit", account="Main"):
    tools.add_transaction(str(amount), direction, when, payee=payee, category=category, account=account, source="manual")


def bank():
    tools.add_account("Main", "1234")


def salary(*months, amount="10000"):
    for m in months:
        add(amount, date(2026, m, 1), "Stipend", "Salary & Allowance", direction="credit")


def kinds(found, kind):
    return [i for i in found if i.kind == kind]


def only(found, kind) -> Insight:
    hits = kinds(found, kind)
    assert len(hits) == 1, [i.title for i in found]
    return hits[0]


# ───────────────────────── nothing to say ─────────────────────────

def test_no_data_gives_no_insights():
    assert insights() == []


def test_the_same_data_gives_the_same_list_twice():
    bank()
    salary(7, 8, 9, 10)
    for m in (7, 8, 9):
        add(1000, date(2026, m, 10), "Zara", "Shopping")
    add(2500, date(2026, 10, 10), "Zara", "Shopping")
    assert [(i.key, i.title) for i in insights()] == [(i.key, i.title) for i in insights()]


# ───────────────────────── budget pace ─────────────────────────

def food_budget(limit="3000", month=date(2026, 10, 1)):
    tools.set_budget("Food & Dining", limit, month)


def test_pace_projects_the_month_and_says_how_far_over():
    bank()
    food_budget("3000")
    for day in (2, 5, 9, 14, 18, 20):
        add(400, date(2026, 10, day), "Swiggy", "Food & Dining")                      # 2,400 in 21 of 31 days
    hit = only(insights(), "budget_pace")
    assert hit.severity == "act"
    assert hit.title == "At this pace Food & Dining ends the month ~₹543 over"       # 2400 x 31 / 21 = 3542.86 -> 3,543 - 3,000
    assert hit.numbers == {"spent": D(2400), "limit": D(3000), "day": D(21), "days_in_month": D(31),
                           "projected": D(3543), "over_by": D(543)}
    assert (hit.action_page, hit.action_label) == ("budgets", "Open Budgets")


def test_pace_near_the_limit_is_a_watch():
    bank()
    food_budget("3000")
    for day in (2, 5, 9, 14, 18, 20):
        add(330, date(2026, 10, day), "Swiggy", "Food & Dining")                      # 1,980 -> 2,923 projected, 97% of the limit
    hit = only(insights(), "budget_pace")
    assert hit.severity == "watch" and hit.numbers["projected"] == D(2923)


def test_pace_comfortably_under_the_limit_says_nothing():
    bank()
    food_budget("3000")
    for day in (2, 9, 14):
        add(200, date(2026, 10, day), "Swiggy", "Food & Dining")
    assert kinds(insights(), "budget_pace") == []


def test_no_projection_before_day_5_and_it_says_so(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: date(2026, 10, 3))
    bank()
    food_budget("1000")
    for day in (1, 2, 3):
        add(900, date(2026, 10, day), "Swiggy", "Food & Dining")                      # would project to ~9,300 if it dared
    hit = only(insights(), "budget_pace")
    assert hit.severity == "info" and "start on day 5" in hit.title
    assert "projected" not in hit.numbers and hit.numbers["day"] == D(3)


def test_a_single_big_payment_is_not_projected_but_already_over_is_still_reported():
    bank()
    tools.set_budget("Rent", "12000", date(2026, 10, 1))
    add(10000, date(2026, 10, 1), "Landlord", "Rent")                                # one payment: lumpy, not a pace
    assert kinds(insights(), "budget_pace") == []
    tools.set_budget("Shopping", "1000", date(2026, 10, 1))
    add(1500, date(2026, 10, 2), "Shoes", "Shopping")
    hit = only(insights(), "budget_pace")
    assert hit.severity == "act" and hit.title == "Shopping is already ₹500 over its ₹1,000 budget"
    assert "projected" not in hit.numbers


def test_no_budgets_no_pace_insight():
    bank()
    add(400, date(2026, 10, 2), "Swiggy", "Food & Dining")
    assert kinds(insights(), "budget_pace") == []


# ───────────────────────── category spike ─────────────────────────

def spend_months(category, amounts, payee="Zara"):
    for month, amount in amounts.items():
        add(amount, date(2026, month, 10), payee, category)


def test_a_category_far_above_your_usual_month_is_flagged():
    bank()
    spend_months("Shopping", {7: 1000, 8: 1200, 9: 800, 10: 2000})
    hit = only(insights(), "category_spike")
    assert hit.severity == "watch" and hit.title == "Shopping is already 100% above your usual month"
    assert hit.numbers == {"current": D(2000), "usual": D(1000), "extra": D(1000), "above_pct": D(100), "prior_months": D(3)}


def test_spike_needs_30_percent_and_500_rupees():
    bank()
    spend_months("Shopping", {7: 1000, 8: 1000, 10: 1400})                           # +40% but only 400 more
    spend_months("Education", {7: 10000, 8: 10000, 10: 12000})                      # +500 more but only 20%
    assert kinds(insights(), "category_spike") == []
    spend_months("Groceries", {7: 1000, 8: 1000, 10: 1500})                         # +50% and 500: flagged
    assert [i.title.split(" is")[0] for i in kinds(insights(), "category_spike")] == ["Groceries"]


def test_spike_needs_two_earlier_months_of_data():
    bank()
    spend_months("Shopping", {9: 1000, 10: 5000})                                    # only one earlier month
    assert kinds(insights(), "category_spike") == []
    spend_months("Shopping", {8: 1000})
    assert len(kinds(insights(), "category_spike")) == 1


def test_a_month_with_data_but_none_in_that_category_counts_as_zero():
    bank()
    spend_months("Food & Dining", {7: 100, 8: 100, 9: 100})                          # July, August, September all have data
    spend_months("Shopping", {9: 3000, 10: 3000})                                    # Shopping only in September
    hit = only(insights(), "category_spike")
    assert hit.numbers["usual"] == D(1000) and hit.numbers["above_pct"] == D(200)    # (0 + 0 + 3000) / 3 months, not 3000 / 1


# ───────────────────────── recurring payments ─────────────────────────

def monthly(payee, amount, months, day, category="Subscriptions"):
    for m in months:
        add(amount, date(2026, m, day), payee, category)


def test_recurring_payments_are_listed_with_monthly_and_yearly_cost():
    bank()
    monthly("Netflix", 199, (8, 9, 10), 5)                                           # 3 of the last 4 months
    monthly("Spotify", 119, (7, 8, 9, 10), 12)
    for day in range(1, 15):
        add(20, date(2026, 10, day), "Chai stall", "Food & Dining")                  # a habit, not a subscription
    hit = only(insights(), "recurring")
    assert hit.severity == "info"
    assert hit.title == "2 recurring payments cost ₹318 a month"
    assert hit.numbers == {"monthly_total": D(318), "yearly_total": D(3816), "recurring_count": D(2),
                           "netflix_monthly": D(199), "spotify_monthly": D(119)}
    assert "Netflix ₹199, Spotify ₹119" in hit.detail and "₹3,816 a year" in hit.detail


def test_two_payments_in_two_months_are_not_recurring_yet():
    bank()
    monthly("Netflix", 199, (9, 10), 5)
    assert kinds(insights(), "recurring") == []


def test_amounts_must_be_similar_for_a_payee_to_count_as_recurring():
    bank()
    for m, amount in ((8, 250), (9, 900), (10, 40)):
        add(amount, date(2026, m, 6), "Swiggy", "Food & Dining")
    assert kinds(insights(), "recurring") == []


def test_a_four_weekly_payment_counts_as_monthly():
    bank()
    for d in (date(2026, 7, 29), date(2026, 8, 26), date(2026, 9, 23), date(2026, 10, 21)):
        add(500, d, "Gym", "Health")
    assert [i.numbers["gym_monthly"] for i in kinds(insights(), "recurring")] == [D(500)]


def test_a_payment_that_stopped_months_ago_was_cancelled_and_is_not_listed():
    bank()
    for d in (date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 30)):              # last charge 52 days ago
        add(500, d, "Gym", "Health")
    assert kinds(insights(), "recurring") == []
    add(500, date(2026, 9, 30), "Gym", "Health")                                     # 21 days ago: still being charged
    assert len(kinds(insights(), "recurring")) == 1


def test_a_new_recurring_payment_is_flagged_when_older_history_exists():
    bank()
    add(300, date(2026, 6, 1), "Stationery", "Shopping")                             # the data starts in June
    monthly("Hotstar", 149, (8, 9, 10), 8)                                           # first charge 8 Aug, 74 days ago
    new = only(insights(), "recurring_new")
    assert new.severity == "watch" and new.title == "Hotstar looks like a new monthly payment"
    assert new.numbers == {"monthly": D(149), "yearly": D(1788), "charges_count": D(3)}


def test_a_payment_that_has_always_been_there_is_not_new():
    bank()
    monthly("Spotify", 119, (6, 7, 8, 9, 10), 12)
    assert kinds(insights(), "recurring_new") == []


def test_a_price_rise_on_a_recurring_payment_is_flagged():
    bank()
    monthly("Gym", 500, (7, 8, 9), 3, "Health")
    add(650, date(2026, 10, 3), "Gym", "Health")
    up = only(insights(), "recurring_price")
    assert up.title == "Gym went up from ₹500 to ₹650"
    assert up.numbers == {"old": D(500), "new": D(650), "extra_monthly": D(150), "extra_yearly": D(1800)}
    assert only(insights(), "recurring").numbers["gym_monthly"] == D(650)             # the cost shown is the new price


def test_an_old_price_rise_is_not_news():
    bank()
    monthly("Gym", 500, (4, 5), 3, "Health")
    monthly("Gym", 650, (6, 7, 8, 9, 10), 3, "Health")
    assert kinds(insights(), "recurring_price") == []


# ───────────────────────── large payments ─────────────────────────

def everyday_payments(count=12, amount=300):
    for n in range(count):
        add(amount, TODAY - timedelta(days=20 + n * 3), f"Shop{n}", "Shopping")


def test_a_recent_payment_above_the_threshold_is_flagged():
    bank()
    everyday_payments()
    add(5000, date(2026, 10, 18), "Croma", "Shopping")
    add(1500, date(2026, 10, 19), "Pharmacy", "Health")                              # big for you, still under 2,000
    big = only(insights(), "large_payment")
    assert big.severity == "watch" and big.title == "₹5,000 to Croma on 18 Oct is unusually large"
    assert big.numbers == {"amount": D(5000), "threshold": D(2000), "typical": D(300), "floor": D(2000)}


def test_the_threshold_is_three_times_the_median_when_that_is_more_than_2000():
    bank()
    everyday_payments(amount=900)                                                    # median 900: threshold 2,700
    add(2500, date(2026, 10, 18), "Croma", "Shopping")
    assert kinds(insights(), "large_payment") == []
    add(2800, date(2026, 10, 19), "Croma", "Shopping")
    assert only(insights(), "large_payment").numbers["threshold"] == D(2700)


def test_an_old_large_payment_and_too_little_history_are_not_flagged():
    bank()
    everyday_payments()
    add(9000, date(2026, 9, 1), "Croma", "Shopping")                                 # long ago
    assert kinds(insights(), "large_payment") == []
    tools.delete_transaction(tools.list_transactions(search="Croma", limit=1)[0]["id"])
    for n in range(2):
        add(100, date(2026, 10, 10 + n), "Chai", "Food & Dining")
    # only 14 payments are in the window: add one big one, and a tiny history elsewhere would hide it
    assert kinds(insights(), "large_payment") == []


def test_four_payments_are_too_few_to_know_what_is_typical():
    bank()
    for n in range(3):
        add(100, date(2026, 10, 5 + n), "Chai", "Food & Dining")
    add(9000, date(2026, 10, 18), "Croma", "Shopping")
    assert kinds(insights(), "large_payment") == []


def test_rent_is_not_unusual_because_it_repeats():
    bank()
    everyday_payments()
    monthly("Landlord", 8000, (7, 8, 9), 15, "Rent")
    add(8000, date(2026, 10, 15), "Landlord", "Rent")
    assert kinds(insights(), "large_payment") == []
    assert any("Landlord" in i.detail for i in kinds(insights(), "recurring"))


# ───────────────────────── savings rate ─────────────────────────

def spend_in(month, amount):
    add(amount, date(2026, month, 15), "Spend", "Shopping")


def test_a_drop_of_10_points_or_more_is_flagged():
    bank()
    salary(7, 8, 9)
    spend_in(7, 5000)
    spend_in(8, 5000)
    spend_in(9, 8000)                                                                # 50%, 50%, then 20%
    hit = only(insights(), "savings_rate")
    assert hit.severity == "watch" and hit.title == "Your savings rate fell from 50.0% to 20.0% in September"
    assert hit.numbers == {"latest_pct": D("20.0"), "before_pct": D("50.0"), "change_points": D("30.0"),
                           "income": D(10000), "spending": D(8000)}


def test_spending_above_income_is_an_act():
    bank()
    salary(7, 8, 9)
    spend_in(7, 5000)
    spend_in(8, 5000)
    spend_in(9, 12000)
    assert only(insights(), "savings_rate").severity == "act"


def test_an_improvement_of_10_points_is_praised_as_info():
    bank()
    salary(7, 8, 9)
    spend_in(7, 8000)
    spend_in(8, 8000)
    spend_in(9, 4000)
    hit = only(insights(), "savings_rate")
    assert hit.severity == "info" and hit.title == "Your savings rate rose from 20.0% to 60.0% in September. Nice."


def test_small_changes_and_missing_income_say_nothing():
    bank()
    salary(7, 8, 9)
    for m, amount in ((7, 5000), (8, 5000), (9, 5800)):                              # 50, 50, 42: only 8 points
        spend_in(m, amount)
    assert kinds(insights(), "savings_rate") == []


def test_the_trend_needs_the_latest_complete_month_and_one_other():
    bank()
    salary(9)
    spend_in(9, 9000)
    assert kinds(insights(), "savings_rate") == []                                   # one month is not a trend
    salary(7)
    spend_in(7, 1000)
    assert len(kinds(insights(), "savings_rate")) == 1


# ───────────────────────── unbudgeted categories ─────────────────────────

def test_a_big_category_without_a_budget_gets_a_rounded_suggestion_you_must_apply_yourself():
    bank()
    spend_months("Food & Dining", {7: 3000, 8: 3300, 9: 3450}, payee="Swiggy")
    spend_months("Education", {7: 300, 8: 300, 9: 300}, payee="Course")              # average 300: too small to suggest
    hit = only(insights(), "unbudgeted")
    assert hit.title == "Food & Dining has no budget but averages ₹3,250 a month"
    assert hit.numbers == {"average": D(3250), "suggested_budget": D(3300), "prior_months": D(3)}   # 3,250 -> 3,300 (nearest 100)
    assert hit.apply == {"tool": "set_budget", "category": "Food & Dining", "limit": D(3300), "month": date(2026, 10, 1)}
    assert hit.action_label == "Set a ₹3,300 budget"
    assert tools.budget_status("2026-10")["categories"] == []                        # computing the insight changed nothing


def test_a_category_that_already_has_a_budget_is_not_suggested():
    bank()
    spend_months("Food & Dining", {7: 3000, 8: 3000, 9: 3000}, payee="Swiggy")
    food_budget("3500")
    assert kinds(insights(), "unbudgeted") == []


def test_other_is_never_suggested():
    bank()
    spend_months("Miscellaneous", {7: 3000, 8: 3000, 9: 3000}, payee="Stuff")
    assert kinds(insights(), "unbudgeted") == []


# ───────────────────────── small leaks ─────────────────────────

def test_many_small_payments_that_add_up_are_a_leak():
    bank()
    for n in range(10):
        add(120, TODAY - timedelta(days=n * 2), "Chai point", "Food & Dining")
    add(150, TODAY - timedelta(days=3), "Snacks", "Shopping")                         # one small payment elsewhere: not a leak
    hit = only(insights(), "small_leak")
    assert hit.title == "10 small Food & Dining payments added up to ₹1,200 in 30 days"
    assert hit.numbers == {"leak_count": D(10), "total": D(1200), "under": D(200), "window_days": D(30)}


def test_a_few_small_payments_or_a_small_total_is_not_a_leak():
    bank()
    for n in range(10):
        add(30, TODAY - timedelta(days=n), "Chai point", "Food & Dining")            # 10 payments but only 300 in total
    for n in range(5):
        add(190, TODAY - timedelta(days=n), "Canteen", "Shopping")                    # 950 in total, only 5 payments
    assert kinds(insights(), "small_leak") == []


def test_payments_of_200_or_more_are_not_small():
    bank()
    for n in range(10):
        add(200, TODAY - timedelta(days=n), "Canteen", "Food & Dining")
    assert kinds(insights(), "small_leak") == []


# ───────────────────────── data quality ─────────────────────────

def test_no_account_is_the_first_thing_to_fix():
    add(100, date(2026, 10, 2), "Chai", "Food & Dining", account=None)
    first = insights()[0]
    assert first.kind == "no_account" and first.severity == "act" and first.action_page == "accounts"


def test_unlinked_entries_are_counted_with_a_way_to_fix_them():
    bank()
    add(100, date(2026, 10, 2), "Chai", "Food & Dining", account=None)
    add(100, date(2026, 10, 3), "Chai", "Food & Dining", account=None)
    add(100, date(2026, 10, 4), "Chai", "Food & Dining")
    hit = only(insights(), "unlinked")
    assert hit.severity == "act" and hit.title == "2 entries are not linked to an account"
    assert hit.numbers["unlinked_count"] == D(2) and hit.action_page == "ledger"


def test_payments_stuck_in_other_are_counted():
    bank()
    for day in (2, 3, 4):
        add(100, date(2026, 10, day), "Mystery", None)                               # no rule matches: lands in Miscellaneous
    hit = only(insights(), "other_pile")
    assert hit.severity == "watch" and hit.title == "3 payments sit in “Miscellaneous” (₹300)"
    assert hit.numbers["other_total"] == D(300)


def test_an_account_never_checked_or_checked_long_ago_is_flagged():
    bank()
    add(100, date(2026, 10, 2), "Chai", "Food & Dining")
    assert "never been checked" in only(insights(), "stale_account").title
    tools.record_balance("Main", date(2026, 9, 1), "5000")                           # 50 days before today
    hit = only(insights(), "stale_account")
    assert hit.title == "Main was last checked against the bank 50 days ago" and hit.numbers == {"age_days": D(50)}
    tools.record_balance("Main", date(2026, 10, 15), "5000")
    assert kinds(insights(), "stale_account") == []


def test_a_balance_gap_is_an_act():
    bank()
    tools.record_balance("Main", date(2026, 10, 1), "1000")
    add(100, date(2026, 10, 5), "Chai", "Food & Dining")
    tools.record_balance("Main", date(2026, 10, 20), "850")                          # expected 900
    hit = only(insights(), "balance_gap")
    assert hit.severity == "act" and hit.title == "Main is out by ₹50" and hit.numbers == {"gap": D(50)}


def test_data_quality_comes_first_even_when_other_insights_are_more_severe():
    bank()
    food_budget("1000")
    for day in (2, 5, 9, 14):
        add(500, date(2026, 10, day), "Swiggy", "Food & Dining")                      # already over: an act
    add(100, date(2026, 10, 3), "Chai", "Food & Dining", account=None)                # unlinked: data quality
    found = insights()
    assert [i.kind for i in found][0] == "unlinked"
    assert found.index(only(found, "budget_pace")) > found.index(only(found, "unlinked"))


def test_within_one_group_act_comes_before_watch_before_info():
    bank()
    salary(7, 8, 9)
    spend_months("Shopping", {7: 1000, 8: 1000, 9: 1000, 10: 3000})                   # spike (watch)
    monthly("Netflix", 199, (8, 9, 10), 5)                                           # recurring (info)
    food_budget("1000")
    for day in (2, 5, 9, 14):
        add(500, date(2026, 10, day), "Swiggy", "Food & Dining")                      # pace (act)
    rest = [i for i in insights() if i.kind not in ins.DATA_QUALITY_KINDS]
    ranks = [ins._RANK[i.severity] for i in rest]
    assert ranks == sorted(ranks) and ranks[0] == 0 and ranks[-1] == 2


# ───────────────────────── every figure comes from `numbers` ─────────────────────────

def big_scenario():
    bank()
    salary(7, 8, 9, 10)
    add(300, date(2026, 6, 1), "Stationery", "Shopping")
    for m, amount in ((7, 2500), (8, 2600), (9, 2700)):
        add(amount, date(2026, m, 9), "Swiggy", "Food & Dining")
    monthly("Netflix", 199, (8, 9, 10), 5)
    monthly("Hotstar", 149, (8, 9, 10), 8)
    monthly("Gym", 500, (7, 8, 9), 3, "Health")
    add(650, date(2026, 10, 3), "Gym", "Health")
    everyday_payments()
    add(5400, date(2026, 10, 18), "Croma", "Shopping")
    food_budget("3000")
    for day in (2, 5, 9, 14, 18, 20):
        add(450, date(2026, 10, day), "Swiggy", "Food & Dining")
    for n in range(10):
        add(110, TODAY - timedelta(days=n), "Chai point", "Food & Dining")
    add(100, date(2026, 10, 3), "Mystery", None, account=None)
    for m, amount in ((7, 5000), (8, 5000), (9, 9000)):
        spend_in(m, amount)


def figures(text: str) -> list[Decimal]:
    return [D(m.replace(",", "")) for m in re.findall(r"₹\s*([\d,]+(?:\.\d+)?)", text)]


def test_every_rupee_figure_shown_is_in_numbers_and_numbers_are_whole_rupees():
    big_scenario()
    found = insights()
    seen = {i.kind for i in found}
    assert {"budget_pace", "recurring", "recurring_new", "recurring_price", "large_payment", "savings_rate",
            "small_leak", "unlinked", "other_pile", "stale_account", "category_spike"} <= seen
    for i in found:
        allowed = set(i.numbers.values())
        for text in (i.title, i.detail, i.action_label or ""):
            for value in figures(text):
                assert value in allowed, f"{i.kind}: ₹{value} in {text!r} is not in numbers {i.numbers}"
        for key, value in i.numbers.items():
            assert isinstance(value, Decimal)
            if ins.is_money_key(key):
                assert value == value.to_integral_value(), f"{i.kind}.{key} = {value} is not whole rupees"


def test_money_keys_are_told_apart_from_counts():
    assert ins.is_money_key("spent") and ins.is_money_key("netflix_monthly") and ins.is_money_key("floor")
    for key in ("unlinked_count", "above_pct", "age_days", "prior_months", "day", "days_in_month", "change_points"):
        assert not ins.is_money_key(key), key


def test_insights_text_carries_the_titles_for_the_agent():
    big_scenario()
    rows = ins.insights_text(insights())
    assert rows and set(rows[0]) == {"severity", "kind", "title", "detail", "where_to_act"}
    assert rows[0]["title"] == insights()[0].title


def test_top_insights_prefers_things_to_act_on_and_act_insights_filters():
    big_scenario()
    top = ins.top_insights(3)
    assert len(top) == 3 and all(i.severity != "info" for i in top)
    assert all(i.severity == "act" for i in ins.act_insights()) and ins.act_insights()


def test_insights_never_writes_anything():
    big_scenario()
    before = (len(tools.list_transactions(limit=500)), tools.budget_status("2026-10")["total_limit"])
    insights()
    assert (len(tools.list_transactions(limit=500)), tools.budget_status("2026-10")["total_limit"]) == before


def test_insights_module_has_no_model_code_and_no_floats():
    source = Path(ins.__file__).read_text(encoding="utf-8")
    assert "rectifi.agent" not in source and "langchain" not in source and "from .agent" not in source
    assert not re.search(r"\bfloat\(", source)


def test_data_span_says_how_much_there_is():
    assert ins.data_span()["count"] == 0
    bank()
    add(100, date(2026, 10, 1), "Chai", "Food & Dining")
    add(100, date(2026, 10, 11), "Chai", "Food & Dining")
    assert ins.data_span() == {"count": 2, "first": date(2026, 10, 1), "last": date(2026, 10, 11), "days": 11}
