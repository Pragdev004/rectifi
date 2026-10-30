"""The web app in a real browser, part 3: Review, Insights, Phone and Ask.

Skipped when Playwright, Chromium or web/dist is missing. With RECTIFI_SCREENSHOTS=1 it saves screenshots.
"""

from datetime import date

import pytest
from langchain_core.messages import AIMessage
from support.fakes import ScriptedStructured
from test_agent import ScriptedModel, call
from test_web_e2e import CHECK_TARGETS
from web_support import PINNED_TODAY, browser, live_server, new_page, pinned_today, seed_month, shot  # noqa: F401

from rectifi import insights as ins
from rectifi import tools
from rectifi.api import ask

WIDTHS = pytest.mark.parametrize("kind", ["phone", "desktop"])


def tag(kind: str) -> str:
    return "390" if kind == "phone" else "1280"


def no_sideways(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"


def history_for_insights():
    """Three earlier months of food spending and no food budget, so Rectifi suggests one."""
    tools.add_account("Main", "1234")
    first = PINNED_TODAY.replace(day=1)
    for back in (1, 2, 3):
        month = ins._shift_month(first, back)
        tools.add_transaction("3000", "debit", month.replace(day=10), payee="Swiggy", category="Food & Dining", account="Main")


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    ask.reset()

    def script(messages):
        scripted = ScriptedModel(script=list(messages))
        monkeypatch.setattr("rectifi.agent.graph.get_llm", lambda role="chat": scripted)
        monkeypatch.setattr("rectifi.agent.llm.get_llm", lambda role="chat": scripted if role == "chat" else ScriptedStructured())   # the chat supervisor asks for a role
        return scripted

    yield script
    ask.reset()


# ───────────────────────── Review ─────────────────────────

@WIDTHS
def test_review_shows_the_week_and_the_comparison(browser, live_server, kind):
    seed_month()
    page, problems = new_page(browser, live_server, kind)
    page.goto("/review")
    page.wait_for_selector("[data-testid=review-spent]")
    review = tools.weekly_review("2026-10-19")
    from rectifi.money import fmt_inr

    assert page.inner_text("[data-testid=review-spent]") == fmt_inr(review["spending"], paise=False)
    assert page.query_selector("[data-testid=compare-slip]")
    assert page.query_selector("[data-testid=needs-attention]")
    shot(page, f"review-{tag(kind)}-light")
    no_sideways(page)
    assert not problems, problems


def test_review_commitment_saves_then_reads_as_text_with_edit(browser, live_server):
    seed_month()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/review")
    box = page.get_by_role("textbox", name="Your commitment for next week")
    box.fill("No more than 3 delivery orders")
    page.get_by_role("button", name="Save commitment").click()
    page.wait_for_selector("[data-testid=commitment-saved]")
    assert tools.weekly_review("2026-10-19")["commitment"] == "No more than 3 delivery orders"
    page.get_by_role("button", name="Edit").click()
    box.fill("x" * 241)
    assert "241 of 280" in page.inner_text("[data-testid=commitment-count]")
    box.fill("x" * 281)
    page.get_by_role("button", name="Save commitment").click()
    page.wait_for_selector("form[aria-label='Your commitment'] [role=alert]")
    assert tools.weekly_review("2026-10-19")["commitment"] == "No more than 3 delivery orders"
    assert not [p for p in problems if "pageerror" in p], problems


def test_review_shows_last_weeks_commitment_and_steps_back(browser, live_server):
    seed_month()
    tools.save_commitment("Cook at home twice", date(2026, 10, 12))
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/review")
    page.wait_for_selector("[data-testid=last-commitment]")
    assert "Cook at home twice" in page.inner_text("[data-testid=last-commitment]")
    page.goto("/review?week=2026-10-12")
    page.locator("[data-testid=review-spent]").or_(page.get_by_text("Nothing recorded this week yet.")).wait_for()
    assert not problems, problems


def test_review_empty_week(browser, live_server):
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/review")
    page.wait_for_selector("text=Nothing recorded this week yet.")
    assert not problems, problems


# ───────────────────────── Insights ─────────────────────────

@WIDTHS
def test_insights_apply_sets_the_budget_the_server_chose(browser, live_server, kind):
    history_for_insights()
    page, problems = new_page(browser, live_server, kind)
    page.goto("/insights")
    page.wait_for_selector("[data-testid=insight-row]")
    row = page.locator("[data-testid=insight-row][data-key='unbudgeted:Food & Dining']")
    shot(page, f"insights-{tag(kind)}-light")
    row.get_by_role("button").first.click()
    page.wait_for_selector("text=Budget set for Food & Dining.")
    status = tools.budget_status(tools.today().strftime("%Y-%m"))
    assert "Food & Dining" in {c["category"] for c in status["categories"]}
    no_sideways(page)
    assert not problems, problems


def test_insights_note_for_a_short_history_and_empty_state(browser, live_server):
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/insights")
    page.wait_for_selector("text=Insights need about a month of data.")
    tools.add_account("Main", "1234")
    tools.add_transaction("100", "debit", PINNED_TODAY, payee="Chai", category="Food & Dining", account="Main")
    page.goto("/insights")
    page.wait_for_selector("text=Not much history yet.")
    assert not problems, problems


def test_insights_explain_button_follows_the_model(browser, live_server, model):
    history_for_insights()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/insights")
    page.wait_for_selector("[data-testid=insight-row]")
    assert page.get_by_role("button", name="Explain this").count() > 0
    assert not problems, problems


def test_insights_without_a_model_says_so(browser, live_server, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    history_for_insights()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/insights")
    page.wait_for_selector("[data-testid=insight-row]")
    assert page.get_by_role("button", name="Explain this").count() == 0
    assert "appears when the Ask model is on" in page.inner_text("main")
    assert not problems, problems


# ───────────────────────── Phone ─────────────────────────

@WIDTHS
def test_phone_pairing_shows_the_token_once(browser, live_server, kind):
    page, problems = new_page(browser, live_server, kind)
    page.goto("/phone")
    page.wait_for_selector("text=Pair a phone")
    page.get_by_role("textbox", name="Phone name").fill("My Pixel")
    page.get_by_role("button", name="Create a pairing token").click()
    page.wait_for_selector("[data-testid=pairing-panel]")
    token_shown = page.inner_text("[data-testid=pairing-panel]")
    assert "shown once" in token_shown
    page.get_by_text("Set it up by hand instead").click()
    header = page.locator("code[aria-label='the Authorization header']").inner_text()
    token = header.split("Bearer ")[1].rstrip('"}')
    assert len(token) > 20
    assert [d["name"] for d in tools.list_devices()] == ["My Pixel"]
    shot(page, f"phone-pairing-{tag(kind)}-light")
    no_sideways(page)
    page.get_by_role("button", name="Done: hide the token").click()
    page.wait_for_selector("[data-testid=pairing-panel]", state="detached")
    assert token not in page.content()
    assert page.locator("[data-testid=device-row]", has_text="My Pixel").count() == 1
    page.reload()
    page.wait_for_selector("[data-testid=device-row]")
    assert token not in page.content()
    assert not problems, problems


def test_phone_disables_the_download_with_a_reason_when_there_is_no_address(browser, live_server):
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/phone")
    page.get_by_role("textbox", name="Phone name").fill("Tab")
    page.get_by_role("button", name="Create a pairing token").click()
    page.wait_for_selector("[data-testid=pairing-panel]")
    download = page.get_by_role("button", name="Download setup file")
    assert download.get_attribute("aria-disabled") == "true"
    assert "Tailscale" in page.inner_text("[data-testid=pairing-panel]")
    assert not problems, problems


def test_phone_revoke_asks_first(browser, live_server):
    device = tools.create_device("Old phone")
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/phone")
    page.wait_for_selector("[data-testid=device-row]")
    page.get_by_role("button", name="Revoke").click()
    page.get_by_role("dialog").get_by_role("button", name="Cancel").click()
    assert tools.list_devices()[0]["revoked_at"] is None
    page.get_by_role("button", name="Revoke").click()
    page.get_by_role("dialog").get_by_role("button", name="Revoke").click()
    page.wait_for_selector("text=Revoked")
    assert tools.list_devices()[0]["revoked_at"] is not None and device["id"]
    assert not problems, problems


def inbox_item():
    device = tools.create_device("Pixel")
    sms = "Rs.640.00 debited from A/c XX1234 on 20-10-26 to Mystery Shop UPI Ref No 429183746599"
    return tools._to_inbox(device["id"], "AX-BANK", sms, None, "hash-1", "test")["inbox_id"]


def test_phone_inbox_add_opens_the_form_filled_and_dismiss_removes(browser, live_server):
    tools.add_account("Main", "1234")
    first = inbox_item()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/phone")
    page.wait_for_selector("[data-testid=inbox-item]")
    assert "Mystery Shop" in page.inner_text("[data-testid=inbox-item]")
    page.locator("[data-testid=inbox-item]").get_by_role("link", name="Add", exact=True).click()
    page.wait_for_selector("text=Filled in from a bank SMS your phone sent.")
    assert page.get_by_label("Amount (₹)").input_value() == "640"
    page.get_by_role("button", name="Save entry").click()
    page.wait_for_selector("[data-testid=receipt]")
    assert [t for t in tools.list_transactions() if t["upi_ref"] == "429183746599"]
    assert tools.list_inbox("pending") == [] and first
    assert not problems, problems


def test_phone_inbox_dismiss(browser, live_server):
    inbox_item()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/phone")
    page.wait_for_selector("[data-testid=inbox-item]")
    page.get_by_role("button", name="Dismiss").click()
    page.wait_for_selector("text=Nothing waiting.")
    assert tools.list_inbox("pending") == []
    assert not problems, problems


def test_phone_shows_the_tailscale_login_when_the_header_is_there(browser, live_server):
    page, problems = new_page(browser, live_server, "desktop")
    page.set_extra_http_headers({"Tailscale-User-Login": "me@example.com"})
    page.goto("/phone")
    page.wait_for_selector("[data-testid=tailscale-login]")
    assert "me@example.com" in page.inner_text("[data-testid=tailscale-login]")
    assert not problems, problems


# ───────────────────────── Ask ─────────────────────────

@WIDTHS
def test_ask_a_message_saves_a_row_shows_steps_and_keeps_the_thread(browser, live_server, model, kind):
    tools.add_account("Main", "1234")
    model([
        call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}),
        AIMessage("Saved **₹250.00** at Swiggy under *Food & Dining*."),
    ])
    page, problems = new_page(browser, live_server, kind)
    page.goto("/ask")
    page.wait_for_selector("section[aria-label='Try one of these']")
    shot(page, f"ask-empty-{tag(kind)}-light")
    page.get_by_role("textbox", name="Message Rectifi").fill("spent 250 on swiggy")
    page.keyboard.press("Enter")
    page.wait_for_selector("[data-role=assistant]")
    assert "Saved ₹250.00" in page.inner_text("[data-role=assistant]")
    assert page.query_selector("[data-role=assistant] strong")
    assert len(tools.list_transactions()) == 1
    page.get_by_text("What Rectifi did").click()
    assert "log_transaction" in page.inner_text("[data-testid=ask-steps]")
    shot(page, f"ask-reply-{tag(kind)}-light")
    no_sideways(page)
    page.reload()
    page.wait_for_selector("[data-role=assistant]")
    assert page.locator("[data-role=user]").count() == 1
    assert not problems, problems


def test_ask_flags_an_invented_figure_as_not_verified(browser, live_server, model):
    model([AIMessage("You spent ₹77,777 on food this month.")])
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/ask")
    page.get_by_role("button", name="how much did I spend on food this month?").click()
    page.wait_for_selector("[data-testid=not-verified]")
    assert "₹77,777" in page.inner_text("[data-testid=not-verified]")
    assert not problems, problems


def test_ask_new_conversation_starts_empty(browser, live_server, model):
    model([AIMessage("Hello there.")])
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/ask")
    page.get_by_role("textbox", name="Message Rectifi").fill("hi")
    page.keyboard.press("Enter")
    page.wait_for_selector("[data-role=assistant]")
    page.get_by_role("button", name="New conversation").click()
    page.wait_for_selector("section[aria-label='Try one of these']")
    assert page.locator("[data-role=user]").count() == 0
    assert not problems, problems


def test_ask_markdown_in_a_reply_cannot_inject_markup(browser, live_server, model):
    model([AIMessage("<img src=x onerror=\"window.__pwned=1\"> and [x](javascript:window.__pwned=1)")])
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/ask")
    page.get_by_role("textbox", name="Message Rectifi").fill("hi")
    page.keyboard.press("Enter")
    page.wait_for_selector("[data-role=assistant]")
    assert page.query_selector("[data-role=assistant] img") is None
    assert page.query_selector("[data-role=assistant] a") is None
    assert page.evaluate("window.__pwned") is None
    assert not [p for p in problems if "pageerror" in p], problems


def test_ask_when_the_model_is_off(browser, live_server, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    ask.reset()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/ask")
    page.wait_for_selector("text=The assistant is off")
    assert "DEEPSEEK_API_KEY" in page.inner_text("main")
    shot(page, "ask-off-1280-light")
    assert not problems, problems


# ───────────────────────── every new page: targets and sideways scroll ─────────────────────────

@WIDTHS
@pytest.mark.parametrize("path", ["/review", "/insights", "/phone", "/ask"])
def test_targets_are_44px_and_nothing_scrolls_sideways(browser, live_server, model, kind, path):
    seed_month()
    history_for_insights()
    inbox_item()
    model([AIMessage("ok")])
    page, problems = new_page(browser, live_server, kind)
    page.goto(path)
    page.wait_for_selector("main h1")
    page.wait_for_timeout(600)
    no_sideways(page)
    assert page.evaluate(CHECK_TARGETS) == []
    assert not problems, problems
