"""P2: the alert rules. Pure reads of the ledger: caps, streaks, projection, safe to spend, income, presets, the waterfall."""

import subprocess
import sys
from datetime import date
from decimal import Decimal as D

import pytest

from rectifi import alerts, db, tools
from rectifi.money import amounts_in_text

TODAY = date(2026, 10, 21)          # a Wednesday; its ISO week starts Monday 19 October


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: TODAY)
    tools.add_account("Main", "1234")


def add(amount, when, payee, category=None, direction="debit"):
    return tools.add_transaction(str(amount), direction, when, payee=payee, category=category, account="Main", source="manual")["transaction"]


def ctx():
    return alerts.build_context(TODAY)


def caps_now():
    return alerts.cap_alerts(ctx())


# ───────────── caps ─────────────

def test_cap_steps_fire_once_each():
    alerts.create_rule("merchant", "Swiggy", "week", 1000)
    seen = []
    for step in (490, 10, 290, 20, 200):              # running totals 490, 500, 790, 810, 1010
        if step:
            add(step, date(2026, 10, 20), "Swiggy")
        found = caps_now()
        seen.append(found[0].threshold if found else None)
    assert seen == [None, 50, 50, 80, 100]
    keys = {a.dedupe_key for a in caps_now()}
    assert keys == {"cap:merchant:Swiggy:week:2026-10-19:100"}


def test_jump_from_40_to_85_fires_only_80():
    alerts.create_rule("merchant", "Swiggy", "week", 1000)
    add(400, date(2026, 10, 19), "Swiggy")
    assert caps_now() == []
    add(450, date(2026, 10, 20), "Swiggy")
    (only,) = caps_now()
    assert only.threshold == 80 and only.severity == "watch"
    assert only.numbers["spent"] == D(850) and only.numbers["cap"] == D(1000) and only.numbers["left"] == D(150)


def test_group_category_total_and_period_scopes():
    alerts.create_rule("group", "Food delivery", "day", 500)
    alerts.create_rule("category", "Groceries", "month", 1000)
    alerts.create_rule("total", "all", "day", 2000)
    add(300, TODAY, "Zomato")
    add(300, TODAY, "Swiggy")            # group total today 600 -> over its 500 daily cap
    add(900, date(2026, 10, 3), "Dmart", "Groceries")
    by_key = {a.dedupe_key: a for a in caps_now()}
    assert "cap:group:Food delivery:day:2026-10-21:100" in by_key
    assert "cap:category:Groceries:month:2026-10-01:80" in by_key
    assert not any(k.startswith("cap:total") for k in by_key)          # only today counts for a day cap: 600 of 2,000
    over = by_key["cap:group:Food delivery:day:2026-10-21:100"]
    assert over.severity == "act" and over.numbers["over"] == D(100)


def test_yesterday_does_not_count_for_a_day_cap():
    alerts.create_rule("merchant", "Swiggy", "day", 500)
    add(900, date(2026, 10, 20), "Swiggy")
    assert caps_now() == []


def test_budgets_are_implicit_category_month_caps():
    tools.set_budget("Groceries", 1000, "2026-10")
    add(850, date(2026, 10, 5), "Dmart", "Groceries")
    (a,) = caps_now()
    assert a.rule_id is None and a.threshold == 80 and a.dedupe_key == "cap:category:Groceries:month:2026-10-01:80"


def test_explicit_rule_replaces_the_budget_cap_and_muted_or_snoozed_rules_are_silent():
    tools.set_budget("Groceries", 1000, "2026-10")
    rule = alerts.create_rule("category", "Groceries", "month", 5000)
    add(850, date(2026, 10, 5), "Dmart", "Groceries")
    assert caps_now() == []                                            # 850 of 5,000, not of the 1,000 budget
    alerts.update_rule(rule["id"], cap=1000)
    assert len(caps_now()) == 1
    alerts.update_rule(rule["id"], muted=True)
    assert caps_now() == []


def test_new_debits_limit_which_caps_speak():
    alerts.create_rule("merchant", "Swiggy", "week", 1000)
    add(900, date(2026, 10, 20), "Swiggy")
    unrelated = add(120, TODAY, "Dmart", "Groceries")
    assert alerts.cap_alerts(ctx(), new=[dict(unrelated, merchant=None, grp=None, category="Groceries")]) == []
    assert len(alerts.cap_alerts(ctx())) == 1


def test_rule_validation():
    from rectifi.tools import RectifiError

    for bad in (("merchant", "Nonexistent", "week", 100), ("group", "Nope", "week", 100), ("category", "Salary & Allowance", "week", 100),
                ("merchant", "Swiggy", "year", 100), ("merchant", "Swiggy", "week", 0), ("banana", "x", "week", 10)):
        with pytest.raises(RectifiError):
            alerts.create_rule(*bad)
    again = alerts.create_rule("merchant", "swiggy", "week", 700)       # spelling is corrected, same row is updated
    assert again["target"] == "Swiggy"
    assert alerts.create_rule("merchant", "Swiggy", "week", 900)["id"] == again["id"] and len(alerts.list_rules()) == 1


# ───────────── streaks ─────────────

def test_streak_tiers_count_per_iso_week():
    for day in (19, 19, 20, 20):
        add(100, date(2026, 10, day), "Swiggy")
    assert alerts.streak_alerts(ctx()) == []
    add(214, date(2026, 10, 21), "Swiggy")                                # 5th this week
    (a,) = alerts.streak_alerts(ctx())
    assert a.dedupe_key == "streak:Swiggy:2026-10-19:5" and a.title.startswith("5th Swiggy order this week") and a.numbers["week_total"] == D(614)
    add(100, date(2026, 10, 21), "Swiggy")
    assert alerts.streak_alerts(ctx())[0].dedupe_key.endswith(":5")       # the 6th: still tier 5
    add(100, date(2026, 10, 21), "Swiggy")
    (seven,) = alerts.streak_alerts(ctx())
    assert seven.dedupe_key.endswith(":7") and seven.title.startswith("7th Swiggy order")


def test_orders_before_monday_do_not_count():
    for day in (14, 15, 16, 17, 18, 18):
        add(100, date(2026, 10, day), "Zomato")
    add(100, TODAY, "Zomato")
    assert alerts.streak_alerts(ctx()) == []


# ───────────── projection, income, safe to spend ─────────────

def test_no_projection_before_day_five(monkeypatch):
    add(1000, date(2026, 10, 3), "Dmart", "Groceries")
    monkeypatch.setattr(tools, "today", lambda: date(2026, 10, 4))
    assert alerts.projection(alerts.build_context(date(2026, 10, 4))) is None


def test_projection_and_percent_of_income():
    add(2100, date(2026, 10, 2), "Dmart", "Groceries")
    alerts.save_prefs({"monthly_income": "100000"})
    p = alerts.projection(ctx())
    assert p["spent"] == D(2100) and p["day"] == 21 and p["days_in_month"] == 31
    assert p["projected"] == D("3100.00") and p["pct_of_income"] == D("3.1")


def test_income_precedence():
    assert alerts.income(ctx())["source"] is None
    add(40000, date(2026, 9, 1), "Employer", "Salary & Allowance", "credit")          # 50 days ago: too old
    assert alerts.income(ctx())["source"] is None
    add(30000, date(2026, 10, 1), "Employer", "Salary & Allowance", "credit")
    add(5000, date(2026, 10, 2), "Employer", "Salary & Allowance", "credit")
    found = alerts.income(ctx())
    assert found["source"] == "salary" and found["amount"] == D(35000)
    alerts.save_prefs({"monthly_income": "50000"})
    assert alerts.income(ctx()) == {"amount": D("50000.00"), "source": "manual", "month": None}


def test_safe_to_spend_none_without_inputs():
    assert alerts.safe_to_spend(ctx()) is None


def test_safe_to_spend_formula_days_left_includes_today_and_floors():
    alerts.save_prefs({"spend_plan": "31000"})
    add(5000, date(2026, 10, 10), "Dmart", "Groceries")
    add(333, TODAY, "Zepto")
    s = alerts.safe_to_spend(ctx())
    # 11 days left in October from the 21st (21..31); (31000 - 5000) / 11 = 2363.6 -> 2363
    assert s["days_left"] == 11 and s["spent_before_today"] == D(5000) and s["committed"] == 0
    assert s["daily"] == D(2363) and s["spent_today"] == D(333) and s["left_today"] == D(2030) and s["source"] == "plan"


def test_safe_to_spend_floored_at_zero_and_sources():
    add(8000, date(2026, 10, 10), "Dmart", "Groceries")
    tools.set_budget("Groceries", 5000, "2026-10")
    s = alerts.safe_to_spend(ctx())
    assert s["source"] == "budgets" and s["daily"] == D(0) and s["left_today"] == D(0)


def test_committed_recurring_not_yet_paid_is_taken_off():
    for month in (7, 8, 9):
        add(500, date(2026, month, 25), "Gym Club", "Subscriptions")
    alerts.save_prefs({"spend_plan": "10000"})
    s = alerts.safe_to_spend(ctx())
    assert [i["name"] for i in s["committed_items"]] == ["Gym"] and s["committed"] == D(500)
    assert s["daily"] == D(int((10000 - 500) / 11))          # 863
    add(500, date(2026, 10, 15), "Gym Club", "Subscriptions")                    # paid this month: no longer committed
    assert alerts.safe_to_spend(ctx())["committed"] == 0


# ───────────── presets ─────────────

def test_presets_average_three_complete_months_and_round_up():
    for month in (7, 8, 9):
        add(1010, date(2026, month, 10), "Swiggy")                              # 3,030 in 3 months
    p = {(x["scope"], x["target"]): x for x in alerts.presets(ctx())}
    sw = p[("merchant", "Swiggy")]
    assert sw["average"]["month"] == D(1010) and sw["keep"]["month"] == D(1100) and sw["trim"]["month"] == D(900)   # 808 -> 900
    assert p[("group", "Food delivery")]["keep"]["month"] == D(1100)
    assert ("category", "Food & Dining") in p
    assert all(v % 100 == 0 and v >= 100 for x in p.values() for k in ("keep", "trim") for v in x[k].values())


def test_presets_empty_without_history():
    assert alerts.presets(ctx()) == []


# ───────────── the waterfall ─────────────

def test_waterfall_invariant_is_exact():
    alerts.save_prefs({"monthly_income": "100000"})
    add(25000, date(2026, 10, 1), "Landlord", "Rent")
    add(1999.99, date(2026, 10, 2), "Dmart", "Groceries")                       # big one-off
    add(412.37, date(2026, 10, 3), "Blinkit")
    add(250.5, date(2026, 10, 4), "Zepto")
    add(700.25, date(2026, 10, 5), "Swiggy")
    add(333.33, date(2026, 10, 6), "Amazon")
    add(120.10, date(2026, 10, 7), "Chai Point")
    add(800, date(2026, 9, 7), "Blinkit")                                       # another month: ignored
    rep = alerts.leak_report(ctx(), date(2026, 10, 1))
    seg = {s["key"]: s for s in rep["segments"]}
    assert [s["key"] for s in rep["segments"]] == ["income", "fixed", "big", "quick", "food", "shopping", "other", "left"]
    assert seg["fixed"]["amount"] == D("25000.00") and seg["big"]["amount"] == D("1999.99")
    assert seg["quick"]["amount"] == D("662.87") and seg["food"]["amount"] == D("700.25") and seg["shopping"]["amount"] == D("333.33")
    assert seg["other"]["amount"] == D("120.10")
    outflow = sum((s["amount"] for s in rep["segments"] if s["kind"] == "outflow"), D(0))
    assert rep["segments"][0]["amount"] - outflow == seg["left"]["amount"] == rep["left"]
    with db.connect() as conn:                                                   # the segments add up to what the database says
        in_sql = conn.execute("SELECT SUM(amount) AS s FROM transactions WHERE direction = 'debit' AND txn_date BETWEEN '2026-10-01' AND '2026-10-31'").fetchone()["s"]
    assert outflow == rep["spent"] == in_sql
    level = D(100000)
    for s in rep["segments"][1:-1]:                                              # bars chain: each start is the previous end
        assert s["start"] == level and s["end"] == level - s["amount"]
        level = s["end"]
    assert level == seg["left"]["amount"]


def test_waterfall_top_merchants_with_percent_of_income():
    alerts.save_prefs({"monthly_income": "100000"})
    for day in (2, 3, 4):
        add(500, date(2026, 10, day), "Swiggy")
    add(250, date(2026, 10, 5), "Blinkit")
    rep = alerts.leak_report(ctx(), date(2026, 10, 1))
    top = rep["merchants"][0]
    assert top["merchant"] == "Swiggy" and top["orders"] == 3 and top["total"] == D(1500) and top["pct_of_income"] == D("1.5")
    assert rep["projection"]["spent"] == D(2500) and rep["projection"]["projected"] == D("3690.48")        # 2,500 x 31 / 21


def test_waterfall_without_income_has_no_income_or_left():
    add(300, date(2026, 10, 2), "Swiggy")
    rep = alerts.leak_report(ctx(), date(2026, 10, 1))
    assert rep["income"] is None and rep["left"] is None and "income" not in {s["key"] for s in rep["segments"]}


# ───────────── the Today strip ─────────────

def test_today_view_lists_small_spends_with_merchant_or_masked_label():
    alerts.save_prefs({"spend_plan": "30000"})
    add(120, TODAY, "swiggy@icici")
    add(80, TODAY, "ramesh.kumar9876543210@okaxis")
    add(900, TODAY, "Dmart", "Groceries")
    view = alerts.today_view(ctx())
    labels = [s["label"] for s in view["small_spends"]]
    assert labels[0] == "Swiggy" and len(labels) == 2 and "@" not in labels[1] and not any(c.isdigit() for c in labels[1])
    assert view["safe"]["spent_today"] == D(1100) and view["unread"] == 0


# ───────────── the numbers rule ─────────────

def _all_texts(a):
    return " ".join((a.title, a.detail, a.push_title, a.push_body))


def test_every_rupee_figure_and_number_in_the_texts_is_in_numbers():
    alerts.create_rule("merchant", "Swiggy", "week", 1000)
    alerts.create_rule("total", "all", "month", 5000)
    tools.set_budget("Groceries", 1000, "2026-10")
    for amount in (214, 100, 100, 100, 100, 100, 100, 100):                       # eight Swiggy orders: tier 7
        add(amount, date(2026, 10, 20), "Swiggy")
    add(1500, date(2026, 10, 5), "Dmart", "Groceries")                           # over the budget
    found = alerts.cap_alerts(ctx()) + alerts.streak_alerts(ctx())
    assert {a.kind for a in found} == {"cap", "streak"} and len(found) >= 3
    for a in found:
        allowed = {v.quantize(D("0.01")) for v in a.numbers.values()}
        assert amounts_in_text(_all_texts(a)) <= allowed, (a.title, a.detail, a.push_title, a.push_body, a.numbers)
        assert "₹" in a.title + a.detail or a.kind == "cap"


def test_alerts_module_loads_no_langchain_or_agent():
    code = ("import sys, rectifi.alerts; "
            "bad = [m for m in sys.modules if m.split('.')[0].startswith('langchain') or m.startswith('rectifi.agent') or m.startswith('langgraph')]; "
            "print(bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout
