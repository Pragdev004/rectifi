"""The Ask page: is the assistant on, start a conversation, read one back, send a message (streamed)."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.routing import Route

from .. import ask
from ..inputs import api, ensure_db, read_json, text_field


@api()
def status(request):
    return ask.status()


@api()
def new_thread(request):
    return {"thread_id": ask.new_thread_id()}


@api()
def get_thread(request):
    thread_id = ask.require_thread(request.path_params["thread_id"])
    return {"items": ask.history(thread_id)}


async def send_message(request: Request):
    thread_id = ask.require_thread(request.path_params["thread_id"])
    data = await read_json(request)
    text = text_field(data, "text", max_len=ask.TEXT_MAX)
    await run_in_threadpool(ensure_db)
    return await ask.start_turn(thread_id, text)


routes = [
    Route("/ask/status", status, methods=["GET"]),
    Route("/ask/threads", new_thread, methods=["POST"]),
    Route("/ask/threads/{thread_id}", get_thread, methods=["GET"]),
    Route("/ask/threads/{thread_id}/messages", send_message, methods=["POST"]),
]
