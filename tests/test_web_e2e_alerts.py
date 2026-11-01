"""The alerts pages in a real browser: Alerts (Inbox, Caps, Delivery), Leaks, and the Today strip on Overview.

Skipped when Playwright, Chromium or web/dist is missing. With RECTIFI_SCREENSHOTS=1 (and RECTIFI_SHOTS_DIR) it saves screenshots.
Every figure the browser shows is compared with the one the server works out, read straight from rectifi/alerts.py.
"""

import re
from datetime import date

import pytest
from alerts_support import TODAY, add, add_phone
from web_support import PINNED_TODAY, browser, live_server, new_page, pinned_today, shot  # noqa: F401

from rectifi import alerts, tools, webpush
from rectifi.db import connect
from rectifi.money import fmt_inr

WIDTHS = pytest.mark.parametrize("kind", ["phone", "desktop"])


def tag(kind: str) -> str:
    return "390" if kind == "phone" else "1280"


def no_sideways(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"


@pytest.fixture(autouse=True)
def world(monkeypatch, tmp_path):
    """Nothing from this laptop leaks in: no real push key, no real model, and the push key goes to a temp folder."""
    assert PINNED_TODAY == TODAY
    monkeypatch.delenv("RECTIFI_VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("RECTIFI_VAPID_SUB", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")             # a provider with no key: the chat model is "off"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(webpush, "KEY_DIR", tmp_path / ".rectifi")
    webpush._reset_for_tests()
    yield
    webpush._reset_for_tests()


def seed() -> None:
    """Salary in, three months of Swiggy before, a few small payments this month; then 'now' becomes the starting line for alerts."""
    tools.add_account("Main", "1234")
    add(100000, date(2026, 10, 1), "IITM stipend", "Salary & Allowance", "credit")
    for month in (7, 8, 9):
        add(3000, date(2026, month, 10), "Swiggy", "Food & Dining")
    add(640, date(2026, 10, 20), "Blinkit", "Groceries")
    add(349, TODAY, "Swiggy", "Food & Dining")
    alerts.save_prefs({"monthly_income": "100000"})
    alerts.ensure_watermark()                                  # all of the above is history, not news


def first_alert() -> dict:
    with connect() as conn:
        return conn.execute("SELECT * FROM alerts ORDER BY id").fetchone()


def make_swiggy_alert() -> None:
    """A Swiggy payment today that goes past a ₹700 weekly cap: one 'act' alert in the inbox."""
    alerts.create_rule("merchant", "Swiggy", "week", "700")
    add(850, TODAY, "Swiggy", "Food & Dining")
    alerts.sweep("test")
    assert first_alert()["severity"] == "act"


# ───────────────────────── Caps ─────────────────────────

def test_a_weekly_swiggy_cap_from_a_preset_is_saved(browser, live_server):
    seed()
    expected = next(p for p in alerts.presets(alerts.build_context()) if (p["scope"], p["target"]) == ("merchant", "Swiggy"))
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/alerts?tab=caps")
    row = page.locator("[data-testid=preset-row][data-scope=merchant][data-target=Swiggy]")
    row.wait_for()
    keep = row.get_by_role("button", name=re.compile("^Keep"))
    assert fmt_inr(expected["keep"]["week"], paise=False) in keep.inner_text()      # the figure on the button is the server's
    keep.click()
    page.wait_for_selector("[data-testid=rule-row][data-target=Swiggy][data-period=week]")
    with connect() as conn:
        rules = conn.execute("SELECT scope, target, period, cap FROM alert_rules").fetchall()
    assert [(r["scope"], r["target"], r["period"], r["cap"]) for r in rules] == [("merchant", "Swiggy", "week", expected["keep"]["week"])]
    assert "A cap exists" in row.inner_text()                  # the page now says pressing again replaces it
    assert not problems, problems


def test_a_custom_cap_that_names_nothing_is_refused_with_words(browser, live_server):
    seed()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/alerts?tab=caps")
    form = page.get_by_role("form", name="Set your own cap")
    form.get_by_label("Merchant name").fill("Nobody Ltd")
    form.get_by_label("At most, in rupees").fill("500")
    form.get_by_role("button", name="Set cap").click()
    form.get_by_role("alert").wait_for()
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM alert_rules").fetchone()["n"] == 0
    assert not [p for p in problems if "pageerror" in p], problems


# ───────────────────────── Inbox ─────────────────────────

def test_inbox_read_sets_read_at_and_the_menu_chip_uses_a_word(browser, live_server):
    seed()
    make_swiggy_alert()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/alerts")
    row = page.locator("[data-testid=alert-row]").first
    row.wait_for()
    stored = first_alert()
    assert stored["read_at"] is None
    assert row.get_attribute("data-read") == "false"
    assert stored["title"] in row.inner_text()                 # the words are the server's
    chip = page.locator("nav[aria-label=Main] [data-testid=unread-chip]").first
    chip.wait_for()
    assert chip.inner_text().strip().startswith("1 new")
    assert "Explain this" not in page.content()                # no model, so no Explain button
    row.get_by_role("button", name="Read", exact=True).click()
    page.wait_for_selector("[data-testid=alert-row][data-read=true]")
    assert first_alert()["read_at"] is not None
    page.wait_for_selector("[data-testid=unread-chip]", state="detached")
    assert not problems, problems


def test_inbox_mark_all_read_and_snooze(browser, live_server):
    seed()
    make_swiggy_alert()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/alerts")
    row = page.locator("[data-testid=alert-row]").first
    row.get_by_role("combobox").select_option("24")
    page.wait_for_selector("text=Snoozed.")
    with connect() as conn:
        assert conn.execute("SELECT snoozed_until FROM alert_rules").fetchone()["snoozed_until"] is not None
    page.get_by_role("button", name="Mark all read").click()
    page.wait_for_selector("text=Everything is read.")
    assert first_alert()["read_at"] is not None
    assert not problems, problems


def test_the_empty_inbox_points_to_caps(browser, live_server):
    seed()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/alerts")
    page.wait_for_selector("text=No alerts yet.")
    page.get_by_role("link", name="Set a cap").click()
    page.wait_for_selector("text=Set a cap from your average")
    assert not problems, problems


# ───────────────────────── Delivery ─────────────────────────

def test_delivery_never_shows_a_phone_address_and_asks_nothing_until_you_tap(browser, live_server):
    seed()
    add_phone("Pixel 8")
    with connect() as conn:
        endpoint = conn.execute("SELECT endpoint FROM push_subscriptions").fetchone()["endpoint"]
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/alerts?tab=delivery")
    page.wait_for_selector("[data-testid=device-row]")
    page.wait_for_selector("text=Notifications are blocked for Rectifi.")          # headless Chromium starts with notifications blocked: said in words
    assert page.get_by_role("button", name="Turn on phone alerts").count() == 0
    # headless Chromium cannot be granted notifications, so show the page a phone where the question has not been asked yet
    page.add_init_script("Object.defineProperty(Notification, 'permission', { get: () => 'default' })")
    page.reload()
    page.wait_for_selector("[data-testid=device-row]")
    text = page.inner_text("main")
    assert "Pixel 8" in text and "fcm.googleapis.com" in text
    assert endpoint not in page.content() and endpoint.rsplit("/", 1)[-1] not in page.content()
    page.get_by_role("button", name="Turn on phone alerts").wait_for()
    assert page.evaluate("navigator.serviceWorker.getRegistrations().then(r => r.length)") == 0     # no worker before the tap
    assert page.evaluate("Notification.permission") == "default"                   # still unasked: only a tap on the button asks
    assert "Push key fingerprint" in text
    assert not problems, problems


def test_delivery_settings_save_and_show_the_detected_income(browser, live_server):
    seed()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/alerts?tab=delivery")
    form = page.get_by_role("form", name="Alert settings")
    form.get_by_label("Send alerts to my phone").check()
    form.get_by_label("Private notifications", exact=False).check()
    form.get_by_role("button", name="Save settings").click()
    page.wait_for_selector("text=Saved.")
    prefs = alerts.get_prefs()
    assert prefs["push_on"] is True and prefs["private_notifications"] is True
    assert "Detected from your salary" in form.inner_text()
    assert not problems, problems


# ───────────────────────── Today strip ─────────────────────────

def test_today_strip_text_equals_the_servers_figures(browser, live_server):
    seed()
    make_swiggy_alert()
    view = alerts.today_view(alerts.build_context())
    safe = view["safe"]
    assert safe is not None and not safe["over_today"]
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/")
    page.wait_for_selector("[data-testid=today-strip]")
    assert page.inner_text("[data-testid=today-safe]") == fmt_inr(safe["daily"], paise=False)
    assert page.inner_text("[data-testid=today-left]") == fmt_inr(safe["left_today"], paise=False)
    strip = page.inner_text("[data-testid=today-strip]")
    assert "1 new alert" in strip
    assert view["small_spends"], "the seed has one small payment today"
    for spend in view["small_spends"]:                                           # today's small payments, with the server's labels
        assert f"{spend['label']} {fmt_inr(spend['amount'], paise=False)}" in strip
    page.get_by_text("How this is worked out").click()
    formula = page.inner_text("[data-testid=today-formula]")
    assert fmt_inr(safe["spendable"], paise=False) in formula and f"over {safe['days_left']} days" in formula
    page.locator("[data-testid=today-strip]").get_by_role("link", name="1 new alert").click()
    page.wait_for_selector("[data-testid=alert-row]")
    assert not problems, problems


def test_today_strip_says_what_to_set_when_nothing_is_known(browser, live_server):
    tools.add_account("Main", "1234")
    add(100, TODAY, "Chai", "Food & Dining")
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/")
    page.wait_for_selector("text=Set a budget or your monthly income to see this.")
    assert not problems, problems


# ───────────────────────── Leaks ─────────────────────────

def test_leaks_page_shows_the_servers_labels_and_figures(browser, live_server):
    seed()
    make_swiggy_alert()
    report = alerts.leak_report(alerts.build_context(since=date(2026, 10, 1)), date(2026, 10, 1))
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/leaks")
    page.wait_for_selector("[data-testid=waterfall]")
    for segment in report["segments"]:
        row = page.locator(f"[data-testid=wf-{segment['key']}]")
        assert segment["label"] in row.inner_text()
        assert fmt_inr(segment["amount"], paise=False) in row.inner_text()
    merchants = page.inner_text("[data-testid=leaks-merchants]")
    for entry in report["merchants"]:
        assert entry["merchant"] in merchants and fmt_inr(entry["total"], paise=False) in merchants
    projection = report["projection"]
    assert projection is not None
    assert fmt_inr(projection["projected"], paise=False) in page.inner_text("[data-testid=leaks-projection]")
    assert not problems, problems


def test_leaks_with_no_spending_says_so(browser, live_server):
    tools.add_account("Main", "1234")
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/leaks")
    page.wait_for_selector("text=No spending recorded for this month.")
    assert not problems, problems


# ───────────────────────── layouts, themes, keyboard ─────────────────────────

@WIDTHS
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_alert_page_fits_and_has_no_console_errors(browser, live_server, kind, scheme):         # noqa: C901
    seed()
    add_phone("Pixel 8")
    make_swiggy_alert()
    page, problems = new_page(browser, live_server, kind, scheme)
    for name, path, wait, scope in (                                            # scope: what this test judges (Overview has older parts of its own)
        ("overview", "/", "[data-testid=today-strip]", "[data-testid=today-strip]"),
        ("alerts-inbox", "/alerts", "[data-testid=alert-row]", "main"),
        ("alerts-caps", "/alerts?tab=caps", "[data-testid=preset-row]", "main"),
        ("alerts-delivery", "/alerts?tab=delivery", "[data-testid=device-row]", "main"),
        ("leaks", "/leaks", "[data-testid=waterfall]", "main"),
    ):
        page.goto(path)
        page.wait_for_selector(wait)
        page.wait_for_timeout(300)
        shot(page, f"{name}-{tag(kind)}-{scheme}")
        no_sideways(page)
        small = page.evaluate(
            """(scope) => [...document.querySelectorAll(`${scope} button, ${scope} a, ${scope} input:not([type=checkbox]), ${scope} select`)]
                 .filter(e => e.offsetParent !== null && !e.closest('[aria-hidden=true]'))
                 .map(e => [e.getBoundingClientRect(), e.textContent.trim() || e.getAttribute('aria-label') || e.name || e.tagName])
                 .filter(([r]) => r.height < 43.5).map(([r, n]) => n + ' ' + Math.round(r.height))""",
            scope,
        )
        assert not small, f"{path}: targets under 44 px: {small}"
    assert not problems, problems


def test_the_alerts_tabs_work_from_the_keyboard(browser, live_server):
    seed()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/alerts")
    page.get_by_role("tab", name="Inbox").focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_selector("text=Set a cap from your average")
    assert page.url.endswith("tab=caps")
    page.keyboard.press("ArrowRight")
    page.wait_for_selector("text=Alerts on your phone")
    assert page.url.endswith("tab=delivery")
    assert not problems, problems
