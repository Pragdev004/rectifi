"""WS-PROOF: can one friend reach another friend's data through any route? (docs/MULTIUSER_PLAN.md section 8)

Written from the plan alone. A and B are two friends who signed up with invites. A is seeded through the real routes with a
unique canary word in every text field and a distinctive amount in every money field (A's row ids all start at 7,000,000 so an id
cannot be mistaken for a number of rows). Then B calls every route with A's real ids.

For each route as B: no answer is a server error; the body holds no A canary, no A id, no A amount; A's tables have the same
checksum afterwards. A route that needs a session gives 401 without a cookie. An owner-only route gives B 403.
A meta-test reads api_routes() and fails when a route has neither a case here nor a place on the short public list, so a route
added later cannot dodge this file.

Expected red until the streams are merged. Where the plan leaves a detail open the assumption lives in tests/support/proof.py.
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass

import pytest
from support import proof

# Public on purpose: they hold no data and are how a person gets in. Everything else needs a session.
PUBLIC: set[tuple[str, str]] = {
    ("GET", "/api/health"), ("GET", "/api/auth/status"), ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/signup"), ("POST", "/api/auth/signup/confirm"), ("POST", "/api/auth/recover"), ("GET", "/api/privacy"),
}

# Path parameter -> the id of A's thing it should carry. Owner-only routes get stand-in values: the gate answers before the lookup.
PARAMS = {
    "txn_id": "txn", "account_id": "account", "batch_id": "batch", "device_id": "device", "item_id": "inbox", "alert_id": "alert",
    "rule_id": "rule", "pattern_id": "pattern", "sub_id": "sub",
}
STAND_IN = {"thread_id": "proof-thread", "chart_id": "1", "memory_id": "1", "run_id": "proof-run", "doc_id": "1", "job_id": "1"}


@dataclass
class Case:
    method: str
    path: str
    kind: str                       # by_id: refers to A's data. self: acts on B's own data. owner: owner-only, B must get 403.
    body: object = None             # dict, or f(ids, a, b) -> dict
    query: dict | None = None
    upload: bool = False            # multipart statement upload
    fresh: bool = False             # sign B in again first (the call ends sessions or the account)
    empty: bool = False             # the answer must be an empty list
    last: bool = False
    note: str = ""


CASES: list[Case] = []


def add(kind: str, method: str, path: str, body=None, **kw) -> None:
    CASES.append(Case(method, path, kind, body, **kw))


# ───────────────────────── the case table: one line per route ─────────────────────────
SMS_B = proof.sms_debit("11.00", "bobonlyvpa")

add("self", "GET", "/api/meta")
add("self", "GET", "/api/overview")
add("self", "GET", "/api/review")
add("self", "PUT", "/api/review/commitment", {"text": "bob only promise", "week": "2026-10-19"})
add("self", "GET", "/api/insights")
add("self", "POST", "/api/insights/apply", {"key": "no-such-insight"})
add("owner", "POST", "/api/insights/explain", {"key": "x"})
add("self", "GET", "/api/transactions")
add("self", "GET", "/api/transactions", query=lambda i, a, b: {"search": a.canaries["payee"][0]}, empty=True, note="B searches for A's payee")
add("self", "GET", "/api/transactions", query=lambda i, a, b: {"account": str(i["account"])}, empty=True, note="B filters on A's account")
add("by_id", "POST", "/api/transactions", lambda i, a, b: {"amount": "11.00", "direction": "debit", "date": "2026-10-05", "payee": "bob", "account_id": i["account"]},
    note="B adds a payment to A's account")
add("by_id", "PATCH", "/api/transactions/{txn_id}", {"note": "hijacked", "category": "Shopping"})
add("by_id", "DELETE", "/api/transactions/{txn_id}")
# Deterministic (no model), so friends may call it (FRIEND_ROUTES); A's id must still be refused for B and change nothing of A's.
add("by_id", "POST", "/api/transactions/{txn_id}/apply-similar", {})
add("self", "POST", "/api/sms/preview", {"text": SMS_B})
add("self", "POST", "/api/sms/save", {"text": SMS_B})
add("by_id", "GET", "/api/inbox/{item_id}/prefill")
add("self", "POST", "/api/imports", upload=True)
add("by_id", "POST", "/api/imports/preview", lambda i, a, b: {"upload_id": i["upload"], "mapping": i["upload_mapping"]}, note="A's upload id used by B")
add("by_id", "POST", "/api/imports/commit", lambda i, a, b: {"upload_id": i["upload"], "account_id": i["account2"], "mapping": i["upload_mapping"]},
    note="A's upload id and account used by B")
add("by_id", "GET", "/api/imports/{batch_id}")
add("by_id", "POST", "/api/imports/{batch_id}/undo")
add("by_id", "GET", "/api/imports/{batch_id}/rows")
add("self", "GET", "/api/budgets")
add("self", "PUT", "/api/budgets", {"month": "2026-10", "category": "Food & Dining", "limit": "5555.55"})
add("self", "POST", "/api/budgets/copy", {"from": "2026-10", "to": "2026-11"})
add("self", "GET", "/api/accounts")
add("self", "POST", "/api/accounts", {"name": "Bob's wallet", "last4": "9090"})
add("by_id", "POST", "/api/accounts/{account_id}/balances", {"as_of": "2026-10-02", "balance": "1.00"})
add("self", "GET", "/api/phone/status")
add("self", "GET", "/api/phone/devices")
add("self", "POST", "/api/phone/devices", {"name": "Bob's phone"})
add("by_id", "POST", "/api/phone/devices/{device_id}/revoke")
add("self", "POST", "/api/phone/test", {"token": "rectifi_not_a_real_token"})
add("self", "GET", "/api/phone/inbox")
add("by_id", "POST", "/api/phone/inbox/{item_id}/dismiss")
add("self", "GET", "/api/leaks")
add("self", "GET", "/api/alerts")
add("self", "GET", "/api/alerts", query={"status": "all"})
add("self", "GET", "/api/alerts/today")
add("self", "POST", "/api/alerts/read-all")
add("self", "GET", "/api/alerts/presets")
add("self", "GET", "/api/alerts/prefs")
add("self", "PUT", "/api/alerts/prefs", {"push_on": False})
add("self", "GET", "/api/alerts/rules")
add("self", "POST", "/api/alerts/rules", {"scope": "total", "target": "all", "period": "month", "cap": "4321.00"})
add("by_id", "PATCH", "/api/alerts/rules/{rule_id}", {"cap": "1.00"})
add("by_id", "DELETE", "/api/alerts/rules/{rule_id}")
add("by_id", "POST", "/api/alerts/{alert_id}/read")
add("by_id", "POST", "/api/alerts/{alert_id}/mute")
add("by_id", "POST", "/api/alerts/{alert_id}/snooze", {"hours": 24})
add("owner", "POST", "/api/alerts/{alert_id}/explain")
add("self", "GET", "/api/merchants/patterns")
add("self", "POST", "/api/merchants/patterns", {"pattern": "bobtea", "merchant": "Bob Tea", "group": "Other"})
add("by_id", "DELETE", "/api/merchants/patterns/{pattern_id}")
add("self", "GET", "/api/push/key")
add("self", "GET", "/api/push/subscriptions")
add("self", "POST", "/api/push/subscriptions", lambda i, a, b: proof.push_body("Bob's phone"))
add("by_id", "DELETE", "/api/push/subscriptions/{sub_id}")
add("self", "POST", "/api/push/test")

# AI and library: owner only this phase (decision D7). Stand-in ids; the gate must answer 403 before any lookup.
for _method, _path in [
    ("GET", "/api/ask/status"), ("POST", "/api/ask/threads"), ("GET", "/api/ask/threads/{thread_id}"), ("POST", "/api/ask/threads/{thread_id}/messages"),
    ("POST", "/api/classify/jobs"), ("GET", "/api/classify/jobs/{job_id}"),
    ("GET", "/api/charts/status"), ("GET", "/api/charts"), ("POST", "/api/charts/draw"), ("POST", "/api/charts/threads/{thread_id}/resume"),
    ("POST", "/api/charts/threads/{thread_id}/retry"), ("GET", "/api/charts/{chart_id}"), ("DELETE", "/api/charts/{chart_id}"),
    ("POST", "/api/charts/{chart_id}/pin"), ("POST", "/api/charts/{chart_id}/rerun"),
    ("GET", "/api/chat/status"), ("GET", "/api/chat/threads"), ("POST", "/api/chat/threads"), ("GET", "/api/chat/threads/{thread_id}"),
    ("PATCH", "/api/chat/threads/{thread_id}"), ("DELETE", "/api/chat/threads/{thread_id}"), ("POST", "/api/chat/threads/{thread_id}/messages"),
    ("POST", "/api/chat/threads/{thread_id}/stop"), ("POST", "/api/chat/threads/{thread_id}/regenerate"), ("POST", "/api/chat/threads/{thread_id}/resume"),
    ("GET", "/api/memory"), ("POST", "/api/memory"), ("DELETE", "/api/memory"), ("DELETE", "/api/memory/rules/{rule_id}"),
    ("PATCH", "/api/memory/{memory_id}"), ("DELETE", "/api/memory/{memory_id}"),
    ("GET", "/api/traces"), ("DELETE", "/api/traces"), ("GET", "/api/traces/{run_id}"),
    ("GET", "/api/models/status"), ("POST", "/api/models/probe"),
    ("GET", "/api/knowledge/docs"), ("POST", "/api/knowledge/docs"), ("DELETE", "/api/knowledge/docs/{doc_id}"),
    ("GET", "/api/knowledge/status"), ("POST", "/api/knowledge/reindex"),
]:
    add("owner", _method, _path, {})

# Account and session (plan section 4). They act on B's own account, so they run last and sign B in again first.
add("self", "GET", "/api/account/export", last=True)
add("self", "POST", "/api/auth/recovery-code", lambda i, a, b: {"password": b.password}, fresh=True, last=True)
add("self", "POST", "/api/auth/logout-all", fresh=True, last=True)
add("self", "POST", "/api/auth/change-password", lambda i, a, b: {"current": b.password, "new": b.password + " again"}, fresh=True, last=True)
add("self", "DELETE", "/api/account", lambda i, a, b: {"password": b.password, "confirm_username": b.username}, fresh=True, last=True,
    note="B deletes B; A must not notice")


# ───────────────────────── helpers ─────────────────────────

def resolve(path: str, ids: dict) -> str:
    def fill(match):
        name = match.group(1)
        return str(ids[PARAMS[name]]) if name in PARAMS else STAND_IN.get(name, "1")

    return re.sub(r"\{(\w+)\}", fill, path)


def key_of(case: Case) -> tuple[str, str]:
    return case.method, case.path


def route_keys() -> set[tuple[str, str]]:
    from rectifi.api.app import api_routes

    keys = set()
    for route in api_routes():
        path = "/api" + re.sub(r"\{(\w+):\w+\}", r"{\1}", route.path)
        for method in route.methods - {"HEAD", "OPTIONS"}:
            keys.add((method, path))
    return keys


def body_text(reply) -> str:
    """The answer as text. A ZIP (the account export) is opened and every file read."""
    data = reply.content or b""
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                return "\n".join(bundle.read(n).decode("utf-8", errors="replace") for n in bundle.namelist())
        except zipfile.BadZipFile:
            pass
    return data.decode("utf-8", errors="replace")


def leaks(text: str, a: proof.Person, request_text: str) -> list[str]:
    """What of A's is in `text`: canaries, amounts, ids (not an id B itself put in the request: an error may echo it)."""
    found = []
    lowered = text.lower()
    for label, (word, _) in a.canaries.items():
        if word in lowered:
            found.append(f"canary {label}")
    if re.search(rf"\b{re.escape(a.username)}\b", lowered):
        found.append("A's username")
    plain = text.replace(",", "")
    for amount in a.amounts:
        if re.search(rf"(?<![\d.]){re.escape(amount)}(?!\d)", plain):
            found.append(f"amount {amount}")
    for name, value in a.ids.items():
        token = str(value)
        if isinstance(value, dict) or token in request_text:
            continue
        if (token.isdigit() and int(token) >= 1000) or name == "upload":
            if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text):
                found.append(f"id {name}={token}")
    return found


def send(client, case: Case, a: proof.Person, b: proof.Person):
    url = resolve(case.path, a.ids)
    body = case.body(a.ids, a, b) if callable(case.body) else case.body
    query = case.query(a.ids, a, b) if callable(case.query) else case.query
    if case.upload:
        data = proof.statement_csv([("03/10/26", "UPI-BOBONLY-bobonly@icici-ICIC0000103-429183746588-PAYMENT", "0000429183746588", "13.01")])
        reply = proof.upload_statement(client, "bob-statement.csv", data)
        return reply, f"{url} bob-statement.csv"
    kwargs = {"params": query} if query else {}
    if body is not None or case.method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = body if body is not None else {}
    reply = client.request(case.method, url, **kwargs)
    return reply, f"{url} {query or ''} {body or ''}"


def _world(monkeypatch, alert):
    app, gw, a, b, rec = proof.build_world(monkeypatch, alert=alert)
    return type("World", (), {"app": app, "gw": gw, "a": a, "b": b, "push": rec})


@pytest.fixture
def world(monkeypatch):
    """A and B, A seeded completely (with one alert), B with an account, a payment, a budget and a promise."""
    return _world(monkeypatch, True)


@pytest.fixture
def quiet_world(monkeypatch):
    """The same, but A has no alert yet: for the tests that make one on purpose."""
    return _world(monkeypatch, False)


# ───────────────────────── the meta-tests ─────────────────────────

def test_every_route_has_a_case_or_is_public():
    covered = {key_of(c) for c in CASES} | PUBLIC
    missing = sorted(k for k in route_keys() if k not in covered)
    assert not missing, ("These routes have no case in tests/test_isolation_routes.py and are not on the public list. Add a case "
                         "(or, if it really is public, add it to PUBLIC with a reason):\n  " + "\n  ".join(f"{m} {p}" for m, p in missing))


def test_every_case_names_a_real_route():
    """Catches a typo in this table, and a planned route (account export, privacy, sign-up) that never got built."""
    known = route_keys()
    wanted = {key_of(c) for c in CASES} | PUBLIC
    gone = sorted(k for k in wanted if k not in known)
    assert not gone, "These cases or public routes match no route in api_routes():\n  " + "\n  ".join(f"{m} {p}" for m, p in gone)


# ───────────────────────── the isolation tests ─────────────────────────

def test_every_private_route_gives_401_without_a_cookie(world):
    anon = proof.new_client(world.app)
    wrong = []
    for case in CASES:
        reply, _ = send(anon, case, world.a, world.b) if not case.upload else (anon.post("/api/imports", json={}), "")
        if reply.status_code != 401:
            wrong.append(f"{case.method} {case.path}: {reply.status_code}")
    assert not wrong, "Routes that answered something other than 401 with no cookie:\n  " + "\n  ".join(wrong)


def test_b_cannot_reach_a_through_any_route(world):
    a, b = world.a, world.b
    tables = proof.tenant_tables()
    start = proof.table_checksums(a.uid, tables)
    problems = []
    ordered = [c for c in CASES if not c.last] + [c for c in CASES if c.last]
    for case in ordered:
        label = f"{case.method} {case.path}" + (f" ({case.note})" if case.note else "")
        if case.fresh:
            b.login()
        before = proof.table_checksums(a.uid, tables)
        try:
            reply, request_text = send(b.client, case, a, b)
        except Exception as exc:                                    # noqa: BLE001
            problems.append(f"{label}: the call raised {type(exc).__name__}")
            continue
        after = proof.table_checksums(a.uid, tables)
        status = reply.status_code
        if status >= 500:
            problems.append(f"{label}: server error {status}")
        if case.kind == "owner" and status != 403:
            problems.append(f"{label}: an owner-only route gave a friend {status}, not 403")
        if case.kind == "by_id" and not (400 <= status < 500 and status not in (401, 429)):
            if not (200 <= status < 300):
                problems.append(f"{label}: expected 403/404-style refusal, got {status}")
        if case.kind == "self" and status == 401 and not case.fresh:
            problems.append(f"{label}: B was signed out (401) in the middle of the run")
        if case.empty and status == 200:
            try:
                if proof.items_of(reply.json()):
                    problems.append(f"{label}: expected an empty list, got rows")
            except ValueError:
                problems.append(f"{label}: expected JSON")
        found = leaks(body_text(reply), a, request_text)
        if found:
            problems.append(f"{label}: the answer holds A's data: {found}")
        changed = [t for t in tables if before[t] != after[t]]
        if changed:
            problems.append(f"{label}: A's tables changed: {changed}")
        if case.fresh and case.path != "/api/account":
            try:
                if case.path == "/api/auth/change-password" and status in (200, 204):
                    b.password = b.password + " again"
                b.login()
            except AssertionError:
                pass
    end = proof.table_checksums(a.uid, tables)
    changed = [t for t in tables if start[t] != end[t]]
    assert not problems, "Isolation failures:\n  " + "\n  ".join(problems)
    assert not changed, f"A's tables are not the same after all of B's calls: {changed}"


def test_a_keeps_a_pending_upload_after_b_tried_its_id(world):
    a, b = world.a, world.b
    b_rows_before = proof.count_rows(b.uid, "transactions")
    for path, body in (("/api/imports/preview", {"upload_id": a.ids["upload"], "mapping": a.ids["upload_mapping"]}),
                       ("/api/imports/commit", {"upload_id": a.ids["upload"], "account_id": b.ids["account2"], "mapping": a.ids["upload_mapping"]})):
        reply = b.post(path, body)
        assert 400 <= reply.status_code < 500 and reply.status_code not in (401, 429), f"B used A's upload id on {path}: {reply.status_code}"
        assert not leaks(body_text(reply), a, a.ids["upload"] + str(b.ids["account2"]))
    assert proof.count_rows(b.uid, "transactions") == b_rows_before, "B's use of A's upload id imported something into B's books"
    listed = proof.items_of(a.get("/api/transactions").json())
    assert any(str(t.get("id")) == str(a.ids["txn"]) for t in listed), "A cannot see A's own payment"
    mine = a.get(f"/api/imports/{a.ids['batch']}")
    assert mine.status_code == 200
    committed = a.post("/api/imports/commit", {"upload_id": a.ids["upload"], "account_id": a.ids["account2"], "mapping": a.ids["upload_mapping"]})
    assert committed.status_code == 200, "A's own pending upload is gone (B's use of the upload id must not consume it)"


def test_same_statement_and_same_sms_give_each_person_their_own_rows(world):
    a, b, gw = world.a, world.b, world.gw
    word = "zqxsharedstmt" + os.urandom(2).hex()
    rows = [("03/10/26", f"UPI-{word.upper()}-{word}@icici-ICIC0000103-429183746577-PAYMENT", "0000429183746577", "17.17")]
    data = proof.statement_csv(rows, "15000.00")
    first = proof.upload_statement(a.client, "same.csv", data)
    second = proof.upload_statement(b.client, "same.csv", data)
    assert first.status_code == 200 and second.status_code == 200, (first.status_code, second.status_code)
    for who, reply in (("A", first), ("B", second)):
        outcome = reply.json()
        if outcome.get("outcome") == "preview":
            outcome = proof.commit_preview(a.client if who == "A" else b.client, outcome, (a if who == "A" else b).ids["account2"]).json()
        assert outcome.get("outcome") == "imported", f"{who}'s upload of an identical file was treated as {outcome.get('outcome')}"
    batch_a, batch_b = proof.first_id(first.json(), "batch"), proof.first_id(second.json(), "batch")
    assert batch_a != batch_b
    assert b.get(f"/api/imports/{batch_a}").status_code in (403, 404, 422), "B can open A's import"
    # the same SMS to both people: one row each, and a retry is a duplicate for that person only
    tokens = {}
    for who in (a, b):
        tokens[who.username] = who.device_token or who.post("/api/phone/devices", {"name": f"{who.username} phone"}).json()["token"]
    before = {who.username: proof.count_rows(who.uid, "transactions") for who in (a, b)}
    sms = proof.sms_debit("23.23", "zqxsamesms" + os.urandom(2).hex(), ref="429183746511")
    for who in (a, b):
        reply = gw.post("/sms", headers={"Authorization": f"Bearer {tokens[who.username]}"}, json={"from": "AX-HDFCBK", "text": sms})
        assert reply.json() == {"status": "added"}, f"{who.username}: {reply.status_code} {reply.text[:80]}"
    again = gw.post("/sms", headers={"Authorization": f"Bearer {tokens[a.username]}"}, json={"from": "AX-HDFCBK", "text": sms})
    assert again.json() == {"status": "duplicate"}
    for who in (a, b):
        assert proof.count_rows(who.uid, "transactions") == before[who.username] + 1, f"{who.username} should have exactly one new row"


def test_a_device_token_only_ever_writes_as_its_own_person(world):
    a, b, gw = world.a, world.b, world.gw
    b_token = b.post("/api/phone/devices", {"name": "Bob's phone"}).json()["token"]
    a_before, b_before = proof.count_rows(a.uid, "transactions"), proof.count_rows(b.uid, "transactions")
    a_checks = proof.table_checksums(a.uid)
    word = "zqxbonlysms" + os.urandom(2).hex()
    # the text names A's account digits (XX1234) and was sent with B's token: it can only land in B's books
    reply = gw.post("/sms", headers={"Authorization": f"Bearer {b_token}"},
                    json={"from": "AX-HDFCBK", "text": proof.sms_debit("31.31", word)})
    assert reply.json() == {"status": "added"}
    assert proof.count_rows(a.uid, "transactions") == a_before, "an SMS sent with B's token reached A's books"
    assert proof.count_rows(b.uid, "transactions") == b_before + 1
    assert proof.table_checksums(a.uid) == a_checks
    # a token that was never issued, and a revoked one, are refused with 401
    assert gw.post("/sms", headers={"Authorization": "Bearer rectifi_not_a_token"}, json={"text": SMS_B}).status_code == 401
    device_id = next(d["id"] for d in proof.items_of(b.get("/api/phone/devices").json()) if d.get("name") == "Bob's phone")
    assert b.post(f"/api/phone/devices/{device_id}/revoke").status_code == 200
    assert gw.post("/sms", headers={"Authorization": f"Bearer {b_token}"}, json={"text": proof.sms_debit("32.32", word + "x")}).status_code == 401


def test_alerts_tick_never_crosses_subscriptions(quiet_world):
    from rectifi import alerts

    world = quiet_world
    a, b, rec = world.a, world.b, world.push
    ends = {}
    for who in (a, b):
        who.post("/api/alerts/rules", {"scope": "total", "target": "all", "period": "week", "cap": "100.00"})
        who.post("/api/push/subscriptions", proof.push_body(f"{who.username}'s phone"))
        with proof.raw_conn(who.uid) as conn:
            ends[who.username] = {r[0] for r in conn.execute("SELECT endpoint FROM push_subscriptions").fetchall()}
    assert ends["alice"].isdisjoint(ends["bob"])
    proof.baseline_alerts(a.uid, b.uid)                     # what is here already is history
    rec.sent.clear()
    big = a.post("/api/transactions", {"amount": "9999.00", "direction": "debit", "date": "2026-10-21", "payee": "Chai stall", "account_id": a.ids["account"]})
    assert big.status_code in (200, 201)
    alerts.tick()
    assert rec.sent, "A passed a cap and nothing was pushed to anybody"
    assert all(endpoint in ends["alice"] for endpoint, _ in rec.sent), "an alert caused by A was pushed to a phone that is not A's"
    with proof.raw_conn(b.uid) as conn:
        assert conn.execute("SELECT count(*) FROM alerts").fetchone()[0] == 0, "A's payment made an alert for B"
    rec.sent.clear()
    assert b.post("/api/transactions", {"amount": "8888.00", "direction": "debit", "date": "2026-10-21", "payee": "Tea stall", "account_id": b.ids["account"]}).status_code in (200, 201)
    alerts.tick()
    assert rec.sent, "B passed a cap and nothing was pushed"
    assert all(endpoint in ends["bob"] for endpoint, _ in rec.sent), "an alert caused by B was pushed to a phone that is not B's"
    for endpoint, payload in rec.sent:
        assert not proof.scan_text(payload, {k: w for k, (w, _) in a.canaries.items()}), "B's push carries something of A's"


def test_a_410_from_the_push_service_deletes_only_that_persons_subscription(quiet_world, monkeypatch):
    """Plan, ALERTS acceptance: a 410 removes the subscription of the person it was sent for, never another person's."""
    from rectifi import alerts, webpush

    a, b = quiet_world.a, quiet_world.b
    b.post("/api/push/subscriptions", proof.push_body("Bob's phone"))
    before_b = proof.count_rows(b.uid, "push_subscriptions")
    assert proof.count_rows(a.uid, "push_subscriptions") == 1
    proof.baseline_alerts(a.uid, b.uid)

    monkeypatch.setattr(webpush, "send", lambda sub, payload, ttl=3600, urgency="normal", client=None: "gone")
    a.post("/api/transactions", {"amount": "9999.00", "direction": "debit", "date": "2026-10-21", "payee": "Chai stall", "account_id": a.ids["account"]})
    alerts.tick()
    assert proof.count_rows(a.uid, "push_subscriptions") == 0, "a 410 did not remove the phone it was sent to"
    assert proof.count_rows(b.uid, "push_subscriptions") == before_b, "A's 410 deleted B's phone"


# ───────────────────────── the honesty test ─────────────────────────

def privacy_facts(app, monkeypatch, provider: str | None = None) -> dict:
    if provider:
        monkeypatch.setenv("LLM_PROVIDER", provider)
    reply = proof.new_client(app).get("/api/privacy")
    assert reply.status_code == 200, f"/api/privacy answered {reply.status_code} without a cookie (it is public)"
    return reply.json()


def test_privacy_facts_list_every_sealed_field_every_brand_and_every_plain_item(monkeypatch):
    from rectifi import merchant_allowlist, sealed_fields

    proof.multiuser_env(monkeypatch)
    facts = privacy_facts(proof.make_app(), monkeypatch)
    hay = proof.haystack(facts)
    missing = [f"{t}.{c}" for t, c in sealed_fields.all_sealed() if proof.norm(c) not in hay]
    missing += [f"blind {t}.{c}" for t, c in sealed_fields.all_blind() if proof.norm(c) not in hay]
    missing += [f"brand {brand.name}" for brand in merchant_allowlist.ALLOWED if proof.norm(brand.name) not in hay]
    plain = {  # every plain item in plan section 1: the words a reader would look for
        "amounts": ["amount"], "dates": ["date"], "categories": ["categor"], "account names": ["accountname"],
        "last 4 digits": ["last4", "lastfour"], "caps": ["cap"], "times": ["time"], "well-known merchant names": ["merchant"],
    }
    missing += [f"plain item '{item}'" for item, stems in plain.items() if not any(s in hay for s in stems)]
    assert not missing, "/api/privacy does not mention: " + ", ".join(missing)
    assert "owner" in hay, "the Privacy page must say what the owner (the person running the server) can see"


@pytest.mark.parametrize("provider,expected", [("ollama", "local"), ("openrouter", "cloud"), ("deepseek", "cloud")])
def test_privacy_facts_agree_with_the_configuration(monkeypatch, provider, expected):
    from rectifi import config

    proof.multiuser_env(monkeypatch)
    facts = privacy_facts(proof.make_app(), monkeypatch, provider)
    assert config.llm_provider() == provider
    values = [v.lower() for k, v in proof.flatten(facts) if "provider" in k.lower() or "model" in k.lower()]
    hay = " ".join(values) or proof.haystack(facts)
    assert expected in hay, f"with LLM_PROVIDER={provider} the privacy facts should say the AI is {expected}: {values or 'no provider key found'}"
    other = "cloud" if expected == "local" else "local"
    assert other not in " ".join(values), f"the privacy facts call a {provider} model {other}"
    modes = [v.lower() for k, v in proof.flatten(facts) if "mode" in k.lower() or "multi" in k.lower()]
    assert any("multi" in m or m == "true" for m in modes) or "multiuser" in proof.haystack(facts), "the facts do not say the server is in multi-user mode"


def test_privacy_facts_say_laptop_mode_when_it_is_laptop_mode(monkeypatch):
    monkeypatch.delenv("RECTIFI_MULTIUSER", raising=False)
    reply = proof.new_client(proof.make_app()).get("/api/privacy")
    assert reply.status_code == 200
    modes = [v.lower() for k, v in proof.flatten(reply.json()) if "mode" in k.lower() or "multi" in k.lower()]
    assert not any(m == "true" for m in modes), "laptop mode reported as multi-user"
