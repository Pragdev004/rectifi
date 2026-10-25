"""The web app in a real browser (Chromium), against a live server and the test database: the shell and the Overview page.

Skipped when Playwright, Chromium or web/dist is not available. With RECTIFI_SCREENSHOTS=1 it also saves screenshots to
/tmp/rectifi-web-shots/ for people to look at.
"""

import pytest
from web_support import browser, live_server, new_page, pinned_today, seed_month, shot  # noqa: F401

from rectifi import tools
from rectifi.money import fmt_inr

TAPPABLE = "button, a[href], input, select, textarea, [role=tab], summary"

CHECK_TARGETS = """
() => {
  const bad = [];
  for (const el of document.querySelectorAll(%r)) {
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    let box = el.getBoundingClientRect();
    if ((el.type === 'checkbox' || el.type === 'radio') && el.closest('label')) box = el.closest('label').getBoundingClientRect();
    if (box.width <= 1 || box.height <= 1) continue;                    // visually hidden helpers
    if (box.bottom <= 0 || box.top >= innerHeight + 4000) continue;     // off-screen (the skip link waits above the page)
    if (box.width < 43.5 || box.height < 43.5) bad.push((el.getAttribute('aria-label') || el.textContent || el.tagName).trim().slice(0, 40) + ' ' + Math.round(box.width) + 'x' + Math.round(box.height));
  }
  return bad;
}
""" % TAPPABLE


def month_figures():
    start, end = tools.month_bounds("2026-10")
    return tools.spending_summary(start, end)


def test_overview_hero_matches_the_ledger_to_the_rupee(browser, live_server):
    seed_month()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/")
    page.wait_for_selector("[data-testid=hero-income]")
    summary = month_figures()
    assert page.inner_text("[data-testid=hero-income]") == fmt_inr(summary["income"], paise=False)
    assert page.inner_text("[data-testid=hero-spending]") == fmt_inr(summary["spending"], paise=False)
    assert page.inner_text("[data-testid=hero-saved]") == fmt_inr(summary["saved"], paise=False)
    assert page.inner_text("[data-testid=hero-rate]").replace("\n", "") == f"{summary['savings_rate']}%"
    assert page.inner_text("h1") == "October 2026"
    assert "SBI Savings" in page.inner_text("main")
    assert page.query_selector("[data-testid=recon-slip][data-balanced=true]")
    assert not problems, problems
    shot(page, "overview-1280-light")


def test_overview_has_budgets_pace_chart_and_a_table_alternative(browser, live_server):
    seed_month()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/")
    page.wait_for_selector("[data-testid=hero-income]")
    assert page.query_selector("[data-testid='budget-Food & Dining']")
    food = page.get_attribute("[data-testid='budget-Food & Dining'] [role=img]", "aria-label")
    assert food.startswith("Food & Dining: ") and "of budget used" in food and "of October gone" in food
    assert page.query_selector("figure svg[role=img]")
    assert "Spending so far is" in page.inner_text("figcaption")
    page.click("text=Show as table")
    assert page.query_selector("figure table tbody tr")
    assert not problems, problems


@pytest.mark.parametrize("kind", ["phone", "desktop"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_overview_in_both_widths_and_both_themes(browser, live_server, kind, scheme):
    seed_month()
    page, problems = new_page(browser, live_server, kind, scheme)
    page.goto("/")
    page.wait_for_selector("[data-testid=hero-income]")
    page.evaluate("document.fonts.ready")
    expected = "rgb(23, 21, 15)" if scheme == "dark" else "rgb(246, 242, 233)"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == expected
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"
    assert not problems, problems
    shot(page, f"overview-{390 if kind == 'phone' else 1280}-{scheme}")


def test_phone_overview_has_no_sideways_scroll_and_big_enough_targets(browser, live_server):
    seed_month()
    page, _ = new_page(browser, live_server, "phone")
    page.goto("/")
    page.wait_for_selector("[data-testid=hero-income]")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    assert page.evaluate(CHECK_TARGETS) == []
    page.goto("/")                                     # the same at the top and after scrolling down
    page.mouse.wheel(0, 3000)
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_desktop_overview_targets_are_big_enough(browser, live_server):
    seed_month()
    page, _ = new_page(browser, live_server, "desktop")
    page.goto("/")
    page.wait_for_selector("[data-testid=hero-income]")
    assert page.evaluate(CHECK_TARGETS) == []


def test_keyboard_reaches_the_skip_link_first_then_the_navigation_with_a_visible_focus_ring(browser, live_server):
    page, _ = new_page(browser, live_server, "desktop")
    page.goto("/")
    page.wait_for_selector("h1")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.textContent") == "Skip to content"
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") != "none"
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.closest('nav')?.getAttribute('aria-label')") == "Main" or \
        page.evaluate("document.activeElement.textContent").strip() == "Rectifi"
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Enter")                       # the skip link moves focus into the page
    assert page.evaluate("document.activeElement.id") == "main"


def test_the_rupee_sign_and_the_optical_size_font_load(browser, live_server):
    seed_month()
    page, _ = new_page(browser, live_server, "desktop")
    page.goto("/")
    page.wait_for_selector("[data-testid=hero-income]")
    result = page.evaluate("""async () => {
        await document.fonts.ready;
        const mono = await document.fonts.load('500 16px "IBM Plex Mono"', '\\u20b9');
        const sans = await document.fonts.load('400 16px "IBM Plex Sans"', '\\u20b9');
        const fr = await document.fonts.load('600 40px "Fraunces Variable"', '27.5');
        return [mono.length, sans.length, fr.length, document.fonts.check('500 16px "IBM Plex Mono"', '\\u20b9')];
    }""")
    assert result[0] > 0 and result[1] > 0 and result[2] > 0 and result[3] is True, result


def test_dark_mode_follows_the_system_and_the_switch_overrides_it(browser, live_server):
    page, _ = new_page(browser, live_server, "desktop", "dark")
    page.goto("/")
    page.wait_for_selector("h1")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(23, 21, 15)"
    page.get_by_label("Light").check(force=True)
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(246, 242, 233)"
    page.reload()                                       # theme-init.js applies it before the first paint
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    page.get_by_label("System").check(force=True)
    assert page.evaluate("localStorage.getItem('rectifi.theme')") is None


def test_empty_month_has_a_designed_empty_state(browser, live_server):
    page, problems = new_page(browser, live_server, "phone")
    page.goto("/")
    page.wait_for_selector("text=Nothing recorded for this month yet.")
    assert page.get_by_role("link", name="Add an entry").is_visible()
    assert page.get_by_role("link", name="Import a statement").is_visible()
    assert not problems, problems
    shot(page, "overview-empty-390-light")


def test_phone_navigation_and_the_more_sheet(browser, live_server):
    seed_month()
    page, problems = new_page(browser, live_server, "phone")
    page.goto("/")
    page.wait_for_selector("[data-testid=hero-income]")
    nav = page.get_by_role("navigation", name="Main")
    assert [a.inner_text().strip() for a in nav.get_by_role("link").all()] == ["Home", "Ledger", "Add", "Ask"]
    page.get_by_role("button", name="More").click()
    dialog = page.get_by_role("dialog", name="More")
    dialog.wait_for()
    assert [a.inner_text().strip() for a in dialog.get_by_role("link").all()] == [
        "Review", "Insights", "Charts", "Budgets", "Accounts", "Phone", "Library", "Memory", "Traces"]
    assert dialog.get_by_label("Dark").count() == 1
    shot(page, "more-sheet-390")
    page.keyboard.press("Escape")
    page.wait_for_selector("dialog:not([open])", state="attached")
    assert page.get_by_role("dialog").count() == 0
    page.get_by_role("button", name="More").click()
    page.get_by_role("dialog", name="More").get_by_role("link", name="Budgets").click()
    page.wait_for_url("**/budgets")
    assert not problems, problems


def test_month_stepper_changes_the_month_in_the_address(browser, live_server):
    seed_month()
    page, _ = new_page(browser, live_server, "desktop")
    page.goto("/")
    page.wait_for_selector("[data-testid=hero-income]")
    assert page.get_by_role("button", name="Next month").is_disabled()
    page.get_by_role("button", name="Previous month").click()
    page.wait_for_url("**/?month=2026-09")
    page.wait_for_selector("text=Nothing recorded for this month yet.")
    assert page.inner_text("h1") == "September 2026"
    page.go_back()
    page.wait_for_selector("[data-testid=hero-income]")


def test_database_down_shows_the_designed_page_and_recovers(browser, live_server, monkeypatch):
    from rectifi.api.routes import overview
    from rectifi.db import DatabaseUnavailable

    page, _ = new_page(browser, live_server, "desktop")

    def down(*a, **k):
        raise DatabaseUnavailable("Can't reach Postgres at localhost:5432.")

    monkeypatch.setattr(overview.tools, "parse_month", down)
    page.goto("/")
    page.wait_for_selector("[data-testid=db-down]")
    assert "Your data is safe" in page.inner_text("main")
    assert page.get_by_role("button", name="Try again").is_visible()
    shot(page, "db-down-1280")
    monkeypatch.undo()
    page.get_by_role("button", name="Try again").click()
    page.wait_for_selector("text=Nothing recorded for this month yet.")


def test_offline_banner_and_retry(browser, live_server):
    seed_month()
    page, _ = new_page(browser, live_server, "phone")
    page.goto("/")
    page.wait_for_selector("[data-testid=hero-income]")
    page.route("**/api/**", lambda route: route.abort())
    page.get_by_role("button", name="Previous month").click()
    page.wait_for_selector("text=isn't answering")
    assert "dev.py up" in page.inner_text("[role=alert]")
    shot(page, "offline-390")
    page.unroute("**/api/**")
    page.get_by_role("button", name="Retry").first.click()
    page.wait_for_selector("text=isn't answering", state="detached")


def test_unknown_address_is_a_designed_page_not_a_blank(browser, live_server):
    page, _ = new_page(browser, live_server, "desktop")
    page.goto("/nowhere")
    page.wait_for_selector("text=There is nothing at this address.")


def test_reduced_motion_removes_the_motion(browser, live_server):
    seed_month()
    page, _ = new_page(browser, live_server, "phone", reduced_motion="reduce")
    page.goto("/")
    page.wait_for_selector("[data-testid=hero-income]")
    assert float(page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--d-base').trim().replace('ms', '')")) == 0.01
    page.get_by_role("button", name="More").click()
    page.get_by_role("dialog", name="More").wait_for()
    assert page.evaluate("getComputedStyle(document.querySelector('dialog')).animationName") in ("none", "")
