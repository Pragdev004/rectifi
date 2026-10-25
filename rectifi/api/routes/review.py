"""GET /api/review?week=YYYY-MM-DD and PUT /api/review/commitment: the weekly review and its one promise."""

from __future__ import annotations

import logging

from starlette.routing import Route

from ... import insights, tools
from ..errors import DB_ERRORS
from ..inputs import api, date_field, query, text_field
from .. import wire

log = logging.getLogger("rectifi.api")


@api()
def review(request):
    r = tools.weekly_review(query(request, "week"))
    try:
        act = [wire.insight(i) for i in insights.act_insights()]
        failed = False
    except DB_ERRORS:
        raise
    except Exception:
        log.exception("insights failed")
        act, failed = [], True
    return {**wire.review(r), "act_insights": act, "insights_error": failed}


@api(body=True)
def save_commitment(request, data):
    week = date_field(data, "week", required=False)
    text = text_field(data, "text", max_len=tools.COMMITMENT_MAX * 4)       # tools.save_commitment gives the exact 280-character message
    return tools.save_commitment(text, week)


routes = [
    Route("/review", review, methods=["GET"]),
    Route("/review/commitment", save_commitment, methods=["PUT"]),
]
