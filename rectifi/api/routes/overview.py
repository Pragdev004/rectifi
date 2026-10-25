"""GET /api/overview?month=YYYY-MM: everything the Overview page shows, in one trip."""

from __future__ import annotations

import logging

from starlette.routing import Route

from ... import insights, tools
from ..errors import DB_ERRORS
from ..inputs import api, query
from .. import wire

log = logging.getLogger("rectifi.api")


def proofs() -> list[dict]:
    """One entry per account: its balance check, or the plain reason it can't be checked yet."""
    out = []
    for a in tools.list_accounts():
        entry = {"account": {"id": a["id"], "name": a["name"], "last4": a["last4"]}, "reconcile": None, "reason": None}
        try:
            entry["reconcile"] = wire.recon(tools.reconcile(a["id"]))
        except tools.RectifiError as exc:
            entry["reason"] = str(exc)
        out.append(entry)
    return out


def safe_top_insights(limit: int = 3) -> tuple[list[dict] | None, bool]:
    """(insights, error?). A surprise in one rule must not take the page down; a database error still does."""
    try:
        return [wire.insight(i) for i in insights.top_insights(limit)], False
    except DB_ERRORS:
        raise
    except Exception:
        log.exception("insights failed")
        return None, True


@api()
def overview(request):
    month = tools.parse_month(query(request, "month"))
    start, end = tools.month_bounds(month)
    summary = tools.spending_summary(start, end)
    top, failed = safe_top_insights(3)
    return {
        "month": wire.month_text(month), "start": start, "end": end,
        "summary": wire.summary(summary),
        "budgets": wire.budget_status(tools.budget_status(month), tools.today()),
        "pace": wire.pace(tools.spending_pace(month)),
        "recent": [wire.txn(t) for t in tools.list_transactions(start, end, limit=8)],
        "proofs": proofs(),
        "top_insights": top, "insights_error": failed,
    }


routes = [Route("/overview", overview, methods=["GET"])]
