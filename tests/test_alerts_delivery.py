"""P4: the sweep, the gates, delivery, the tick. A fake clock and a fake push service; the database is real."""

import logging
import re
import threading
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from alerts_support import FakePush, add, add_phone, rows, states
from alerts_support import Clock, TODAY

from rectifi import alerts, importer, tools, webpush
from rectifi.db import connect


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: TODAY)
    clock = Clock()
    monkeypatch.setattr(alerts, "_clock", clock)
    push = FakePush()
    monkeypatch.setattr(webpush, "send", push)
    tools.add_account("Main", "1234")
    add_phone()
    alerts.sweep("baseline")                      # the first sweep only marks what is already here as history
    return clock, push


@pytest.fixture
def clock(world):
    return world[0]


@pytest.fixture
def push(world):
    return world[1]


def cap(target="Swiggy", period="week", amount=1000, scope="merchant"):
    return alerts.create_rule(scope, target, period, amount)


def run():
    out = alerts.sweep("test")
    alerts.deliver()
    return out


# ───────────── the sweep ─────────────

def test_sms_style_payment_today_makes_one_pending_alert_then_sends_it(push):
    cap()
    add(850, TODAY, "swiggy@icici")
    assert alerts.sweep("sms") == {"new": 1, "mode": "realtime"}
    (row,) = rows()
    assert row["push_state"] == "pending" and row["kind"] == "cap" and row["dedupe_key"].endswith(":80")
    assert alerts.deliver() == {"sent": 1, "failed": 0, "skipped": 0}
    assert rows()[0]["push_state"] == "sent" and rows()[0]["pushed_at"] is not None
    assert len(push.calls) == 1 and push.calls[0]["payload"]["u"] == f"/alerts#{row['id']}"
    assert alerts.sweep("again")["mode"] == "idle" and len(rows()) == 1                       # the watermark moved


def test_concurrent_sweeps_write_one_alert_per_key():
    cap()
    add(950, TODAY, "swiggy@icici")
    results = []
    threads = [threading.Thread(target=lambda: results.append(alerts.sweep("race"))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["new"] for r in results) == 1 and len(rows()) == 1
    assert {r["mode"] for r in results} <= {"realtime", "idle", "busy"}


def test_the_database_key_is_the_real_guard():
    cap()
    add(950, TODAY, "swiggy@icici")
    alerts.sweep("x")
    with connect() as conn:                                  # even if the watermark is lost, the same step cannot be stored twice
        conn.execute("DELETE FROM app_settings WHERE key = 'alerts.watermark'")
        conn.execute("DELETE FROM app_settings WHERE key = 'alerts.watermark'")
    alerts.sweep("again")
    assert len(rows()) == 1


def test_a_credit_or_a_transfer_makes_no_alert():
    cap(scope="total", target="all", period="day", amount=100)
    add(5000, TODAY, "Employer", "Salary & Allowance", "credit")
    add(5000, TODAY, "Mutual fund", "Investments")
    assert alerts.sweep("x") == {"new": 0, "mode": "idle"}


def test_an_old_payment_makes_one_bulk_summary_and_no_cap_noise(push):
    cap(period="month")
    old = add(1200, TODAY - timedelta(days=5), "swiggy@icici")
    assert alerts.sweep("sms") == {"new": 2, "mode": "bulk"}
    got = states()
    assert got[f"bulk:{old['id']}-{old['id']}"] == "pending" and got["cap:merchant:Swiggy:month:2026-10-01:100"] == "skipped"
    alerts.deliver()
    assert len(push.calls) == 1 and "1 payment added at once" in rows()[0]["title"] + rows()[1]["title"]


def test_more_than_five_debits_is_bulk_but_five_is_not():
    cap(scope="total", target="all", period="month", amount=100000)
    for _ in range(5):
        add(10, TODAY, "chai")
    assert alerts.sweep("x")["mode"] == "realtime"
    for _ in range(6):
        add(10, TODAY, "tea")
    assert alerts.sweep("x")["mode"] == "bulk"


def test_a_40_row_import_gives_exactly_one_pushed_alert_and_a_later_sms_does_not_refire(push):
    cap(period="month")
    batch = []
    for n in range(40):
        batch.append(importer.StatementRow(txn_date=TODAY - timedelta(days=n % 3), amount=D(30), direction="debit",
                                           narration=f"UPI/DR/4291837465{n:02d}/SWIGGY/YESB/swiggy@yesbank/Pay", balance=None,
                                           ref=f"4291837465{n:02d}", line=n + 1))
    result = importer.import_rows(batch, "Main", "oct.csv")
    assert result.added == 40
    out = alerts.sweep("import")
    assert out["mode"] == "bulk"
    got = states()
    imports = [k for k in got if k.startswith("import:")]
    assert imports == [f"import:{result.batch_id}"] and got[imports[0]] == "pending"
    assert got["cap:merchant:Swiggy:month:2026-10-01:100"] == "skipped"
    assert [k for k in got if k.startswith("streak:")] and all(v == "skipped" for k, v in got.items() if k.startswith(("streak:", "cap:")))
    alerts.deliver()
    assert len(push.calls) == 1
    add(60, TODAY, "swiggy@icici")                                    # a real-time SMS afterwards
    assert alerts.sweep("sms") == {"new": 0, "mode": "realtime"}      # 100% was already written: nothing fires again
    alerts.deliver()
    assert len(push.calls) == 1


# ───────────── the gates ─────────────

def test_quiet_hours_hold_alerts_and_the_tick_sends_one_merged_push(clock, push):
    cap("Swiggy")
    cap("Zomato")
    clock.at("23:10")
    add(850, TODAY, "swiggy@icici")
    add(850, TODAY, "zomato@ptybl")
    assert alerts.sweep("sms")["new"] == 2
    assert set(states().values()) == {"held"}
    alerts.deliver()
    assert push.calls == []
    clock.at("08:00", day=22)
    out = alerts.tick()
    assert out["released"] == 2 and len(push.calls) == 1
    assert push.calls[0]["payload"]["t"] == "While you were asleep: 2 alerts" and push.calls[0]["payload"]["u"] == "/alerts"
    assert set(states().values()) == {"sent"}
    alerts.tick()
    assert len(push.calls) == 1                                       # not again


def test_still_quiet_nothing_is_released(clock, push):
    cap()
    clock.at("22:30")
    add(850, TODAY, "swiggy@icici")
    alerts.sweep("x")
    clock.at("23:59")
    assert alerts.tick()["released"] == 0 and push.calls == []


def test_cooldown_per_scope_and_a_100_percent_alert_bypasses_it(push):
    cap()
    add(520, TODAY, "swiggy@icici")                       # 52%
    run()
    add(300, TODAY, "swiggy@icici")                       # 82% -> same scope within 6 hours
    run()
    assert [v for k, v in states().items() if k.endswith(":80")] == ["skipped"]
    add(250, TODAY, "swiggy@icici")                       # 107%: the cap is blown, tell me anyway
    run()
    assert [v for k, v in states().items() if k.endswith(":100")] == ["sent"]
    assert len(push.calls) == 2


def test_cooldown_ends_after_the_configured_hours(clock, push):
    cap()
    add(520, TODAY, "swiggy@icici")
    run()
    clock.plus(hours=7)
    add(300, TODAY, "swiggy@icici")
    run()
    assert len(push.calls) == 2


def test_hourly_limit_skips_the_push_but_keeps_the_inbox(push):
    for name in ("Swiggy", "Zomato", "Blinkit", "Zepto", "Amazon"):
        cap(name)
        add(900, TODAY, name)
    assert alerts.sweep("x")["new"] == 5
    assert sorted(states().values()) == ["pending"] * 3 + ["skipped"] * 2          # max_per_hour = 3
    alerts.deliver()
    assert len(push.calls) == 3
    assert len(alerts.list_alerts("all")) == 5 and alerts.unread_count() == 5


def test_daily_limit(clock, push):
    alerts.save_prefs({"max_per_hour": 20, "max_per_day": 2})
    for name in ("Swiggy", "Zomato", "Blinkit"):
        cap(name)
        add(900, TODAY, name)
        alerts.sweep("x")
    assert sorted(states().values()) == ["pending", "pending", "skipped"]


def test_global_snooze_push_off_and_muted_kind_skip_the_push_not_the_inbox(clock, push):
    cap("Swiggy")
    cap("Zomato")
    cap("Blinkit")
    alerts.save_prefs({"snooze_until": (clock.now + timedelta(hours=2)).isoformat()})
    add(900, TODAY, "swiggy@icici")
    alerts.sweep("x")
    alerts.save_prefs({"snooze_until": None, "push_on": False})
    add(900, TODAY, "zomato@ptybl")
    alerts.sweep("x")
    alerts.save_prefs({"push_on": True, "muted_kinds": ["cap"]})
    add(900, TODAY, "blinkit@hdfc")
    alerts.sweep("x")
    assert set(states().values()) == {"skipped"} and len(rows()) == 3
    alerts.deliver()
    assert push.calls == []


def cap_rows():
    return [r for r in rows() if r["kind"] == "cap"]


def test_muting_a_rule_and_snoozing_a_rule_stop_new_alerts(clock):
    rule = cap("Swiggy")
    add(850, TODAY, "swiggy@icici")
    alerts.sweep("x")
    (first,) = rows()
    assert alerts.mute_alert(first["id"]) == {"muted": "rule", "rule_id": rule["id"]}
    add(200, TODAY, "swiggy@icici")                       # would cross 100
    alerts.sweep("x")
    assert len(cap_rows()) == 1
    alerts.update_rule(rule["id"], muted=False)
    add(10, TODAY, "swiggy@icici")
    alerts.sweep("x")
    assert len(cap_rows()) == 2                           # the 100% step fires once the rule is back
    newest = cap_rows()[-1]
    alerts.snooze_alert(newest["id"], 24)
    add(10, TODAY, "swiggy@icici")
    alerts.sweep("x")
    assert len(cap_rows()) == 2
    clock.plus(hours=25)
    add(10, TODAY, "swiggy@icici")
    alerts.sweep("x")
    assert len(cap_rows()) == 2                           # 100% was written already: still one alert per step


def test_muting_a_streak_mutes_that_merchant_only():
    for _ in range(5):
        add(100, TODAY, "swiggy@icici")
        add(100, TODAY, "zomato@ptybl")
    alerts.sweep("x")
    streak = [r for r in rows() if r["kind"] == "streak" and "Swiggy" in r["title"]][0]
    assert alerts.mute_alert(streak["id"]) == {"muted": "scope"}
    for _ in range(2):
        add(100, TODAY, "swiggy@icici")
        add(100, TODAY, "zomato@ptybl")
    alerts.sweep("x")
    keys = [r["dedupe_key"] for r in rows() if r["kind"] == "streak"]
    assert any("Zomato" in k and k.endswith(":7") for k in keys) and not any("Swiggy" in k and k.endswith(":7") for k in keys)


def test_budget_alerts_can_be_muted_per_budget():
    tools.set_budget("Groceries", 1000, "2026-10")
    tools.set_budget("Fuel", 1000, "2026-10")
    add(850, TODAY, "Dmart", "Groceries")
    alerts.sweep("x")
    (g,) = rows()
    alerts.mute_alert(g["id"])
    add(100, TODAY, "Dmart", "Groceries")
    add(850, TODAY, "Pump", "Fuel")
    alerts.sweep("x")
    assert [r["dedupe_key"] for r in rows()][1:] == ["cap:category:Fuel:month:2026-10-01:80"]


# ───────────── digest, retries, phones ─────────────

def test_digest_once_a_day_after_its_time(clock, push):
    alerts.save_prefs({"digest_on": True, "digest_time": "21:00", "spend_plan": "30000"})
    add(120, TODAY, "chai")
    alerts.tick()
    assert [r for r in rows() if r["kind"] == "digest"] == []                  # 12:00 is too early
    clock.at("21:30")
    alerts.tick()
    alerts.tick()
    digests = [r for r in rows() if r["kind"] == "digest"]
    assert len(digests) == 1 and digests[0]["dedupe_key"] == "digest:2026-10-21" and digests[0]["push_state"] == "sent"
    assert len(push.calls) == 1 and alerts.get_alert(digests[0]["id"])["numbers"]["day_total"] == D(120)


def test_no_digest_on_a_day_with_no_spending(clock):
    alerts.save_prefs({"digest_on": True})
    clock.at("21:30")
    alerts.tick()
    assert rows() == []


def test_a_gone_phone_is_deleted_and_the_alert_is_skipped(push):
    cap()
    push.answer = "gone"
    add(900, TODAY, "swiggy@icici")
    run()
    assert alerts.list_subscriptions() == [] and rows()[0]["push_state"] == "skipped"


def test_no_phone_saved_means_skipped():
    for sub in alerts.list_subscriptions():
        alerts.delete_subscription(sub["id"])
    cap()
    add(900, TODAY, "swiggy@icici")
    run()
    assert rows()[0]["push_state"] == "skipped"


def test_failed_pushes_retry_with_backoff_then_stop(clock, push):
    cap()
    push.answer = "retry"
    add(900, TODAY, "swiggy@icici")
    run()
    row = rows()[0]
    assert row["push_state"] == "failed" and row["push_attempts"] == 1 and row["next_try_at"] > clock.now
    alerts.tick()
    assert rows()[0]["push_attempts"] == 1                                       # not due yet
    for _ in range(6):
        clock.plus(hours=4)
        alerts.tick()
    row = rows()[0]
    assert row["push_state"] == "failed" and row["push_attempts"] == alerts.RETRY_MAX and row["next_try_at"] is None
    push.answer = "ok"
    clock.plus(hours=9)
    alerts.tick()
    assert rows()[0]["push_state"] == "failed"                                   # given up for good


def test_a_retry_that_works_marks_the_alert_sent(clock, push):
    cap()
    push.answer = "retry"
    add(900, TODAY, "swiggy@icici")
    run()
    push.answer = "ok"
    clock.plus(minutes=2)
    alerts.tick()
    assert rows()[0]["push_state"] == "sent"


def test_a_push_that_raises_never_breaks_the_caller_or_leaves_sending(push):
    cap()
    push.answer = RuntimeError("boom")
    add(900, TODAY, "swiggy@icici")
    assert alerts.sweep_and_deliver("sms")["mode"] == "realtime"
    assert rows()[0]["push_state"] == "failed"


def test_sweep_and_deliver_swallows_a_broken_database_step(monkeypatch, caplog):
    monkeypatch.setattr(alerts, "sweep", lambda origin: (_ for _ in ()).throw(ValueError("secret detail")))
    caplog.set_level(logging.INFO)
    assert alerts.sweep_and_deliver("sms") == {"new": 0, "mode": "error"}
    assert "ValueError" in caplog.text and "secret detail" not in caplog.text


# ───────────── what leaves the laptop ─────────────

def test_payload_is_masked_and_small(push):
    alerts_merchant = __import__("rectifi.merchants", fromlist=["x"])
    alerts_merchant.add_pattern("corner shop", "Shop 9876543210 @ybl", "Other")
    cap("Shop 9876543210 @ybl")
    add(900, TODAY, "corner shop ramesh@okaxis")
    run()
    (call,) = push.calls
    body = call["payload"]
    text = " ".join(str(v) for v in body.values())
    assert "@" not in text and not re.search(r"(?<![₹\d,.])\d{4,}", text) and len(call["raw"]) <= 2048
    assert set(body) <= {"t", "b", "u", "tag"} and body["u"].startswith("/alerts#")
    assert "₹900" in body["b"]                                                    # amounts stay readable


def test_private_notifications_send_only_a_generic_text(push):
    alerts.save_prefs({"private_notifications": True})
    cap()
    add(900, TODAY, "swiggy@icici")
    run()
    body = push.calls[0]["payload"]
    assert body["t"] == "Rectifi" and body["b"] == "New spending alert" and "₹" not in str(body) and "Swiggy" not in str(body["t"] + body["b"])
    assert alerts.get_alert(rows()[0]["id"])["title"].startswith("Swiggy")     # the inbox keeps the full text


def test_payload_never_holds_a_upi_ref_or_last4(push):
    cap(scope="total", target="all", period="day", amount=100)
    tools.add_transaction("500", "debit", TODAY, payee="swiggy@icici", account="Main", source="sms", upi_ref="429183746521",
                          raw_text="Rs.500 debited from A/c XX1234 to swiggy@icici UPI Ref No 429183746521")
    run()
    raw = push.calls[0]["raw"].decode()
    assert "429183746521" not in raw and "1234" not in raw and "icici" not in raw


def test_test_push_goes_to_every_phone(push):
    add_phone("Second", 2)
    out = alerts.send_test()
    assert out["devices"] == 2 and out["ok"] == 2 and push.calls[0]["payload"]["t"] == "Rectifi test"
    assert rows() == []                                                            # not an alert
