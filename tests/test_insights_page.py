"""The Insights page, the Overview strip, the Review section, "Explain this", and the agent and MCP tools."""

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_agent import ScriptedModel, call
from test_app import page_text, run_page

from rectifi import insights as ins
from rectifi import tools
from rectifi.agent import explain as explain_module
from rectifi.agent import llm as llm_module
from rectifi.agent.graph import build_graph, chat
from rectifi.insights import Insight

D = Decimal
ROOT = Path(__file__).resolve().parent.parent


def first_of_month(back: int) -> date:
    return ins._shift_month(date.today().replace(day=1), back)


def with_history():
    """Three earlier months of 3,000 on Food, an account, nothing budgeted: the page suggests a budget (date independent)."""
    tools.add_account("Main", "1234")
    for back in (1, 2, 3):
        tools.add_transaction("3000", "debit", first_of_month(back).replace(day=10), payee="Swiggy", category="Food & Dining", account="Main")


def with_unlinked_entry():
    tools.add_account("Main", "1234")
    tools.add_transaction("100", "debit", date.today(), payee="Chai", category="Food & Dining")


EXPLAIN_FOOD = "ins_explain_unbudgeted:Food & Dining"          # the "Explain this" button of the Food budget suggestion


def labels(at):
    return [b.label for b in at.button]


def codes_and_text(at) -> str:
    return page_text(at)


@pytest.fixture
def assistant_off(monkeypatch):
    monkeypatch.setattr(llm_module, "llm_status", lambda: (False, "off"))


@pytest.fixture
def assistant_on(monkeypatch):
    monkeypatch.setattr(llm_module, "llm_status", lambda: (True, ""))


class FakeModel:
    def __init__(self, reply):
        self.reply, self.seen = reply, []

    def invoke(self, messages):
        self.seen = messages
        return AIMessage(self.reply)


# ───────────────────────── the page ─────────────────────────

def test_empty_database_shows_the_designed_empty_state(assistant_off):
    at = run_page("insights")
    assert not at.exception
    text = page_text(at)
    assert "Insights need about a month of data" in text and "Import last month's bank statement" in text


def test_with_data_the_page_groups_findings_and_every_chip_has_a_word(assistant_off):
    with_unlinked_entry()
    at = run_page("insights")
    assert not at.exception
    text = page_text(at)
    assert "Fix these first" in text and "1 entry is not linked to an account" in text
    assert "Act</span>" in text                                                   # the chip says "Act", colour is not the only signal
    assert "Rectifi has 1 day of data" in text                                    # fewer than 28 days: says what is quiet and why


def test_the_page_works_with_no_assistant_and_says_so(assistant_off):
    with_unlinked_entry()
    at = run_page("insights")
    assert "Explain this" not in labels(at)
    assert "“Explain this” appears when the Ask model is on" in page_text(at)


def test_a_suggested_budget_is_applied_only_when_you_press_the_button(assistant_off):
    with_history()
    at = run_page("insights")
    assert not at.exception
    assert tools.budget_status(date.today())["categories"] == []                  # nothing changed just by looking
    button = next(b for b in at.button if b.label == "Set a ₹3,000 budget")
    button.click().run()
    assert not at.exception
    assert [(b["category"], b["limit"]) for b in tools.budget_status(date.today())["categories"]] == [("Food & Dining", D("3000.00"))]
    assert not any(b.label == "Set a ₹3,000 budget" for b in at.button)           # now it has a budget, so no suggestion


# ───────────────────────── "Explain this" ─────────────────────────

def test_explain_shows_advice_when_every_figure_comes_from_the_insight(assistant_on, monkeypatch):
    with_history()
    model = FakeModel("Food averages ₹3,000 a month, so a ₹3,000 budget would hold you to your usual. Start there.")
    monkeypatch.setattr(explain_module, "get_llm", lambda: model)
    at = run_page("insights")
    at.button(key=EXPLAIN_FOOD).click().run()
    assert not at.exception
    assert "a ₹3,000 budget would hold you to your usual" in page_text(at)
    assert "checked against Rectifi's own numbers" in page_text(at)
    sent = "\n".join(str(m.content) for m in model.seen)
    assert "suggested_budget: ₹3,000" in sent and "prior_months: 3" in sent        # money as rupees, counts as plain numbers


def test_explain_throws_away_advice_that_uses_a_figure_rectifi_did_not_calculate(assistant_on, monkeypatch):
    with_history()
    monkeypatch.setattr(explain_module, "get_llm", lambda: FakeModel("Cut ₹750 a month from Food and you will be fine."))
    at = run_page("insights")
    at.button(key=EXPLAIN_FOOD).click().run()
    text = page_text(at)
    assert "Cut ₹750" not in text
    assert "Not shown" in text and "₹750" in text and "did not calculate" in text


def test_explain_failure_is_a_plain_message_not_a_crash(assistant_on, monkeypatch):
    with_history()

    class Broken:
        def invoke(self, messages):
            raise RuntimeError("boom")

    monkeypatch.setattr(explain_module, "get_llm", lambda: Broken())
    at = run_page("insights")
    at.button(key=EXPLAIN_FOOD).click().run()
    assert not at.exception


def insight(**numbers) -> Insight:
    return Insight("x", "watch", "Title", "Detail", {k: D(v) for k, v in numbers.items()}, key="x")


def test_explain_checks_only_money_numbers_so_a_count_is_not_a_rupee_figure():
    item = insight(spent=1200, over_count=25)
    assert explain_module.not_from_numbers("You spent ₹1,200.", item) == []
    assert explain_module.not_from_numbers("That is ₹25 too much.", item) == [D("25.00")]
    assert explain_module.not_from_numbers("Rs. 1200 and INR 1,200.00", item) == []
    assert explain_module.not_from_numbers("No figure here at all.", item) == []


def test_explain_masks_account_numbers_before_the_model_sees_them():
    item = Insight("x", "watch", "₹500 to 123456789012 on 18 Oct", "Paid UPI ref 429183746521 from A/c 50100123456789.", {"amount": D(500)}, key="x")
    prompt = explain_module.prompt_for(item)
    assert "429183746521" not in prompt and "50100123456789" not in prompt and "amount: ₹500" in prompt


def test_explain_returns_the_three_outcomes():
    item = insight(spent=1200)
    ok = explain_module.explain(item, FakeModel("Spend a little less."))
    assert ok == {"text": "Spend a little less.", "dropped": [], "error": None}
    dropped = explain_module.explain(item, FakeModel("Save ₹300."))
    assert dropped["text"] is None and dropped["dropped"] == [D("300.00")]
    empty = explain_module.explain(item, FakeModel("   "))
    assert empty["text"] is None and empty["error"]


# ───────────────────────── Overview and Review ─────────────────────────

def test_overview_shows_the_top_suggestions_with_a_link(assistant_off):
    with_unlinked_entry()
    at = run_page("overview")
    assert not at.exception
    text = page_text(at)
    assert "Top suggestions" in text and "1 entry is not linked to an account" in text
    assert "Act</span>" in text


def test_overview_has_no_strip_when_there_is_nothing_to_suggest(assistant_off):
    tools.add_account("Main", "1234")
    tools.record_balance("Main", date.today().replace(day=1), "1000")
    tools.add_transaction("5000", "credit", date.today(), payee="Stipend", category="Salary & Allowance", account="Main")
    at = run_page("overview")
    assert not at.exception and "Top suggestions" not in page_text(at)


def test_overview_survives_a_broken_rule(monkeypatch, assistant_off):
    with_unlinked_entry()

    def broken(limit=3, today=None):
        raise ValueError("bug in one rule")

    monkeypatch.setattr(ins, "top_insights", broken)
    at = run_page("overview")
    assert not at.exception
    assert "Suggestions could not be worked out" in page_text(at)


def test_review_shows_only_the_act_level_insights(assistant_off):
    with_unlinked_entry()                                                          # act: unlinked.
    tools.add_transaction("100", "debit", date.today(), payee="Mystery", account="Main")   # lands in Other: only a watch/info
    text = page_text(run_page("review"))
    assert "Insights worth acting on" in text and "1 entry is not linked to an account" in text
    assert "sit in “Other”" not in text and "sits in “Other”" not in text


def test_review_has_no_insights_section_without_act_level_items(assistant_off):
    tools.add_account("Main", "1234")
    tools.add_transaction("100", "debit", date.today(), payee="Chai", category="Food & Dining", account="Main")
    assert "Insights worth acting on" not in page_text(run_page("review"))


def test_insights_is_second_after_review_in_the_navigation():
    import re

    source = (ROOT / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    order = re.findall(r'^    "(\w+)": st\.Page\(', source, flags=re.M)
    assert order[:3] == ["overview", "review", "insights"]


# ───────────────────────── the agent tool and the MCP tool ─────────────────────────

def test_agent_insights_tool_returns_the_deterministic_findings():
    with_unlinked_entry()
    model = ScriptedModel(script=[call("insights", {}), AIMessage("You have one entry to link.")])
    new = chat(build_graph(model), "how can I manage my money better?", "i1")
    out = next(m for m in new if isinstance(m, ToolMessage)).content
    data = json.loads(out)
    assert data["count"] >= 1 and data["insights"][0]["title"] == "1 entry is not linked to an account"
    assert set(data["insights"][0]) == {"severity", "kind", "title", "detail", "where_to_act"}


def test_agent_insights_tool_with_no_data_says_so():
    model = ScriptedModel(script=[call("insights", {}), AIMessage("Nothing yet.")])
    new = chat(build_graph(model), "what should I fix?", "i2")
    assert json.loads(next(m for m in new if isinstance(m, ToolMessage)).content)["count"] == 0


def test_a_figure_from_the_insights_tool_counts_as_verified():
    from rectifi.agent.graph import unverified_amounts

    tools.add_account("Main", "1234")
    for _ in range(3):
        tools.add_transaction("100", "debit", date.today(), payee="Mystery", account="Main")
    model = ScriptedModel(script=[call("insights", {}), AIMessage("Three payments, ₹300 in all, sit in Other.")])
    new = chat(build_graph(model), "what should I fix?", "i3")
    assert unverified_amounts(new) == []


def test_mcp_insights_tool_is_read_only_and_returns_findings():
    from mcp import Client

    from rectifi.mcp_server import server

    with_unlinked_entry()

    async def go():
        async with Client(server) as client:
            listed = {t.name: t for t in (await client.list_tools()).tools}
            result = await client.call_tool("insights", {})
        return listed, result.content[0].text

    listed, text = asyncio.run(go())
    assert "insights" in listed and listed["insights"].annotations.read_only_hint is True
    assert listed["insights"].annotations.destructive_hint is False
    payload = json.loads(text)
    assert payload["count"] >= 1 and payload["insights"][0]["kind"] == "unlinked"
    assert tools.list_transactions(limit=5)[0]["payee"] == "Chai"                  # nothing was written
