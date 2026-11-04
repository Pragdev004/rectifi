"""WS-ALERTS (multi-user): per-person locks and watermarks, the ticker over people, signed-out alerts from a plain
merchant_key, sealed alert text, custom merchants at the next sign-in, and pushes that stay with their owner.

Multi-user mode is switched on per test. People come from tests/support/tenants.py; the phone is the gateway from WS-CORE.
"""

import json
import re

import httpx
import pytest
from alerts_support import TODAY, Clock, add_phone
from starlette.testclient import TestClient
from support.tenants import as_user, make_user

from rectifi import alerts, db, hooks, merchant_allowlist, merchants, sms_gateway, tools, userctx, vault, webpush

STAMP = 1792573200000


@pytest.fixture(autouse=True)
def world(monkeypatch):
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setattr(tools, "today", lambda: TODAY)
    monkeypatch.setattr(alerts, "_clock", Clock())


class Recorder:
    """Replaces webpush.send: remembers which phone (by endpoint) got which text. Never prints an endpoint."""

    def __init__(self, monkeypatch):
        self.sent = []
        self.answer = {}                                   # endpoint -> verdict; default 'ok'
        monkeypatch.setattr(webpush, "send", self)

    def __call__(self, sub, payload, ttl=3600, urgency="normal", client=None):
        self.sent.append({"endpoint": sub["endpoint"], "text": payload.decode() if isinstance(payload, bytes) else payload})
        return self.answer.get(sub["endpoint"], "ok")

    def to(self, endpoint):
        return [s["text"] for s in self.sent if s["endpoint"] == endpoint]


@pytest.fixture
def push(monkeypatch):
    return Recorder(monkeypatch)


@pytest.fixture
def client():
    return TestClient(sms_gateway.create_app())


def sms(amount, vpa, ref):
    return f"Rs.{amount}.00 debited from A/c XX1234 on 21-10-26 to VPA {vpa} UPI Ref No {ref}"


def post(client, token, text, n):
    return client.post("/sms", headers={"Authorization": f"Bearer {token}"}, json={"from": "AX-HDFCBK", "text": text, "sentStamp": STAMP + n})


def person(name):
    """A friend with an account, a paired phone (device token) and one push subscription."""
    ctx = make_user(name, encrypted=True)
    with as_user(ctx):
        tools.add_account("Savings", "1234")
        device = tools.create_device("Pixel")
        sub = add_phone(n=ctx.uid)
        endpoint = db_endpoint(sub["id"])
    return ctx, device, endpoint


def db_endpoint(sub_id):
    with db.connect() as conn:
        return conn.execute("SELECT endpoint FROM push_subscriptions WHERE id = %s", (sub_id,)).fetchone()["endpoint"]


def locked(ctx):
    """The same person with no data key: what the gateway and the ticker bind."""
    return userctx.UserContext(uid=ctx.uid, key=None, public_key=ctx.public_key, ingest_salt=ctx.ingest_salt, strict=False)


def raw(who, sql, params=()):
    from psycopg.rows import dict_row

    with as_user(who), db.connect() as conn:
        conn.row_factory = dict_row                        # exactly as stored: envelopes are NOT opened
        return conn.execute(sql, params).fetchall()


def keys(who):
    return [r["dedupe_key"] for r in raw(who, "SELECT dedupe_key FROM alerts ORDER BY id")]


# ───────────────────────── locks and watermarks ─────────────────────────

def test_two_peoples_sweeps_do_not_block_each_other_or_share_a_watermark():
    a, b = make_user("a", encrypted=True), make_user("b", encrypted=True)
    with as_user(locked(a)):
        alerts.sweep("baseline")
    with as_user(a), db.connect() as holder:                                    # A's sweep is "running": its lock is held
        holder.execute("SELECT pg_advisory_xact_lock(%s, %s)", (alerts.SWEEP_LOCK_ID, a.uid))
        with as_user(locked(b)):
            assert alerts.sweep("tick")["mode"] != "busy"                       # B is not held up
        with as_user(locked(a)):
            assert alerts.sweep("tick")["mode"] == "busy"                       # A's own second sweep waits its turn
    marks = {r["user_id"]: r["value"] for r in _all_marks()}
    assert set(marks) == {a.uid, b.uid}                                         # each person has a watermark of their own


def _all_marks():
    with db.connect_system("test: read watermarks") as conn:                    # no RLS row for tenant tables here; read per person instead
        ids = [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id").fetchall()]
    out = []
    for uid in ids:
        for r in raw(uid, "SELECT user_id, value FROM app_settings WHERE key = %s", (alerts.WATERMARK_KEY,)):
            out.append(r)
    return out


def test_a_watermark_is_made_lazily_at_a_persons_first_sweep_and_never_needs_a_bound_person_at_start():
    a, b = make_user("a", encrypted=True), make_user("b", encrypted=True)
    assert raw(a, "SELECT 1 FROM app_settings WHERE key = %s", (alerts.WATERMARK_KEY,)) == []
    with as_user(locked(a)):
        alerts.sweep("tick")
    assert len(raw(a, "SELECT 1 FROM app_settings WHERE key = %s", (alerts.WATERMARK_KEY,))) == 1
    assert raw(b, "SELECT 1 FROM app_settings WHERE key = %s", (alerts.WATERMARK_KEY,)) == []     # B's is not made by A's sweep
    with pytest.raises(userctx.NoUser):
        alerts.ensure_watermark()                                               # unbound in multi-user mode: fails closed, as it should


def test_the_tick_loops_the_people_without_a_bound_person_and_each_gets_their_own_alerts(push):
    (a, _, ea), (b, _, eb) = person("a")[0:3], person("b")[0:3]
    for who in (a, b):
        with as_user(locked(who)):
            alerts.sweep("baseline")
        with as_user(who):
            alerts.create_rule("total", "all", "day", 100)
            tools.add_transaction("150", "debit", TODAY, payee="Chaayos", account="Savings", source="manual")
    out = alerts.tick()                                                          # nobody is bound here
    assert out["people"] == 2 and out["failed"] == 0 and out["sweep"]["new"] == 2
    for who, mine, other in ((a, ea, eb), (b, eb, ea)):
        assert any(k.startswith("cap:total:all:day") for k in keys(who))
        assert len(push.to(mine)) == 1 and push.to(mine)[0]
    assert alerts.tick()["sweep"]["new"] == 0                                    # nothing new the second minute


def test_one_persons_failure_does_not_stop_the_next(monkeypatch, push):
    a, b = make_user("a", encrypted=True), make_user("b", encrypted=True)
    real = alerts._tick_one

    def flaky(client=None):
        if userctx.current().uid == a.uid:
            raise RuntimeError("boom")
        return real(client)

    monkeypatch.setattr(alerts, "_tick_one", flaky)
    out = alerts.tick()
    assert out["failed"] == 1 and out["people"] == 1


def test_the_ticker_is_started_and_stopped_by_the_web_apps_start_up_without_a_bound_person(monkeypatch):
    from rectifi.api import app as web

    monkeypatch.delenv("RECTIFI_NO_ALERT_BACKGROUND", raising=False)
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "s" * 40)                      # multi-user mode needs it to start
    monkeypatch.setattr(web, "TICK_SECONDS", 0.05)
    calls = []
    monkeypatch.setattr(alerts, "tick", lambda client=None: calls.append(1) or {})
    monkeypatch.setattr(alerts, "ensure_watermark", lambda *a, **k: (_ for _ in ()).throw(AssertionError("start-up must not build a watermark")))
    import time

    with TestClient(web.create_app()):
        time.sleep(0.4)
    assert calls                                                                 # the ticker ran, and start-up raised no NoUser


# ───────────────────────── signed out: a brand drives its merchant cap and streak ─────────────────────────

def test_a_signed_out_sms_to_a_brand_fires_its_merchant_cap_and_streak(client, push):
    friend, device, endpoint = person("friend")
    with as_user(friend):
        alerts.create_rule("merchant", "Swiggy", "week", 1000)
    with as_user(locked(friend)):
        alerts.sweep("baseline")
    for n in range(5):                                                           # five Swiggy orders of Rs 100 arrive while signed out
        assert post(client, device["token"], sms(100, "swiggy@icici", f"42918374652{n}"), n).json() == {"status": "added"}
    got = keys(friend)
    assert any(k.startswith("cap:merchant:Swiggy:week:") and k.endswith(":50") for k in got), got       # 500 of 1000
    assert any(k.startswith("streak:Swiggy:") and k.endswith(":5") for k in got), got                   # the 5th order
    texts = " ".join(push.to(endpoint))
    assert "Swiggy" in texts and "₹500" in texts                                                       # a brand and an amount may be in a push
    [row] = raw(friend, "SELECT title, detail FROM alerts WHERE dedupe_key LIKE 'streak:%%'")
    assert row["title"].startswith("rs1.") and row["detail"].startswith("rs1.")                        # sealed to the public key


def test_the_registry_covers_every_allow_listed_brand_and_agrees_with_the_allow_list():
    assert set(merchants.BRANDS) == set(merchant_allowlist.KEYS)
    for brand in merchant_allowlist.ALLOWED:
        name, group = merchants.BRANDS[brand.key]
        assert group in merchants.GROUPS and merchants.is_plain(name)
        assert name in {n for _, n, _ in merchants.REGISTRY}                        # a cap can be set on it (known_merchants reads the registry)
        for spelling in (brand.key, brand.name, *brand.aliases):
            assert merchant_allowlist.plain_key(spelling) == brand.key, spelling
            found = merchants.match(spelling, user=[]) or merchants.by_key(merchant_allowlist.plain_key(spelling))
            assert found is not None and found.name == name, (spelling, found)
    assert merchants.match("COLA HOUSE", user=[]) is None and merchants.match("olam foods", user=[]) is None      # a short name is a whole word
    assert merchants.match("ola cabs", user=[]) is not None and merchants.match("uber trip", user=[]) is not None


def test_a_row_with_only_a_plain_merchant_key_has_its_merchant_in_the_alert_context():
    friend, _, _ = person("friend")
    with as_user(friend):
        tools.add_transaction("120", "debit", TODAY, payee="Uber India", account="Savings", source="manual")
    with as_user(locked(friend)):
        ctx = alerts.build_context()
    [row] = [r for r in ctx.spend]
    assert row["payee"] is None and row["description"] is None                  # sealed, unreadable signed out
    assert (row["merchant"], row["grp"]) == ("Uber", merchants.OTHER)


# ───────────────────────── signed out: any other payee fires only category and total caps ─────────────────────────

def _chai_setup(client):
    friend, device, endpoint = person("friend")
    with as_user(friend):
        merchants.add_pattern("chai point", "Chaayos", "Other")                  # a custom merchant: sealed
        alerts.create_rule("merchant", "Chaayos", "day", 50)                     # its cap: sealed, waits for sign-in
    with as_user(locked(friend)):
        alerts.sweep("baseline")
    assert post(client, device["token"], "Rs.40.00 debited from A/c XX1234 on 21-10-26 to chai point", 1).json() == {"status": "added"}
    [first] = raw(friend, "SELECT c.name AS category FROM transactions t JOIN categories c ON c.id = t.category_id")
    with as_user(friend):
        alerts.create_rule("category", first["category"], "day", 50)
        alerts.create_rule("total", "all", "day", 50)
    assert post(client, device["token"], "Rs.45.00 debited from A/c XX1234 on 21-10-26 to chai point", 2).json() == {"status": "added"}
    return friend, device, endpoint, first["category"]


def test_a_signed_out_sms_to_another_payee_fires_only_category_and_total_with_no_payee_text(client, push):
    friend, _, endpoint, category = _chai_setup(client)
    got = keys(friend)
    assert any(k.startswith(f"cap:category:{category}:day:") for k in got) and any(k.startswith("cap:total:all:day:") for k in got), got
    assert not any(k.startswith("cap:merchant") or k.startswith("streak") for k in got)       # the sealed merchant cap stays quiet
    every_cell = json.dumps(raw(friend, "SELECT dedupe_key, scope_key, push_title, push_body, numbers FROM alerts"), default=str).lower()
    pushed = " ".join(push.to(endpoint)).lower()
    for text in (every_cell, pushed):
        assert "chai" not in text and "chaayos" not in text and "point" not in text
    assert pushed                                                                # and a push really went out
    rows = raw(friend, "SELECT title, detail FROM alerts")
    assert rows and all(r["title"].startswith("rs1.") and r["detail"].startswith("rs1.") for r in rows)


def test_a_custom_merchant_cap_is_sealed_and_a_brand_cap_is_not():
    friend, _, _ = person("friend")
    with as_user(friend):
        merchants.add_pattern("chai point", "Chaayos", "Other")
        alerts.create_rule("merchant", "Chaayos", "week", 300)
        alerts.create_rule("merchant", "Swiggy", "week", 300)
        alerts.create_rule("category", "Groceries", "week", 300)
        got = {r["target"] for r in alerts.list_rules()}
    assert got == {"Chaayos", "Swiggy", "Groceries"}                             # read back through the code: plain text
    stored = {r["scope"] + ":" + r["target"][:4]: r["target_blind"] for r in raw(friend, "SELECT scope, target, target_blind FROM alert_rules")}
    assert stored["merchant:rx1."].startswith("b1:")                              # the custom one: sealed, keyed hash
    assert stored["merchant:Swig"] == "Swiggy" and stored["category:Groc"] == "Groceries"
    [pattern] = raw(friend, "SELECT pattern, merchant FROM merchant_patterns")
    assert pattern["pattern"].startswith("rx1.") and pattern["merchant"].startswith("rx1.")
    with as_user(friend):
        alerts.create_rule("merchant", "Chaayos", "week", 400)                    # the same cap again changes it, no second row
    assert len(raw(friend, "SELECT 1 FROM alert_rules WHERE scope = 'merchant'")) == 2


def test_a_signed_out_job_reads_a_sealed_rule_as_nothing_instead_of_failing():
    friend, _, _ = person("friend")
    with as_user(friend):
        merchants.add_pattern("chai point", "Chaayos", "Other")
        alerts.create_rule("merchant", "Chaayos", "day", 50)
    with as_user(locked(friend)):
        ctx = alerts.build_context()
        assert ctx.rules == [] and alerts.active_caps(ctx) == []


# ───────────────────────── custom merchants at the next sign-in ─────────────────────────

def test_a_custom_merchant_cap_fires_at_the_next_sign_in_with_no_name_in_the_push(client, push):
    friend, _, endpoint, _ = _chai_setup(client)
    assert not any(k.startswith("cap:merchant") for k in keys(friend))
    hooks.after_sign_in(friend)                                                  # sign-in: unlock the SMS rows, then catch up
    custom = [k for k in keys(friend) if k.startswith("cap:merchant")]
    assert len(custom) == 1 and custom[0].endswith(":100") and "b1:" in custom[0] and "haayos" not in custom[0]
    hooks.after_sign_in(friend)                                                  # a second sign-in writes nothing new
    assert len([k for k in keys(friend) if k.startswith("cap:merchant")]) == 1
    with as_user(friend):
        titles = [a["title"] for a in alerts.list_alerts("all")]
        assert any("Chaayos" in t for t in titles)                               # the person reads the name; it is sealed at rest
    row = raw(friend, "SELECT title, scope_key, push_title, push_body FROM alerts WHERE dedupe_key LIKE 'cap:merchant%%'")[0]
    assert row["title"].startswith("rx1.") and "haayos" not in (row["scope_key"] + row["push_title"] + row["push_body"])
    assert row["push_title"] == "A merchant cap: 100% of daily cap"
    alerts.tick()                                                                # the next minute pushes it
    assert not any("haayos" in t for t in push.to(endpoint))
    assert any("A merchant cap" in t for t in push.to(endpoint))


def test_sign_in_opens_the_texts_that_were_sealed_to_the_public_key(client, push):
    friend, _, _, _ = _chai_setup(client)
    assert all(r["title"].startswith("rs1.") for r in raw(friend, "SELECT title FROM alerts"))
    hooks.after_sign_in(friend)
    assert all(r["title"].startswith("rx1.") and r["detail"].startswith("rx1.") for r in raw(friend, "SELECT title, detail FROM alerts"))


def test_a_custom_streak_at_sign_in_names_no_merchant_in_its_keys(client, push):
    friend, device, endpoint = person("friend")
    with as_user(friend):
        merchants.add_pattern("chai point", "Chaayos", "Other")
    with as_user(locked(friend)):
        alerts.sweep("baseline")
    for n in range(5):
        post(client, device["token"], f"Rs.{20 + n}.00 debited from A/c XX1234 on 21-10-26 to chai point", n)
    assert keys(friend) == []                                                    # nothing signed out: the payee was sealed
    hooks.after_sign_in(friend)
    [streak] = [r for r in raw(friend, "SELECT dedupe_key, scope_key, push_title FROM alerts") if r["dedupe_key"].startswith("streak")]
    assert "b1:" in streak["dedupe_key"] and "haayos" not in json.dumps(streak) and streak["push_title"] == "5th order from one merchant this week"


# ───────────────────────── pushes stay with their owner ─────────────────────────

def test_one_persons_alert_never_reaches_another_persons_phone(push):
    (a, _, ea), (b, _, eb) = person("a")[0:3], person("b")[0:3]
    with as_user(locked(a)):
        alerts.sweep("baseline")
    with as_user(a):
        alerts.create_rule("total", "all", "day", 100)
        tools.add_transaction("500", "debit", TODAY, payee="Chaayos", account="Savings", source="manual")
        alerts.sweep_and_deliver("manual")
    assert push.to(ea) and push.to(eb) == []
    with as_user(locked(b)):                                                     # B's tick: nothing of A's is B's to send
        alerts.deliver()
    assert push.to(eb) == []


def test_a_410_deletes_only_that_persons_subscription():
    a, b = make_user("a", encrypted=True), make_user("b", encrypted=True)
    ends = {}
    for who in (a, b):
        with as_user(who):
            tools.add_account("Savings", "1234")
            ends[who.uid] = db_endpoint(add_phone(n=who.uid)["id"])

    def handler(request):
        return httpx.Response(410 if str(request.url) == ends[a.uid] else 201)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with as_user(locked(a)):
        assert alerts._push_all("t", "b", "/alerts", None, client=client) == {"devices": 1, "ok": 0, "gone": 1, "retry": 0}
    assert raw(a, "SELECT 1 FROM push_subscriptions") == []                       # A's phone is gone
    assert len(raw(b, "SELECT 1 FROM push_subscriptions")) == 1                   # B's is untouched
    with as_user(locked(b)):
        assert alerts._push_all("t", "b", "/alerts", None, client=client)["ok"] == 1


def test_a_subscription_label_is_sealed_and_the_endpoint_never_leaves():
    friend, _, endpoint = person("friend")
    [row] = raw(friend, "SELECT label, endpoint FROM push_subscriptions")
    assert row["label"].startswith("rx1.") and row["endpoint"] == endpoint
    with as_user(friend):
        [pub] = alerts.list_subscriptions()
    assert pub["label"] == "Pixel" and endpoint not in json.dumps(pub, default=str)


def test_alert_text_is_sealed_when_signed_in_and_read_back_through_the_code(push):
    friend, _, _ = person("friend")
    with as_user(friend):
        alerts.create_rule("total", "all", "day", 100)
        tools.add_transaction("500", "debit", TODAY, payee="Chaayos", account="Savings", source="manual")
        alerts.sweep_and_deliver("manual")
        [a] = [a for a in alerts.list_alerts("all") if a["kind"] == "cap"]
    assert "All spending" in a["title"]
    [row] = raw(friend, "SELECT title, detail, push_title, push_body FROM alerts")
    assert row["title"].startswith("rx1.") and row["detail"].startswith("rx1.")
    assert not vault.is_envelope(row["push_title"]) and not vault.is_envelope(row["push_body"])


def test_every_rupee_figure_in_a_push_is_in_the_numbers_even_for_a_custom_merchant(client, push):
    friend, _, endpoint, _ = _chai_setup(client)
    hooks.after_sign_in(friend)
    rows = raw(friend, "SELECT push_title, push_body, numbers FROM alerts WHERE kind IN ('cap', 'streak')")
    assert rows
    for r in rows:
        shown = {int(x.replace(",", "")) for x in re.findall(r"₹([\d,]+)", r["push_title"] + " " + r["push_body"])}
        known = {int(float(v)) for v in r["numbers"].values()}
        assert shown <= known
