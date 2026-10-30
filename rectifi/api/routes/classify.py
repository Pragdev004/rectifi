"""/api routes for the classifier: how an import's rows were sorted, the model pass, and "apply to similar".

A route validates input, calls one function in tools (or imports rectifi.agent.jobs lazily inside the handler) and shapes
the answer with api/wire.py. Importing this module must not load langchain."""

from __future__ import annotations

from starlette.routing import Route

from ... import tools
from .. import wire
from ..errors import ApiError
from ..inputs import api, int_field, path_int, query

DEFAULT_ROWS = 1000


@api()
def import_rows(request):
    """GET /api/imports/{batch}/rows?show=all|misc|model|suggested"""
    batch_id = path_int(request, "batch_id")
    if tools.get_import_batch(batch_id) is None:
        raise ApiError(404, "not_found", f"There is no import #{batch_id}.")
    show = query(request, "show", "all")
    if show not in tools.SHOW_FILTERS:
        raise ApiError(422, "invalid", f"show must be one of: {', '.join(tools.SHOW_FILTERS)}.", "show")
    rows = tools.batch_rows(batch_id, show, DEFAULT_ROWS)
    job_id = tools.classify_job_for_batch(batch_id)
    job = tools.get_classify_job(job_id) if job_id else None
    return {"items": [wire.txn(t) for t in rows], "show": show,
            "summary": wire.batch_summary(tools.batch_source_counts(batch_id)),
            "job": wire.classify_job(job) if job else None}


@api()
def get_job(request):
    job = tools.get_classify_job(path_int(request, "job_id"))
    if job is None:
        raise ApiError(404, "not_found", "There is no such sorting job.")
    return {"job": wire.classify_job(job)}


@api(body=True)
def start_job(request, data):
    """POST /api/classify/jobs {"batch_id": 12} sorts the rows still in Miscellaneous after that import.
    Without batch_id it first marks every Miscellaneous entry you never chose yourself, then sorts them."""
    batch_id = int_field(data, "batch_id", required=False)
    if batch_id is not None and tools.get_import_batch(batch_id) is None:
        raise ApiError(404, "not_found", f"There is no import #{batch_id}.")
    if batch_id is None:
        tools.queue_miscellaneous_resort()
    from ...agent import jobs                                   # lazy: the model libraries load only here

    started = jobs.queue_for_batch(batch_id)
    job = tools.get_classify_job(started["job_id"]) if started["job_id"] else None
    return {"job": wire.classify_job(job) if job else None, "waiting": len(tools.pending_classification(batch_id, limit=5000))}


@api()
def apply_similar(request):
    txn_id = path_int(request, "txn_id")
    if tools.get_transaction(txn_id) is None:
        raise ApiError(404, "not_found", f"There is no transaction #{txn_id}.")
    return {"changed": tools.apply_category_to_similar(txn_id)}


routes = [
    Route("/imports/{batch_id}/rows", import_rows, methods=["GET"]),
    Route("/classify/jobs", start_job, methods=["POST"]),
    Route("/classify/jobs/{job_id}", get_job, methods=["GET"]),
    Route("/transactions/{txn_id}/apply-similar", apply_similar, methods=["POST"]),
]
