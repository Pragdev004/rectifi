"""The chat window in a real browser: new chat -> stream -> reload -> resume -> search -> rename -> delete, plus the Memory and
Traces pages. Proven against the database, not just the screen.

Skipped when Playwright, Chromium or web/dist is missing, and also when the built web/dist does not contain the chat window yet
(the integrator rebuilds it with `python scripts/dev.py build-web`). To run it before that, build to a scratch folder and point
the server at it:  cd web && npx vite build --outDir /tmp/dist-chat  &&  RECTIFI_WEB_DIST=/tmp/dist-chat pytest tests/test_web_e2e_chat.py
"""

import re

import pytest
from langchain_core.messages import AIMessage
from support.chat_fakes import fake_chart, fake_rag
from support.fakes import ScriptedModel, ScriptedStructured
from web_support import browser, live_server, new_page, pinned_today, shot  # noqa: F401

from rectifi import db, tools
from rectifi.api import ask
from rectifi.api.app import default_dist

MARKER = "Search conversations"       # a string only the chat window's built code contains


def chat_is_built() -> bool:
    assets = default_dist() / "assets"
    return assets.is_dir() and any(MARKER in p.read_text(errors="ignore") for p in assets.glob("*.js"))


pytestmark = pytest.mark.skipif(not chat_is_built(), reason="the built web app does not have the chat window yet (run build-web)")


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    holder = {"agent": ScriptedModel(script=[])}
    monkeypatch.setattr("rectifi.agent.llm.get_llm", lambda role="chat": holder.get(role) or ScriptedStructured())
    monkeypatch.setattr("rectifi.agent.graph.get_llm", lambda role="chat": holder["agent"])
    monkeypatch.setattr("rectifi.agent.contracts.build_rag_subgraph", lambda **k: fake_rag("Keep three months of spending aside."))
    monkeypatch.setattr("rectifi.agent.contracts.build_chart_subgraph", lambda **k: fake_chart(5))
    ask.reset()
    yield holder
    ask.reset()


def send(page, text):
    box = page.locator("#ask-input")
    box.fill(text)
    box.press("Enter")


def row(page, title):
    """The sidebar button that opens a conversation (not its "More for ..." button)."""
    return page.get_by_role("button", name=re.compile("^" + re.escape(title)))


def count(sql, *params):
    with db.connect() as conn:
        return conn.execute(sql, params).fetchone()["n"]


def test_chat_new_stream_reload_resume_search_rename_delete(browser, live_server, model):
    model["agent"].script = [AIMessage("Saved ₹250.00 at Swiggy under Food & Dining."), AIMessage("Second answer.")]
    from test_agent import call

    model["agent"].script.insert(0, call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}))
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/ask")
    page.get_by_placeholder("Search conversations").wait_for()
    assert page.get_by_text("Your conversations will be listed here.").is_visible()

    # new chat -> the answer streams in, with a time and a route stamp
    send(page, "spent 250 on swiggy")
    page.locator("[data-role=assistant]").get_by_text("Saved ₹250.00 at Swiggy").wait_for()
    assistant = page.locator("[data-role=assistant]").last
    assert assistant.locator("time").inner_text().count(":") == 1
    assert "DID IT" in assistant.inner_text().upper()
    assert count("SELECT count(*) AS n FROM transactions") == 1                      # the browser did it, the database proves it
    row(page, "spent 250 on swiggy").wait_for()                # the sidebar row (title from the first message)
    shot(page, "chat-desktop")

    # reload -> the same conversation, full history
    page.reload()
    page.locator("[data-role=assistant]").get_by_text("Saved ₹250.00 at Swiggy").wait_for()
    assert page.locator("[data-role=user]").first.inner_text().endswith("spent 250 on swiggy")
    assert page.locator("[data-role=user] time").first.inner_text() != ""

    # a second message in the same conversation continues it
    send(page, "how much did I spend today?")
    page.locator("[data-role=assistant]").get_by_text("Second answer.").wait_for()
    assert page.locator("[data-role=user]").count() == 2
    assert count("SELECT count(*) AS n FROM conversations") == 1

    # search
    page.get_by_placeholder("Search conversations").fill("swiggy")
    row(page, "spent 250 on swiggy").wait_for()
    page.get_by_placeholder("Search conversations").fill("zzzz")
    page.get_by_text("No conversation matches that.").wait_for()
    page.get_by_placeholder("Search conversations").fill("")
    row(page, "spent 250 on swiggy").wait_for()

    # rename
    page.get_by_role("button", name="More for spent 250 on swiggy").click()
    page.get_by_role("menuitem", name="Rename").click()
    page.get_by_label("Conversation name").fill("Lunch money")
    page.get_by_role("button", name="Save").click()
    row(page, "Lunch money").wait_for()
    assert count("SELECT count(*) AS n FROM conversations WHERE title = 'Lunch money'") == 1

    # the list also has a way to start another conversation
    page.get_by_role("button", name="New conversation").first.click()
    page.get_by_text("Try").first.wait_for()

    # delete -> the row, the checkpoints and the spans go
    page.get_by_role("button", name="More for Lunch money").click()
    page.get_by_role("menuitem", name="Delete").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete").click()
    page.get_by_text("Your conversations will be listed here.").wait_for()
    assert count("SELECT count(*) AS n FROM conversations") == 0
    assert count("SELECT count(*) AS n FROM checkpoints") == 0
    assert tools.list_transactions()[0]["payee"] == "Swiggy"                         # the ledger entry stays
    assert problems == []


def test_remember_asks_first_and_the_memory_page_edits_and_forgets(browser, live_server, model):
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/ask")
    send(page, "remember that I prefer short answers")
    card = page.get_by_test_id("interrupt-card")
    card.get_by_text("Save this to memory?").wait_for()
    assert "I prefer short answers" in card.inner_text()
    card.get_by_role("button", name="Save").click()
    page.locator("[data-role=assistant]").get_by_text("Remembered: I prefer short answers").wait_for()

    page.goto("/memory")
    shot(page, "memory-desktop")
    item = page.get_by_test_id("memory-item")
    item.get_by_text("I prefer short answers").wait_for()
    item.get_by_role("button", name="Edit").click()
    page.get_by_label("What to remember").last.fill("I prefer very short answers")
    page.get_by_role("button", name="Save").last.click()
    page.get_by_text("I prefer very short answers").wait_for()
    page.get_by_role("button", name="Forget everything").click()
    page.get_by_role("dialog").get_by_role("button", name="Forget everything").click()
    page.get_by_text("Nothing is remembered yet.").wait_for()
    assert problems == []


def test_traces_page_lists_the_run_and_its_steps(browser, live_server, model):
    model["agent"].script = [AIMessage("Hello there.")]
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/ask")
    send(page, "what is an emergency fund")
    page.locator("[data-role=assistant]").get_by_text("Keep three months").wait_for()
    page.goto("/traces")
    page.get_by_role("list", name="Recent runs").get_by_role("button").first.click()
    page.get_by_test_id("trace-span").first.wait_for()
    assert page.get_by_test_id("trace-span").count() >= 5
    assert "Traces stay on this laptop." in page.inner_text("body")
    shot(page, "traces-desktop")
    assert problems == []


def test_phone_layout_has_no_sideways_scroll_and_a_conversations_sheet(browser, live_server, model):
    model["agent"].script = [AIMessage("Hi.")]
    page, problems = new_page(browser, live_server, "phone")
    page.goto("/ask")
    send(page, "how much did I spend")
    page.locator("[data-role=assistant]").get_by_text("Hi.").wait_for()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    shot(page, "chat-phone")
    page.get_by_role("button", name="Conversations").click()
    row(page.get_by_role("dialog"), "how much did I spend").wait_for()
    shot(page, "chat-phone-sheet")
    assert problems == []
