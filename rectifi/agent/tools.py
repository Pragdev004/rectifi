"""The tools the agent may call. Thin wrappers: they translate between LLM-friendly strings and the
deterministic functions in rectifi.tools, and they format results so the model never has to do arithmetic.
"""

from __future__ import annotations

import functools
import json
import re
from datetime import date
from decimal import Decimal
from typing import Literal

from langchain_core.tools import tool

from .. import tools as core
from ..money import fmt_inr
from ..parsing import parse_sms


def _range(period: str | None) -> tuple[date, date]:
    period = (period or "this_month").strip()
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2})", period)
    if match:
        return core.resolve_date(match.group(1)), core.resolve_date(match.group(2))
    return core.month_bounds(period)


def _txn_line(t: dict) -> str:
    sign = "−" if t["direction"] == "debit" else "+"
    who = t.get("payee") or t.get("description") or "—"
    return f"#{t['id']} {t['txn_date']} {sign}{fmt_inr(t['amount'])} {who} [{t['category']}]"


def _out(payload) -> str:
    def default(o):
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, date):
            return o.isoformat()
        raise TypeError(type(o).__name__)

    return json.dumps(payload, default=default, ensure_ascii=False)


def _safe(fn):
    """Errors become a message the model can read and react to (e.g. ask the user), not a crash."""

    @functools.wraps(fn)  # keeps the signature + docstring, which @tool turns into the JSON schema the LLM sees
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except core.RectifiError as exc:
            return _out({"error": str(exc)})

    return wrapper


@tool
@_safe
def log_transaction(
    amount: str,
    direction: Literal["debit", "credit"],
    payee: str | None = None,
    category: str | None = None,
    date: str = "today",
    note: str | None = None,
    account: str | None = None,
) -> str:
    """Record ONE transaction the user told you about.

    amount: exactly the number the user typed (e.g. "250", "1,240.50", "2.5k" -> "2500"). Never estimate or compute it.
    direction: "debit" if money left the user (spent, paid, sent); "credit" if money came in (received, salary, refund).
    payee: who was paid / who paid, as the user said it (e.g. "Swiggy").
    category: one of the category names from list_categories. Leave empty to let Rectifi's rules decide.
    date: "today", "yesterday" or YYYY-MM-DD.
    """
    if not account:
        account = core.only_account_id()      # decided by code, not the model: one account means it can only be that one
    result = core.add_transaction(
        amount, direction, date, payee=payee, category=category, account=account, note=note, source="chat"
    )
    t = result["transaction"]
    return _out({"status": result["status"], "saved": _txn_line(t)})


@tool
@_safe
def log_sms(sms_text: str) -> str:
    """Record a transaction from a bank SMS the user pasted. Pass the SMS text as you see it.

    Long account and reference numbers are hidden from you on purpose. Rectifi looks up the original SMS
    the user pasted and parses that itself, so you cannot save text the user never pasted.
    """
    parsed = parse_sms(sms_text)
    if parsed is None:
        return _out({"error": "That doesn't look like a bank transaction SMS (no amount or debit/credit word)."})
    account = None
    if parsed.account_last4:
        known = [a for a in core.list_accounts() if a["last4"] and a["last4"][-4:] == parsed.account_last4]
        account = known[0]["id"] if known else None
    else:
        account = core.only_account_id()      # the SMS names no account; with exactly one account it can only be that one
    result = core.add_transaction(
        parsed.amount, parsed.direction, parsed.txn_date, payee=parsed.payee, account=account,
        source="sms", upi_ref=parsed.upi_ref, raw_text=sms_text,
    )
    return _out({"status": result["status"], "saved": _txn_line(result["transaction"]), "warnings": parsed.warnings})


@tool
@_safe
def spending_report(period: str = "this_month") -> str:
    """Income, spending, amount saved, savings rate and per-category totals.

    period: "this_month", "last_month", "YYYY-MM", or "YYYY-MM-DD..YYYY-MM-DD".
    Use the numbers exactly as returned — do not add, subtract or round them yourself.
    """
    start, end = _range(period)
    s = core.spending_summary(start, end)
    return _out({
        "from": start, "to": end,
        "income": fmt_inr(s["income"]), "spending": fmt_inr(s["spending"]), "saved": fmt_inr(s["saved"]),
        "savings_rate": f"{s['savings_rate']}%" if s["savings_rate"] is not None else "no income recorded",
        "transactions": s["count"],
        "by_category": [{"category": r["category"], "spent": fmt_inr(r["amount"]), "share": f"{r['share']}%"} for r in s["by_category"]],
    })


@tool
@_safe
def find_transactions(search: str | None = None, category: str | None = None, period: str | None = None, limit: int = 10) -> str:
    """Look up individual transactions — e.g. 'when did I last pay the electricity bill'. All filters optional.

    search: text matched against payee, narration and notes. period: as in spending_report; omit for all time.
    """
    start, end = _range(period) if period else (None, None)
    rows = core.list_transactions(start, end, category=category, search=search, limit=min(limit, 25))
    return _out({"count": len(rows), "transactions": [_txn_line(t) for t in rows]})


@tool
@_safe
def set_budget(category: str, amount: str, month: str = "this_month") -> str:
    """Set a monthly spending limit for one category. amount: exactly the number the user typed."""
    b = core.set_budget(category, amount, month)
    return _out({"status": "saved", "category": b["category"], "month": b["month"], "limit": fmt_inr(b["limit"])})


@tool
@_safe
def check_budgets(month: str = "this_month") -> str:
    """How each budgeted category is doing: limit, spent, remaining, percent used, and state ok/near/over.
    'pace' is how far through the month we are, in percent."""
    s = core.budget_status(month)
    return _out({
        "month": s["month"], "pace": f"{s['pace']}% of the month gone",
        "categories": [
            {"category": r["category"], "limit": fmt_inr(r["limit"]), "spent": fmt_inr(r["spent"]),
             "remaining": fmt_inr(r["remaining"]), "used": f"{r['percent']}%", "state": r["state"]}
            for r in s["categories"]
        ] or "no budgets set for this month",
    })


@tool
@_safe
def recategorize(transaction_id: int, category: str, remember: bool = True) -> str:
    """Move a transaction to another category. remember=True also teaches Rectifi to use that category for this payee from now on."""
    r = core.set_category(transaction_id, category, remember)
    return _out({"status": "updated", "transaction": _txn_line(r["transaction"]), "learned_rule": r["learned_rule"]})


@tool
@_safe
def weekly_review(week: str = "this_week") -> str:
    """The Monday-to-Sunday review: money in and out, change vs the week before, top categories, the 3 biggest
    payments, uncategorised entries, budgets near or over their limit, accounts that need attention, and last week's
    commitment. week: "this_week", "last_week", or any date YYYY-MM-DD inside the week you want.
    Quote every number exactly as returned."""
    return _out(core.weekly_review_text(core.weekly_review(core.week_anchor(week))))


@tool
@_safe
def save_commitment(text: str) -> str:
    """Save the user's ONE commitment for this week, in their own words (e.g. "no more than 3 delivery orders").
    Only call this when the user has told you what they want to commit to. Do not invent a commitment for them."""
    saved = core.save_commitment(text)
    return _out({"status": "saved", "week_start": saved["week_start"], "commitment": saved["commitment"]})


@tool
@_safe
def insights() -> str:
    """What Rectifi noticed in the user's own numbers, best first: data problems to fix, budgets heading over, spending
    spikes, recurring payments and their yearly cost, unusually large payments, savings-rate changes, categories without
    a budget, and small payments that add up. Use it for "how can I manage my money better" or "what should I fix".
    Fixed rules computed the findings. Quote each title and every figure exactly as returned; never add or change one."""
    from .. import insights as core_insights

    found = core_insights.insights()
    return _out({"count": len(found), "insights": core_insights.insights_text(found)} if found
                else {"count": 0, "insights": [], "note": "Nothing to flag. Insights need about a month of data."})


@tool
def list_categories() -> str:
    """All category names, grouped by kind (expense / income / transfer)."""
    groups: dict[str, list[str]] = {}
    for c in core.list_categories():
        groups.setdefault(c["kind"], []).append(c["name"])
    return _out(groups)


AGENT_TOOLS = [log_transaction, log_sms, spending_report, find_transactions, set_budget, check_budgets, recategorize,
               weekly_review, save_commitment, insights, list_categories]
TOOLS_BY_NAME = {t.name: t for t in AGENT_TOOLS}
