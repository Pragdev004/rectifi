"""GET /api/health and GET /api/meta: what every page needs to start (today, month and week lists, categories, accounts)."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import psycopg
from psycopg_pool import PoolTimeout
from starlette.responses import JSONResponse
from starlette.routing import Route

from ... import tools
from ...db import DatabaseUnavailable
from ..inputs import api

log = logging.getLogger("rectifi.api")


def months_back(n: int = 12) -> list[str]:
    first = tools.today().replace(day=1)
    out = []
    for _ in range(n):
        out.append(first.strftime("%Y-%m"))
        first = (first - timedelta(days=1)).replace(day=1)
    return out


def weeks_back(n: int = 12) -> list[date]:
    monday, _ = tools.week_bounds(None)
    return [monday - timedelta(days=7 * i) for i in range(n)]


async def health(request):
    return JSONResponse({"ok": True})


@api()
def meta(request):
    accounts = tools.list_accounts()
    default = tools.default_account()
    return {
        "today": tools.today(),
        "months": months_back(),
        "weeks": weeks_back(),
        "categories": tools.list_categories(),
        "accounts": [{"id": a["id"], "name": a["name"], "last4": a["last4"]} for a in accounts],
        "default_account_id": default["id"] if default else None,
    }


routes = [Route("/health", health, methods=["GET"]), Route("/meta", meta, methods=["GET"])]
