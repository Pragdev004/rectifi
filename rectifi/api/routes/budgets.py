"""Budgets: the month's limits and progress, setting or removing one limit, and carrying last month's forward."""

from __future__ import annotations

from datetime import timedelta

from starlette.routing import Route

from ... import tools
from ..inputs import api, money_field, query, text_field
from .. import wire


def status_for(month) -> dict:
    return wire.budget_status(tools.budget_status(month), tools.today())


@api()
def get_budgets(request):
    month = tools.parse_month(query(request, "month"))
    previous = (month - timedelta(days=1)).replace(day=1)
    return {
        "status": status_for(month),
        "expense_categories": [c["name"] for c in tools.list_categories("expense")],
        "previous_month": wire.month_text(previous),
    }


@api(body=True)
def put_budget(request, data):
    month = tools.parse_month(text_field(data, "month", max_len=10))
    category = text_field(data, "category", max_len=80)
    limit = money_field(data, "limit", required=False)
    if limit is None:
        tools.delete_budget(category, month)
    else:
        tools.set_budget(category, limit, month)
    return status_for(month)


@api(body=True)
def copy_budgets(request, data):
    source = tools.parse_month(text_field(data, "from", max_len=10))
    target = tools.parse_month(text_field(data, "to", max_len=10))
    return {"copied": tools.copy_budgets(source, target)}


routes = [
    Route("/budgets", get_budgets, methods=["GET"]),
    Route("/budgets", put_budget, methods=["PUT"]),
    Route("/budgets/copy", copy_budgets, methods=["POST"]),
]
