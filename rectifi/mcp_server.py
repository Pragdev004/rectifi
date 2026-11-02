"""Rectifi as an MCP server, so Claude Desktop (or any MCP client) can ask about your money.

Read-only on purpose: an outside assistant can look, but only you (through the app) can write.
The same deterministic functions back the app, the agent, and this server.

Run:  python -m rectifi.mcp_server
"""

from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import config
from . import tools as core
from .money import fmt_inr
from .privacy import mask_sensitive

server = MCPServer(
    name="rectifi",
    instructions="Read-only access to the user's Rectifi expense ledger (Indian rupees). "
                 "Quote amounts exactly as returned; never compute totals yourself.",
)


# Every tool here only reads. Telling the client so lets it skip scary confirmation prompts.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def _pct(value) -> str:
    return f"{value}%"


def _json(payload) -> str:
    """JSON for the outside assistant. Money is formatted as rupees, and long account/card/reference
    numbers are masked before anything leaves this process. Percentages must be turned into text
    with _pct() BEFORE they get here, or they would be printed as rupees."""

    def default(o):
        if isinstance(o, Decimal):
            return fmt_inr(o)
        if isinstance(o, date):
            return o.isoformat()
        raise TypeError(type(o).__name__)

    return mask_sensitive(json.dumps(payload, default=default, ensure_ascii=False, indent=1))


def _period(month: str | None) -> tuple[date, date]:
    return core.month_bounds(month or "this_month")


@server.tool(annotations=READ_ONLY)
def spending_summary(month: str = "this_month") -> str:
    """Income, spending, savings and savings rate, plus spending per category. month: 'this_month', 'last_month' or 'YYYY-MM'."""
    start, end = _period(month)
    s = core.spending_summary(start, end)
    s["savings_rate"] = _pct(s["savings_rate"]) if s["savings_rate"] is not None else None
    for row in s["by_category"]:
        row["share"] = _pct(row["share"])
    return _json(s)


@server.tool(annotations=READ_ONLY)
def budget_status(month: str = "this_month") -> str:
    """Each budgeted category's limit, spent, remaining, percent used and state (ok / near / over)."""
    status = core.budget_status(month)
    status["pace"] = _pct(status["pace"])
    for row in status["categories"]:
        row["percent"] = _pct(row["percent"])
    return _json(status)


@server.tool(annotations=READ_ONLY)
def recent_transactions(search: str | None = None, category: str | None = None, limit: int = 20) -> str:
    """Most recent transactions, optionally filtered by text (payee / narration / note) or category name."""
    rows = core.list_transactions(category=category, search=search, limit=min(limit, 100))
    keep = ("id", "txn_date", "amount", "direction", "payee", "category", "account", "source", "note")
    return _json([{k: r[k] for k in keep} for r in rows])


@server.tool(annotations=READ_ONLY)
def list_accounts() -> str:
    """The user's accounts: name, last 4 digits, how many transactions each has, and the date of the latest bank balance."""
    return _json(core.list_accounts())


@server.tool(annotations=READ_ONLY)
def weekly_review(week: str = "this_week") -> str:
    """The Monday-to-Sunday review: money in and out, change vs the week before, top categories, the 3 biggest payments,
    uncategorised entries, budgets near or over their limit, accounts needing attention, and the weekly commitment.
    week: 'this_week', 'last_week' or any date YYYY-MM-DD inside the week."""
    try:
        return _json(core.weekly_review_text(core.weekly_review(core.week_anchor(week))))
    except core.RectifiError as exc:
        return _json({"error": str(exc)})


@server.tool(annotations=READ_ONLY)
def insights() -> str:
    """What Rectifi noticed in the user's numbers, best first: data problems, budgets heading over, spikes, recurring
    payments with their yearly cost, unusually large payments, savings-rate changes, unbudgeted categories, small leaks.
    Computed by fixed rules, not a model. Quote the titles and figures exactly."""
    from . import insights as core_insights

    found = core_insights.insights()
    return _json({"count": len(found), "insights": core_insights.insights_text(found)})


@server.tool(annotations=READ_ONLY)
def reconciliation(account: str) -> str:
    """Check an account's completeness: opening balance + recorded credits − debits vs the bank's closing balance."""
    try:
        return _json(core.reconcile(account))
    except core.RectifiError as exc:
        return _json({"error": str(exc)})


MULTIUSER_REFUSAL = (
    "The Rectifi MCP server only runs in laptop mode (one person). RECTIFI_MULTIUSER=1 is on, and this server would read "
    "one account without anybody signing in. Turn RECTIFI_MULTIUSER off in .env to use it on your own laptop. "
    "On a shared server, use the web app instead."
)


class MultiUserRefused(RuntimeError):
    """Raised before the server starts when RECTIFI_MULTIUSER=1."""


def refuse_if_multiuser() -> None:
    if config.multiuser():
        raise MultiUserRefused(MULTIUSER_REFUSAL)


def main() -> int:
    """Start the stdio server. stdout is the protocol, so a refusal goes to stderr (never print here) and the exit code is 2."""
    try:
        refuse_if_multiuser()
    except MultiUserRefused as exc:
        sys.stderr.write(f"Rectifi MCP server not started: {exc}\n")
        return 2
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
