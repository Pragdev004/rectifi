"""WS-PROOF: the multi-user app in real browsers (docs/MULTIUSER_PLAN.md section 8).

Two browser contexts, A and B, side by side against one live server in multi-user mode; every claim about what the browser did is
proved by reading the database. And the owner claim with data that already exists: user 1's totals and balance check are the same
before, during and after the sweep.

Skipped (not failed) when Playwright, Chromium or web/dist is missing, like the other browser tests. Sign-up and the writes go through
the browser context's own request channel (so the cookie lives in the browser); what each person SEES is read from the page.
Assumptions about page addresses and test ids are listed in docs/handoffs/ws-proof.md.
"""

from __future__ import annotations

import re

import pytest
from support import proof
from web_support import LiveServer, browser, new_page, pinned_today, skip_reason  # noqa: F401

OWNER_ONLY_PAGES = ("/ask", "/charts", "/library", "/memory", "/traces")
OWNER_ONLY_API = {"/ask": "/api/ask/status", "/charts": "/api/charts/status", "/library": "/api/knowledge/status",
                  "/memory": "/api/memory", "/traces": "/api/traces"}


@pytest.fixture
def mu_server(monkeypatch):
    reason = skip_reason()
    if reason:
        pytest.skip(reason)
    proof.multiuser_env(monkeypatch)             # before the server is built: it reads the mode when it starts
    server = LiveServer()
    server.start()
    yield server
    server.stop()


class Browserist:
    """One person in one browser context."""

    def __init__(self, browser, server, username: str, kind: str = "desktop") -> None:
        self.server, self.username, self.password = server, username, proof.PASSWORD
        self.page, self.problems = new_page(browser, server, kind)
        self.ctx = self.page.context
        self.uid = 0
        self.canaries = proof.Canaries(username[:2])

    def call(self, method: str, path: str, body=None):
        send = getattr(self.ctx.request, method.lower())
        kwargs = {"headers": proof.HEADERS}
        if body is not None or method.upper() in ("POST", "PUT", "PATCH"):
            kwargs["data"] = body if body is not None else {}
        return send(self.server.url + path, **kwargs)

    def sign_up(self, invite: str) -> None:
        made = self.call("POST", "/api/auth/signup", {"invite": invite, "username": self.username, "password": self.password})
        assert made.status == 201, f"sign-up refused: {made.status}"
        token = made.json()["signup_token"]
        done = self.call("POST", "/api/auth/signup/confirm", {"signup_token": token})
        assert done.status in (200, 201), f"confirm refused: {done.status}"
        self.uid = proof.uid_of(self.username)

    def write(self, amount: str, when: str = "2026-10-05") -> int:
        acct = self.call("POST", "/api/accounts", {"name": self.canaries.new("acct"), "last4": "1234"})
        assert acct.status == 200, acct.status
        made = self.call("POST", "/api/transactions", {"amount": amount, "direction": "debit", "date": when, "payee": self.canaries.new("payee"),
                                                      "note": self.canaries.new("note"), "account_id": acct.json()["account"]["id"]})
        assert made.status == 200, f"add payment: {made.status} {made.text()[:100]}"
        return int(made.json()["transaction"]["id"])

    def read_ledger(self) -> str:
        self.page.goto("/ledger")
        self.page.wait_for_selector("main")
        word = self.canaries.items["payee"][0]
        self.page.wait_for_selector(f"text={word}")
        return self.page.inner_text("body")


def test_two_people_side_by_side_see_only_their_own(browser, mu_server):
    a, b = Browserist(browser, mu_server, "alice"), Browserist(browser, mu_server, "bob")
    a.sign_up(proof.make_invite())
    b.sign_up(proof.make_invite())
    a_txn, b_txn = a.write("4412.25"), b.write("5523.35")
    text_a, text_b = a.read_ledger(), b.read_ledger()
    wa, wb = a.canaries.items["payee"][0], b.canaries.items["payee"][0]
    assert wa in text_a.lower() and wb not in text_a.lower(), "A's ledger page shows B's payment (or not A's own)"
    assert wb in text_b.lower() and wa not in text_b.lower(), "B's ledger page shows A's payment (or not B's own)"
    # the database agrees: each person has one payment, and its text is sealed
    for person in (a, b):
        assert proof.count_rows(person.uid, "transactions") == 1
        with proof.raw_conn(person.uid) as conn:
            payee, note = conn.execute("SELECT payee, note FROM transactions").fetchone()
        assert payee.startswith(("rx1.", "rs1.")) and note.startswith(("rx1.", "rs1.")), f"{person.username}'s text is not sealed in the table"
    # B reaches for A's payment by id: refused, and A's page still shows it after a reload
    before = proof.table_checksums(a.uid)
    gone = b.call("DELETE", f"/api/transactions/{a_txn}")      # delete is idempotent on purpose: an id B cannot see answers 200 {"deleted": false}
    assert gone.status in (403, 404) or (gone.status == 200 and gone.json() == {"deleted": False}), (gone.status, gone.text()[:80])
    assert b.call("PATCH", f"/api/transactions/{a_txn}", {"note": "hijacked"}).status in (403, 404)
    assert proof.table_checksums(a.uid) == before
    assert wa in a.read_ledger().lower()
    # a friend is not offered the owner-only pages
    for person in (a, b):
        person.page.goto("/ledger")
        person.page.wait_for_selector("main")
        shown = [href for href in OWNER_ONLY_PAGES if person.page.locator(f'a[href="{href}"]').count()]
        assert not shown, f"{person.username} is offered owner-only pages: {shown}"
        for href in OWNER_ONLY_PAGES:
            assert person.call("GET", OWNER_ONLY_API[href]).status == 403, f"{OWNER_ONLY_API[href]} is open to a friend"
    for person in (a, b):
        assert not person.problems, person.problems


def test_signing_out_and_in_again_gives_the_same_person_the_same_data(browser, mu_server):
    a = Browserist(browser, mu_server, "carol")
    a.sign_up(proof.make_invite())
    a.write("1201.20")
    assert a.call("POST", "/api/auth/logout").status in (200, 204)
    assert a.call("GET", "/api/transactions").status == 401, "still signed in after signing out"
    a.page.goto("/ledger")
    try:
        a.page.wait_for_url(re.compile(r".*/login"), timeout=5000)
    except Exception:                                          # noqa: BLE001
        assert a.page.locator("input[type=password]").count(), "a signed-out visitor should meet the login page"
    login = a.call("POST", "/api/auth/login", {"username": a.username, "password": a.password})
    assert login.status == 200
    assert a.canaries.items["payee"][0] in a.read_ledger().lower()


def test_owner_claim_keeps_totals_and_the_balance_check_before_during_and_after(browser, mu_server, monkeypatch):
    from decimal import Decimal

    from web_support import seed_month

    from rectifi import tools, userctx
    from rectifi.money import fmt_inr

    owner_ctx = userctx.UserContext(uid=userctx.OWNER_UID, strict=False)

    def figures() -> dict:
        """Everything about user 1's money that must not move: counts, sums, the month, and the balance check."""
        start, end = tools.month_bounds("2026-10")
        with userctx.bound(owner_ctx):
            month = tools.spending_summary(start, end)
            recon = tools.reconcile("SBI Savings")
        with proof.raw_conn(1) as conn:
            sums = conn.execute("SELECT direction, count(*), sum(amount) FROM transactions GROUP BY direction ORDER BY direction").fetchall()
        return {"sums": [(d, n, Decimal(s)) for d, n, s in sums], "income": month["income"], "spending": month["spending"],
                "saved": month["saved"], "recon": recon}

    monkeypatch.delenv("RECTIFI_MULTIUSER")                    # the old data was written on the laptop: plain, as it has always been
    with userctx.bound(owner_ctx):
        seed_month()                                           # old data: plain, user 1, no password yet
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")               # now the same database is served in multi-user mode
    baseline = figures()
    assert baseline["sums"], "the owner should have data before the claim"
    assert proof.plain_sealed_cells(1) > 0

    me = Browserist(browser, mu_server, "prag")
    invite = proof.make_invite(owner=True)
    made = me.call("POST", "/api/auth/signup", {"invite": invite, "username": me.username, "password": me.password})
    assert made.status == 201, f"the owner claim was refused: {made.status}"
    sampler = proof.Sampler(figures)
    sampler.start()
    try:
        done = me.call("POST", "/api/auth/signup/confirm", {"signup_token": made.json()["signup_token"]})
        assert done.status in (200, 201)
        me.uid = proof.uid_of(me.username)
        assert me.uid == 1, "the owner invite must claim row 1"
        proof.wait_for_sweep(1)
    finally:
        samples = sampler.finish()
    assert not sampler.errors, f"reading the totals failed during the sweep: {sampler.errors[:2]}"
    assert samples, "the totals were never read during the claim"
    moved = [s for s in samples if s != baseline]
    assert not moved, f"user 1's totals or balance check changed while the sweep ran: {moved[0]} (baseline {baseline})"
    assert figures() == baseline, "user 1's totals or balance check changed after the sweep"
    assert proof.plain_sealed_cells(1) == 0, "the sweep left plain text behind"

    # and the browser tells the same story as the database
    me.page.goto("/")
    me.page.wait_for_selector("[data-testid=hero-income]")
    assert me.page.inner_text("[data-testid=hero-income]") == fmt_inr(baseline["income"], paise=False)
    assert me.page.inner_text("[data-testid=hero-spending]") == fmt_inr(baseline["spending"], paise=False)
    assert me.page.inner_text("[data-testid=hero-saved]") == fmt_inr(baseline["saved"], paise=False)
    assert me.page.query_selector("[data-testid=recon-slip][data-balanced=true]"), "the balance check no longer balances after the claim"
    assert not me.problems, me.problems


def test_the_privacy_page_is_public_and_tells_the_same_story_as_the_registry(browser, mu_server):
    from rectifi import merchant_allowlist, sealed_fields

    visitor = Browserist(browser, mu_server, "visitor")        # never signs in
    visitor.page.goto("/privacy")
    visitor.page.wait_for_selector("main, h1")
    assert not visitor.page.url.rstrip("/").endswith("/login"), "the Privacy page asked for a login (it must be public)"
    text = proof.norm(visitor.page.inner_text("body"))
    missing = [f"{t}.{c}" for t, c in sealed_fields.all_sealed() if proof.norm(c) not in text]
    missing += [f"brand {b.name}" for b in merchant_allowlist.ALLOWED if proof.norm(b.name) not in text]
    assert not missing, f"the Privacy page does not mention: {missing}"
    assert "owner" in text, "the Privacy page must say what the owner can see"
    assert not visitor.problems, visitor.problems
