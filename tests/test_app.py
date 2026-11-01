"""Smoke tests for the Streamlit app, using Streamlit's AppTest (no browser, no server)."""

import re
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from streamlit.util import calc_hash

from rectifi import tools

APP = Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py"
sys.path.insert(0, str(APP.parent))   # the app does `from theme import ...`, like `streamlit run` allows


def run_app(timeout: int = 30) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=timeout)
    at.run()
    return at


def run_page(url_path: str, timeout: int = 30) -> AppTest:
    """Open one page of the app, like clicking it in the sidebar.

    AppTest can only switch pages by file name, but Rectifi's pages are functions. A page's id is the hash of
    its url_path, so we set that id directly. (It uses a private AppTest field; if a Streamlit upgrade breaks
    it, this is the one place to fix.)
    """
    at = AppTest.from_file(str(APP), default_timeout=timeout)
    at._page_hash = calc_hash(url_path)
    at.run()
    return at


def page_text(at: AppTest) -> str:
    parts = [e.value for e in at.markdown] + [e.value for e in at.title] + [e.value for e in at.caption]
    parts += [e.value for e in at.error] + [e.value for e in at.warning] + [e.value for e in at.info]
    return "\n".join(parts)


def test_empty_database_renders():
    at = run_app()
    assert not at.exception
    assert "Nothing recorded for this month yet" in page_text(at)


def test_income_only_month_renders():
    """Regression: a month with income but no spending used to crash the overview chart."""
    tools.add_transaction("9000", "credit", date.today(), payee="Stipend", category="Salary & Allowance")
    at = run_app()
    assert not at.exception
    text = page_text(at)
    assert "No spending this month" in text
    assert "9,000" in text


def test_overview_with_spending_renders():
    tools.add_transaction("9000", "credit", date.today(), payee="Stipend", category="Salary & Allowance")
    tools.add_transaction("250", "debit", date.today(), payee="Swiggy", source="chat")
    at = run_app()
    assert not at.exception
    assert "Swiggy" in page_text(at)


def test_database_down_shows_message_quickly(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://rectifi:topsecretpw@127.0.0.1:9/nodb")
    started = time.monotonic()
    at = run_app(timeout=20)
    assert time.monotonic() - started < 15
    assert not at.exception
    text = page_text(at)
    assert "can't reach its database" in text
    assert "scripts/dev.py db" in text
    assert "topsecretpw" not in text


def test_ask_page_degrades_when_model_is_off(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    at = run_page("ask")
    assert not at.exception
    text = page_text(at)
    assert "DEEPSEEK_API_KEY" in text
    assert "The assistant is off" in text


def seed_review_week(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: date(2026, 10, 21))   # Wednesday
    tools.add_transaction("5000", "credit", "2026-10-19", payee="Stipend", category="Salary & Allowance")
    tools.add_transaction("600", "debit", "2026-10-13", payee="Swiggy", source="chat")
    tools.add_transaction("1500", "debit", "2026-10-20", payee="Amazon", source="chat")
    tools.add_transaction("300", "debit", "2026-10-21", payee="Corner shop", source="chat")
    tools.set_budget("Shopping", "1000", "2026-10")
    tools.add_account("Savings", "1111")
    tools.record_balance("Savings", "2026-10-19", "1000")


def test_review_page_renders_with_data(monkeypatch):
    seed_review_week(monkeypatch)
    at = run_page("review")
    assert not at.exception
    text = page_text(at)
    assert "Weekly review" in text
    assert "Spent this week" in text and "64.0%" in text            # (5000 - 1800) / 5000
    assert "Amazon" in text and "Corner shop" in text              # biggest payments
    assert "Over budget" in text                                   # Shopping 1500 of 1000, said in words
    assert "still in “Miscellaneous”" in text                              # the corner shop has no category rule
    assert "No two bank balances" in text
    assert "Up" in text                                            # up vs the same days last week
    assert at.selectbox[0].label == "Week"


def test_review_page_with_zero_accounts_is_not_all_clear(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: date(2026, 10, 21))
    tools.add_transaction("250", "debit", "2026-10-20", payee="Swiggy", source="chat")
    at = run_page("review")
    assert not at.exception
    text = page_text(at)
    assert "All clear" not in text
    assert "No bank account added yet" in text
    assert "Check your Accounts" in " ".join(str(l.label) for l in at.get("page_link"))
    assert "Nothing to compare" in text
    assert "= change" not in text and "+₹250" not in text         # no change row and no "+" amount beside "Nothing to compare"


def test_review_page_unlinked_entries_are_reported_with_a_ledger_link(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: date(2026, 10, 21))
    tools.add_account("Savings", "1111")
    tools.record_balance("Savings", "2026-10-19", "1000")
    tools.record_balance("Savings", "2026-10-21", "1000")
    tools.add_transaction("250", "debit", "2026-10-20", payee="Swiggy", source="chat")   # no account
    at = run_page("review")
    assert not at.exception
    text = page_text(at)
    assert "All clear" not in text
    assert "1 entry this week isn’t linked to an account" in text
    assert any("Ledger" in str(l.label) for l in at.get("page_link"))


def test_review_page_all_clear_in_the_clean_case(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: date(2026, 10, 21))
    tools.add_account("Savings", "1111")
    tools.record_balance("Savings", "2026-10-19", "1000")
    tools.record_balance("Savings", "2026-10-21", "750")
    tools.add_transaction("250", "debit", "2026-10-20", payee="Swiggy", account="Savings", source="chat")
    at = run_page("review")
    assert not at.exception
    assert "All clear" in page_text(at)


def test_review_change_row_hidden_when_previous_spending_is_zero(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: date(2026, 10, 21))
    tools.add_transaction("150", "debit", "2026-10-20", payee="Swiggy", source="chat")     # nothing in 12 to 14 Oct
    text = page_text(run_page("review"))
    assert "Nothing to compare" in text and "No spending was recorded for 12 Oct – 14 Oct" in text
    assert "= change" not in text and "+₹150.00" not in text
    assert "Up " not in text and "%" not in text.split("Compared with")[1].split("Where it went")[0]


def test_review_change_row_shown_when_there_is_something_to_compare(monkeypatch):
    seed_review_week(monkeypatch)                      # 600 the week before, 1800 this week so far
    text = page_text(run_page("review"))
    assert "= change" in text and "+₹1,200.00" in text and "Nothing to compare" not in text


def test_add_page_preselects_the_only_account():
    tools.add_account("Savings", "1111")
    at = run_page("add")
    assert not at.exception
    acct = next(s for s in at.selectbox if s.label == "Account")
    assert acct.value == "Savings"
    assert acct.options[-1] == "No account"            # still possible, but only as an explicit choice


def test_add_page_preselects_the_most_recently_used_account():
    tools.add_account("Savings", "1111")
    tools.add_account("Wallet", "2222")
    tools.add_transaction("40", "debit", date.today(), payee="Tea", account="Wallet")
    acct = next(s for s in run_page("add").selectbox if s.label == "Account")
    assert acct.value == "Wallet"


def test_add_page_quick_entry_is_linked_by_default_and_unlinked_only_by_choice():
    tools.add_account("Savings", "1111")
    at = run_page("add")
    at.text_input[0].set_value("250")
    at.button[0].click().run()
    assert not at.exception
    [t] = tools.list_transactions()
    assert t["account"] == "Savings"

    at = run_page("add")
    at.text_input[0].set_value("60")
    next(s for s in at.selectbox if s.label == "Account").select("No account")
    at.button[0].click().run()
    assert tools.list_transactions(limit=1)[0]["account"] is None


def test_add_page_without_accounts_offers_only_no_account():
    acct = next(s for s in run_page("add").selectbox if s.label == "Account")
    assert acct.options == ["No account"] and acct.value == "No account"


def test_review_page_empty_week_renders(monkeypatch):
    monkeypatch.setattr(tools, "today", lambda: date(2026, 10, 21))
    at = run_page("review")
    assert not at.exception
    assert "Nothing recorded this week yet" in page_text(at)
    assert at.text_input[0].label == "Your commitment for next week"      # the form is still there


def test_review_page_saves_commitment_and_shows_it_next_week(monkeypatch):
    seed_review_week(monkeypatch)
    at = run_page("review")
    at.text_input[0].set_value("No more than 3 delivery orders")
    at.button[0].click().run()
    assert not at.exception
    assert tools.weekly_review()["commitment"] == "No more than 3 delivery orders"

    monkeypatch.setattr(tools, "today", lambda: date(2026, 10, 28))
    nxt = run_page("review")
    assert not nxt.exception
    assert "What you committed to last week" in page_text(nxt)
    assert "No more than 3 delivery orders" in page_text(nxt)


def test_review_page_empty_commitment_shows_error(monkeypatch):
    seed_review_week(monkeypatch)
    at = run_page("review")
    at.button[0].click().run()
    assert not at.exception
    assert any("one short sentence" in e.value for e in at.error)


def test_review_is_second_in_the_navigation():
    source = APP.read_text(encoding="utf-8")
    order = re.findall(r'^    "(\w+)": st\.Page\(', source, flags=re.M)
    assert order[:2] == ["overview", "review"]


def test_ask_page_warns_about_a_figure_the_model_made_up(monkeypatch):
    import streamlit as st
    from langchain_core.messages import AIMessage
    from test_agent import ScriptedModel  # the same scripted fake model the agent tests use

    from rectifi.agent import graph as graph_module

    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    model = ScriptedModel(script=[AIMessage("You are doing great, you saved ₹77,777 this year.")])
    monkeypatch.setattr(graph_module, "get_llm", lambda: model)
    st.cache_resource.clear()                         # the app caches its graph; start clean

    at = run_page("ask")
    at.chat_input[0].set_value("how am I doing?").run()
    assert not at.exception
    captions = " ".join(c.value for c in at.caption)
    assert "Not verified" in captions and "77,777" in captions
    st.cache_resource.clear()


@pytest.mark.parametrize("url_path", ["overview", "review", "insights", "ask", "add", "ledger", "budgets", "accounts", "phone"])
def test_every_page_renders_with_data(url_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)           # the Ask page shows its "assistant is off" message
    tools.add_account("Savings", "1111")
    tools.record_balance("Savings", date.today().replace(day=1), "1000")
    tools.add_transaction("5000", "credit", date.today(), payee="Stipend", category="Salary & Allowance", account="Savings")
    tools.add_transaction("250", "debit", date.today(), payee="Swiggy", account="Savings", source="chat")
    tools.set_budget("Food & Dining", "1000")
    at = run_page(url_path)
    assert not at.exception, [e.value for e in at.exception]


def test_ledger_notice_when_list_is_cut_off(monkeypatch):
    monkeypatch.setattr(tools, "list_transactions", lambda *a, **k: [
        {"id": i, "txn_date": date.today(), "payee": "x", "description": None, "category": "Miscellaneous", "direction": "debit",
         "amount": Decimal("1"), "source": "chat", "account": None, "note": None}
        for i in range(1000)
    ])
    at = run_page("ledger")
    assert not at.exception
    assert any("newest 1000" in w.value for w in at.warning)


def test_ask_page_explains_a_missing_openrouter_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    at = run_page("ask")
    assert not at.exception
    warnings = " ".join(w.value for w in at.warning)
    assert "OPENROUTER_MODEL" in warnings and "free-models" in warnings
    assert any("openrouter" in c.value and "no model set" in c.value for c in at.caption)     # provider shown in the caption
    assert "sk-or-test" not in warnings


def test_ask_page_warns_that_huggingface_needs_credits(monkeypatch):
    import streamlit as st

    st.cache_resource.clear()
    monkeypatch.setenv("LLM_PROVIDER", "huggingface")
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    monkeypatch.delenv("HF_MODEL", raising=False)                    # a real .env may set one; we want the default
    at = run_page("ask")
    assert not at.exception
    assert any("not free" in i.value.lower() and "$0" in i.value for i in at.info)
    assert any("huggingface" in c.value and "DeepSeek-V3.1:cheapest" in c.value for c in at.caption)
    st.cache_resource.clear()
