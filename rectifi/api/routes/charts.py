"""/api routes for charts: draw (streamed), resume after the code review, try again from an attempt, the gallery, pin, run again, delete.

A route validates input and calls tools / rectifi.charts.run. The model graph (rectifi.charts.graph, langchain) is imported
inside the handlers, so importing this module never loads langchain (a test checks). Pictures travel as base64 text because an
<img> tag can't send the X-Rectifi-Request header. One drawing at a time: a second one gets 409.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from ... import config, tools, userctx
from ..errors import ApiError
from ..inputs import api, ensure_db, int_field, path_int, read_json, text_field
from ..wire import chart_detail, chart_summary, to_wire

TICK_SECONDS = 5.0
REQUEST_MAX = 500
THREAD_ID = re.compile(r"chart-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PERIOD_OK = re.compile(r"[A-Za-z0-9._ -]{1,40}")

_graph = None
_graph_key: str | None = None
_graph_lock = threading.Lock()
_busy = threading.Lock()


def reset() -> None:
    """Forget the cached graph and release the busy flag. Tests call this because each test wipes the tables."""
    global _graph, _graph_key
    with _graph_lock:
        _graph, _graph_key = None, None
    if _busy.locked():
        try:
            _busy.release()
        except RuntimeError:
            pass


def _status() -> dict:
    from ...agent import llm as llm_module

    try:
        ok, why = llm_module.llm_status()
        return {"ok": bool(ok), "why": why or "", "model": llm_module.describe()}
    except Exception as exc:                      # noqa: BLE001 - a broken model setting must never break the page
        return {"ok": False, "why": llm_module.redact(exc), "model": "?"}


def _graph_for_run():
    """The chart graph for the current provider and database, on the shared Postgres checkpointer."""
    global _graph, _graph_key
    from ...agent import checkpoint, contracts
    from ...agent import llm as llm_module

    key = f"{config.llm_provider()}|{config.role_model('code')}|{config.database_url()}"
    with _graph_lock:
        if _graph is None or _graph_key != key:
            try:
                _graph = contracts.build_chart_subgraph(llm=llm_module.get_llm("code"), checkpointer=checkpoint.checkpointer())
            except Exception as exc:              # noqa: BLE001
                _graph = None
                raise ApiError(503, "model_unavailable", f"Couldn't start the chart writer: {exc}") from None
            _graph_key = key
        return _graph


def _detail(chart_id: int) -> dict:
    from ...charts import checks as checks_module
    from ...charts.run import b64

    row = tools.get_chart(chart_id, with_code=True, with_images=True)
    alt = checks_module.alt_text(None, row["checks"] or {}, title=row["title"])
    return chart_detail(row, alt, b64=b64)


def _require_thread(thread_id: str) -> str:
    if not THREAD_ID.fullmatch(thread_id or ""):
        raise ApiError(404, "not_found", "There is no chart run with that id.")
    return thread_id


def _line(payload: dict) -> bytes:
    return (json.dumps(to_wire(payload), ensure_ascii=False) + "\n").encode("utf-8")


def _validated_period(raw) -> str:
    text = text_field({"period": raw}, "period", required=False, max_len=40, default="last-3-months") or "last-3-months"
    if not PERIOD_OK.fullmatch(text):
        raise ApiError(422, "invalid", "That period isn't one I know. Try 'last-3-months', 'last-30-days', '2026-09' or 'all'.", "period")
    tools.chart_period(text)                      # RectifiError (422) when unreadable
    return text


async def _stream(graph_input, thread_id: str) -> StreamingResponse:
    """Run (or resume) the graph in a worker thread and stream one JSON object per line:
    {"type":"progress", step, verdict, detail, attempt}, {"type":"tick"}, then one of
    {"type":"interrupt", thread_id, code, attempt} (waiting for the review gate), {"type":"done", chart},
    {"type":"failed", message, trace} or {"type":"error", message}."""
    if not _busy.acquire(blocking=False):
        raise ApiError(409, "conflict", "Still drawing the last chart.")
    try:
        status = await run_in_threadpool(_status)
        if not status["ok"]:
            raise ApiError(503, "model_unavailable", status["why"] or "The model is off.")
        graph = await run_in_threadpool(_graph_for_run)
    except BaseException:
        _busy.release()
        raise
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(item) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            pass

    def worker() -> None:
        from ...agent.llm import explain_error

        cfg = {"configurable": {"thread_id": thread_id}}
        try:
            for mode, payload in graph.stream(graph_input, cfg, stream_mode=["custom"]):
                if mode == "custom":
                    emit({"type": "progress", **{k: payload.get(k) for k in ("step", "verdict", "detail", "attempt")}})
            state = graph.get_state(cfg)
            if state.next:                                        # paused at the review gate
                pending = next((i.value for t in state.tasks for i in t.interrupts), {})
                emit({"type": "interrupt", "thread_id": thread_id, "code": pending.get("code", ""), "attempt": pending.get("attempt", 1)})
            else:
                values = state.values
                if values.get("ok") and values.get("chart_id"):
                    emit({"type": "done", "thread_id": thread_id, "chart": _detail(values["chart_id"]), "trace": values.get("trace", [])})
                else:
                    emit({"type": "failed", "thread_id": thread_id, "message": values.get("error") or "I couldn't draw that.", "trace": values.get("trace", [])})
        except Exception as exc:                                   # noqa: BLE001
            emit({"type": "error", "message": explain_error(exc)})
        finally:
            _busy.release()
            emit(None)

    userctx.spawn(worker, name="rectifi-chart")        # keeps the signed-in person in the worker thread

    async def lines():
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), TICK_SECONDS)
            except asyncio.TimeoutError:
                yield _line({"type": "tick"})
                continue
            if item is None:
                return
            yield _line(item)

    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@api()
def status(request):
    return {**_status(), "plotly": False}


@api()
def gallery(request):
    from ...charts.run import b64

    pinned = request.query_params.get("pinned") in ("1", "true")
    items = []
    for row in tools.list_charts(pinned_only=pinned, limit=30):
        full = tools.get_chart(row["id"], with_code=False, with_images=True)
        items.append(chart_summary(row, thumb=b64(bytes(full["image_light"])) if full.get("image_light") else None))
    return {"items": items}


@api()
def detail(request):
    return _detail(path_int(request, "chart_id"))


async def draw(request: Request):
    data = await read_json(request)
    text = text_field(data, "request", max_len=REQUEST_MAX)
    period = _validated_period(data.get("period"))
    review = data.get("review", False)
    if not isinstance(review, bool):
        raise ApiError(422, "invalid", "Review must be true or false.", "review")
    await run_in_threadpool(ensure_db)
    from ...charts.graph import new_thread_id

    return await _stream({"request": text, "period": period, "review": review}, new_thread_id())


async def resume(request: Request):
    from langgraph.types import Command

    thread_id = _require_thread(request.path_params["thread_id"])
    data = await read_json(request)
    run = data.get("run")
    if not isinstance(run, bool):
        raise ApiError(422, "invalid", "Say whether to run the code.", "run")
    answer: dict = {"run": run}
    if data.get("code") is not None:
        answer["code"] = text_field(data, "code", max_len=8192)
    await run_in_threadpool(ensure_db)
    return await _stream(Command(resume=answer), thread_id)


async def retry(request: Request):
    thread_id = _require_thread(request.path_params["thread_id"])
    data = await read_json(request)
    attempt = int_field(data, "attempt")
    if attempt is None or not 1 <= attempt <= config.CHART_MAX_ATTEMPTS:
        raise ApiError(422, "invalid", "That attempt number doesn't exist.", "attempt")
    feedback = text_field(data, "feedback", required=False, max_len=500, default="") or ""
    await run_in_threadpool(ensure_db)
    from ...charts.graph import retry_from_attempt

    def fork():
        try:
            return retry_from_attempt(_graph_for_run(), thread_id, attempt, feedback)
        except ValueError as exc:
            raise ApiError(404, "not_found", str(exc)) from None

    await run_in_threadpool(fork)
    return await _stream(None, thread_id)


@api(body=True)
def pin(request, data):
    pinned = data.get("pinned")
    if not isinstance(pinned, bool):
        raise ApiError(422, "invalid", "Say whether to pin it.", "pinned")
    return chart_summary(tools.set_chart_pinned(path_int(request, "chart_id"), pinned))


@api()
def rerun(request):
    from ...charts import run as chart_run

    chart_id = path_int(request, "chart_id")
    chart_run.rerun_saved(chart_id)
    return _detail(chart_id)


@api()
def delete(request):
    if not tools.delete_chart(path_int(request, "chart_id")):
        raise ApiError(404, "not_found", "There is no saved chart with that id.")
    return {"deleted": True}


routes = [
    Route("/charts/status", status, methods=["GET"]),
    Route("/charts", gallery, methods=["GET"]),
    Route("/charts/draw", draw, methods=["POST"]),
    Route("/charts/threads/{thread_id}/resume", resume, methods=["POST"]),
    Route("/charts/threads/{thread_id}/retry", retry, methods=["POST"]),
    Route("/charts/{chart_id:int}", detail, methods=["GET"]),
    Route("/charts/{chart_id:int}", delete, methods=["DELETE"]),
    Route("/charts/{chart_id:int}/pin", pin, methods=["POST"]),
    Route("/charts/{chart_id:int}/rerun", rerun, methods=["POST"]),
]
