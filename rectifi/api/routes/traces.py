"""The Traces page: recent model runs and the steps inside one, all from the local `trace_spans` table.

    GET    /traces?limit=&status=&entry=   {runs: [...], langsmith: {...}}
    GET    /traces/{root_run_id}           {run, spans: [...]}  spans in tree order, each with its depth
    DELETE /traces                         clear them all

Previews were masked and cut to 2 KB when they were written. Nothing here leaves the laptop; LangSmith is separate and opt-in."""

from __future__ import annotations

import os
import re

from starlette.routing import Route

from ... import config, tools
from ..errors import ApiError
from ..inputs import api, query

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def langsmith() -> dict:
    optin = config.langsmith_optin()
    return {"opted_in": optin, "sending": optin and bool(os.getenv("LANGSMITH_API_KEY", "").strip()),
            "note": "Runs are sent to LangSmith." if optin and os.getenv("LANGSMITH_API_KEY") else "Traces stay on this laptop."}


@api()
def runs(request):
    limit = query(request, "limit", "50")
    if not limit.isdigit() or int(limit) < 1:
        raise ApiError(422, "invalid", "The limit must be a whole number.", "limit")
    status = query(request, "status")
    if status not in (None, "ok", "error", "running"):
        raise ApiError(422, "invalid", "Status must be ok, error or running.", "status")
    entry = query(request, "entry")
    if entry and len(entry) > 40:
        raise ApiError(422, "invalid", "That entry name is too long.", "entry")
    return {"runs": tools.trace_runs(int(limit), status, entry), "langsmith": langsmith()}


@api()
def one(request):
    run_id = str(request.path_params["run_id"])
    if not UUID.fullmatch(run_id):
        raise ApiError(404, "not_found", "There is no run with that id.")
    found = tools.trace_tree(run_id)
    if found is None:
        raise ApiError(404, "not_found", "There is no run with that id (it may have been cleared).")
    return found


@api()
def clear(request):
    return {"deleted": tools.trace_clear()}


routes = [
    Route("/traces", runs, methods=["GET"]),
    Route("/traces", clear, methods=["DELETE"]),
    Route("/traces/{run_id}", one, methods=["GET"]),
]
