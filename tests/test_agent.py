"""Agent tests with a scripted fake model — no API key, fully deterministic.

They test the harness, not the LLM: routing, tool execution, and the amount guard.
"""

import re
from decimal import Decimal

from langchain_core.messages import AIMessage, ToolMessage

from rectifi import tools
from rectifi.agent.graph import build_graph, chat, unverified_amounts
from support.fakes import ScriptedModel  # noqa: F401  (moved to tests/support/fakes.py; other tests import it from here)


def call(name, args, id="c1"):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": id, "type": "tool_call"}])


def test_logs_expense_end_to_end():
    model = ScriptedModel(script=[
        call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}),
        AIMessage("Saved ₹250 at Swiggy under Food & Dining."),
    ])
    new = chat(build_graph(model), "spent 250 on swiggy", "t1")
    tool_msg = next(m for m in new if isinstance(m, ToolMessage))
    assert '"added"' in tool_msg.content
    rows = tools.list_transactions()
    assert len(rows) == 1 and rows[0]["amount"] == Decimal("250.00") and rows[0]["category"] == "Food & Dining"


def test_guard_blocks_invented_amount():
    model = ScriptedModel(script=[
        call("log_transaction", {"amount": "2500", "direction": "debit", "payee": "Swiggy"}),  # hallucinated ×10
        AIMessage("Sorry — how much exactly?"),
    ])
    new = chat(build_graph(model), "spent 250 on swiggy", "t2")
    tool_msg = next(m for m in new if isinstance(m, ToolMessage))
    assert tool_msg.content.startswith("GUARD")
    assert tools.list_transactions() == []


def test_guard_allows_amount_from_earlier_turn():
    model = ScriptedModel(script=[
        AIMessage("Was the ₹500 for food or travel?"),
        call("log_transaction", {"amount": "500", "direction": "debit", "category": "Travel"}),
        AIMessage("Saved."),
    ])
    graph = build_graph(model)
    chat(graph, "paid 500", "t3")
    chat(graph, "travel", "t3")
    assert tools.list_transactions()[0]["category"] == "Travel"


def test_tool_error_reaches_model_not_crash():
    model = ScriptedModel(script=[
        call("set_budget", {"category": "Moon trips", "amount": "3000"}),
        AIMessage("That category doesn't exist."),
    ])
    new = chat(build_graph(model), "budget 3000 for moon trips", "t4")
    tool_msg = next(m for m in new if isinstance(m, ToolMessage))
    assert "Unknown category" in tool_msg.content


def test_sms_tool():
    sms = "Rs.250.00 debited from A/c XX1234 on 01-10-26 to VPA swiggy@icici UPI Ref No 429183746521."
    model = ScriptedModel(script=[call("log_sms", {"sms_text": sms}), AIMessage("Saved.")])
    chat(build_graph(model), sms, "t5")
    row = tools.list_transactions()[0]
    assert row["upi_ref"] == "429183746521" and row["source"] == "sms"


SMS = "Rs.250.00 debited from A/c 0012345678901 on 01-10-26 to VPA swiggy@icici UPI Ref No 429183746521."


def _seen_text(model) -> str:
    return " ".join(str(m.content) + str(getattr(m, "tool_calls", "")) for m in model.seen)


def test_model_never_sees_account_numbers():
    # The model only ever sees the masked SMS, so what it sends to log_sms is the masked text.
    masked = "Rs.250.00 debited from A/c XXXXXXXXX8901 on 01-10-26 to VPA swiggy@icici UPI Ref No XXXXXXXX6521."
    model = ScriptedModel(script=[call("log_sms", {"sms_text": masked}), AIMessage("Saved.")])
    chat(build_graph(model), SMS, "p1")
    assert not re.search(r"\d{9,}", _seen_text(model))
    row = tools.list_transactions()[0]
    assert row["upi_ref"] == "429183746521"                      # the real reference was saved...
    with tools.connect() as conn:                                  # ...and the real text stays on this laptop
        raw = conn.execute("SELECT raw_text FROM transactions").fetchone()["raw_text"]
    assert "0012345678901" in raw


def test_model_never_sees_card_numbers_in_tool_results():
    tools.add_transaction("300", "debit", payee="POS 416021XXXXXX4417 DMART", source="chat")
    model = ScriptedModel(script=[call("find_transactions", {"search": "DMART"}), AIMessage("Found it.")])
    chat(build_graph(model), "find my dmart payment", "p2")
    assert "416021XXXXXX4417" not in _seen_text(model)
    assert "4417" in _seen_text(model)


def test_log_sms_rejects_text_user_never_pasted():
    invented = "Rs.250.00 debited from A/c XX1234 on 01-10-26 to VPA swiggy@icici UPI Ref No 111122223333."
    model = ScriptedModel(script=[call("log_sms", {"sms_text": invented}), AIMessage("Please paste it again.")])
    new = chat(build_graph(model), "I paid swiggy 250 earlier, log it", "p3")
    tool_msg = next(m for m in new if isinstance(m, ToolMessage))
    assert tool_msg.content.startswith("GUARD") and "paste" in tool_msg.content
    assert tools.list_transactions() == []


def _only_tool_message(new):
    return next(m for m in new if isinstance(m, ToolMessage))


def test_guard_blocks_invented_budget():
    model = ScriptedModel(script=[
        call("set_budget", {"category": "Food & Dining", "amount": "9000"}),   # user never typed 9000
        AIMessage("How much should the budget be?"),
    ])
    new = chat(build_graph(model), "set a budget for food", "g1")
    assert _only_tool_message(new).content.startswith("GUARD")
    assert tools.budget_status()["categories"] == []


def test_guard_accepts_2_5k_as_2500():
    model = ScriptedModel(script=[
        call("log_transaction", {"amount": "2500", "direction": "debit", "payee": "Landlord", "category": "Rent"}),
        AIMessage("Saved."),
    ])
    new = chat(build_graph(model), "paid 2.5k rent", "g2")
    assert '"added"' in _only_tool_message(new).content
    assert tools.list_transactions()[0]["amount"] == Decimal("2500.00")


def test_guard_forgets_amounts_older_than_lookback():
    model = ScriptedModel(script=[
        AIMessage("ok"), AIMessage("ok"), AIMessage("ok"), AIMessage("ok"),
        call("log_transaction", {"amount": "500", "direction": "debit"}),
        AIMessage("Sorry, how much?"),
    ])
    graph = build_graph(model)
    for text in ["paid 500", "hello", "thanks", "never mind"]:   # 500 is now 4 messages back
        chat(graph, text, "g3")
    new = chat(graph, "log it", "g3")
    assert _only_tool_message(new).content.startswith("GUARD")
    assert tools.list_transactions() == []


def test_guard_rejects_non_numeric_amount():
    model = ScriptedModel(script=[
        call("log_transaction", {"amount": "a lot", "direction": "debit"}),
        AIMessage("How much exactly?"),
    ])
    new = chat(build_graph(model), "spent a lot on food", "g4")
    msg = _only_tool_message(new).content
    assert msg.startswith("GUARD") and "not a plain number" in msg
    assert tools.list_transactions() == []


def test_weekly_review_tool_returns_formatted_numbers():
    tools.add_transaction("5000", "credit", payee="Stipend", category="Salary & Allowance")
    tools.add_transaction("1250", "debit", payee="Amazon", source="chat")
    model = ScriptedModel(script=[call("weekly_review", {"week": "this_week"}), AIMessage("Here is your week.")])
    new = chat(build_graph(model), "how was my week?", "w1")
    out = _only_tool_message(new).content
    assert "₹1,250.00" in out and "₹3,750.00" in out and '"savings_rate": "75.0%"' in out


def test_commitment_saved_in_the_users_own_words():
    model = ScriptedModel(script=[
        call("save_commitment", {"text": "No more than 3 delivery orders"}), AIMessage("Saved your commitment."),
    ])
    chat(build_graph(model), "my commitment is no more than 3 delivery orders", "w2")
    assert tools.weekly_review()["commitment"] == "No more than 3 delivery orders"


def test_commitment_with_invented_number_is_blocked():
    model = ScriptedModel(script=[
        call("save_commitment", {"text": "Spend under 4000 on food"}), AIMessage("What number do you want?"),
    ])
    new = chat(build_graph(model), "help me commit to spending less on food", "w3")
    assert _only_tool_message(new).content.startswith("GUARD")
    assert tools.weekly_review()["commitment"] is None


def _turn(user_text, script, thread):
    """Run one turn and return everything the graph holds (so unverified_amounts sees the whole chat)."""
    graph = build_graph(ScriptedModel(script=script))
    chat(graph, user_text, thread)
    return graph.get_state({"configurable": {"thread_id": thread}}).values["messages"]


def test_reply_figures_from_user_and_tools_are_verified():
    messages = _turn("spent 250 on swiggy", [
        call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}),
        AIMessage("Saved ₹250.00 at Swiggy."),
    ], "v1")
    assert unverified_amounts(messages) == []


def test_reply_with_a_made_up_figure_is_flagged():
    messages = _turn("spent 250 on swiggy", [
        call("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}),
        AIMessage("Saved ₹250. That brings your food spending to ₹9,999 this month."),
    ], "v2")
    assert unverified_amounts(messages) == [Decimal("9999")]


def test_reply_figure_from_a_tool_result_is_verified():
    tools.add_transaction("1250", "debit", payee="Amazon", source="chat")
    messages = _turn("how much did I spend this month?", [
        call("spending_report", {"period": "this_month"}),
        AIMessage("You spent Rs.1,250 this month."),
    ], "v3")
    assert unverified_amounts(messages) == []


def test_reply_figure_computed_by_the_model_is_flagged():
    tools.add_transaction("1250", "debit", payee="Amazon", source="chat")
    messages = _turn("how much did I spend this month?", [
        call("spending_report", {"period": "this_month"}),
        AIMessage("You spent ₹1,250, so about ₹42 a day."),                 # the daily figure is the model's own maths
    ], "v4")
    assert unverified_amounts(messages) == [Decimal("42")]


def test_user_typed_2_5k_counts_as_verified():
    messages = _turn("is 2.5k too much for rent?", [AIMessage("₹2,500 for rent is for you to judge.")], "v5")
    assert unverified_amounts(messages) == []


def test_no_figures_means_nothing_to_flag():
    assert unverified_amounts(_turn("hi", [AIMessage("Hello! Tell me what you spent.")], "v6")) == []
