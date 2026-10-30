"""The chat window: conversations (list, search, new, rename, delete), the full history of one, and the streamed turn.

    GET    /chat/status                       is the assistant on?
    GET    /chat/threads?q=                   the sidebar (newest first; q searches titles and the text of the chats)
    POST   /chat/threads                      a new conversation
    GET    /chat/threads/{id}                 the whole conversation with times, pending question, earlier answers
    PATCH  /chat/threads/{id}                 rename
    DELETE /chat/threads/{id}                 delete the conversation and its saved steps
    POST   /chat/threads/{id}/messages        {text, route?, mode?, review?} -> NDJSON stream
    POST   /chat/threads/{id}/stop            stop at the next model call
    POST   /chat/threads/{id}/regenerate      answer the last question again (forks the thread)
    POST   /chat/threads/{id}/resume          {value} answers a "Save this to memory?" style question -> NDJSON stream

The amount guard, masking and the "not verified" check all live in the graphs; this file only moves data.
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.routing import Route

from ... import tools
from .. import ask, wire
from ..errors import ApiError
from ..inputs import api, ensure_db, query, read_json, text_field

ROUTE_CHOICES = ("", "agent", "knowledge", "chart")
MODE_CHOICES = ("auto", "plain", "crag", "self_rag", "full")


def uid_of(request: Request) -> str:
    """Whose memory: the signed-in person if the auth layer set one, else the one owner of this laptop."""
    value = getattr(request.state, "uid", None)
    return str(value)[:200] if value else "owner"


def _known(thread_id: str) -> dict:
    row = tools.conversation_get(ask.require_thread(thread_id))
    if row is None:
        raise ApiError(404, "not_found", "There is no conversation with that id.")
    return row


@api()
def status(request):
    return ask.status()


@api()
def list_threads(request):
    q = query(request, "q")
    if q and len(q) > 100:
        raise ApiError(422, "invalid", "That search is too long.", "q")
    return {"threads": [wire.conversation(r) for r in tools.conversation_list(q)]}


@api(body=True, status=201)
def create_thread(request, data):
    title = text_field(data, "title", required=False, max_len=tools.CONV_TITLE_MAX)
    return wire.conversation(tools.conversation_create(ask.new_thread_id(), title))


@api()
def get_thread(request):
    row = _known(request.path_params["thread_id"])
    return {"thread": wire.conversation(row), **ask.chat_history(row["thread_id"])}


@api(body=True)
def rename_thread(request, data):
    thread_id = ask.require_thread(request.path_params["thread_id"])
    return wire.conversation(tools.conversation_rename(thread_id, text_field(data, "title", max_len=tools.CONV_TITLE_MAX)))


@api()
def delete_thread(request):
    thread_id = _known(request.path_params["thread_id"])["thread_id"]
    ask.delete_chat(thread_id)
    return {"deleted": True}


async def send_message(request: Request):
    thread_id = ask.require_thread(request.path_params["thread_id"])
    data = await read_json(request)
    text = text_field(data, "text", max_len=ask.TEXT_MAX)
    route = data.get("route") or ""
    mode = data.get("mode") or "auto"
    if route not in ROUTE_CHOICES:
        raise ApiError(422, "invalid", "Route must be agent, knowledge or chart.", "route")
    if mode not in MODE_CHOICES:
        raise ApiError(422, "invalid", "Answer style must be auto, plain, crag, self_rag or full.", "mode")
    await run_in_threadpool(ensure_db)
    await run_in_threadpool(tools.conversation_create, thread_id)
    return await ask.start_chat_turn(thread_id, text, uid=uid_of(request), route=route, mode=mode, review=bool(data.get("review")))


@api()
def stop(request):
    thread_id = ask.require_thread(request.path_params["thread_id"])
    return {"stopping": ask.request_stop(thread_id)}


async def regenerate(request: Request):
    thread_id = ask.require_thread(request.path_params["thread_id"])
    await read_json(request)
    await run_in_threadpool(ensure_db)
    return await ask.start_regenerate(thread_id)


async def resume(request: Request):
    thread_id = ask.require_thread(request.path_params["thread_id"])
    data = await read_json(request)
    if "value" not in data:
        raise ApiError(422, "invalid", "Value is needed.", "value")
    await run_in_threadpool(ensure_db)
    return await ask.start_resume(thread_id, data["value"])


routes = [
    Route("/chat/status", status, methods=["GET"]),
    Route("/chat/threads", list_threads, methods=["GET"]),
    Route("/chat/threads", create_thread, methods=["POST"]),
    Route("/chat/threads/{thread_id}", get_thread, methods=["GET"]),
    Route("/chat/threads/{thread_id}", rename_thread, methods=["PATCH"]),
    Route("/chat/threads/{thread_id}", delete_thread, methods=["DELETE"]),
    Route("/chat/threads/{thread_id}/messages", send_message, methods=["POST"]),
    Route("/chat/threads/{thread_id}/stop", stop, methods=["POST"]),
    Route("/chat/threads/{thread_id}/regenerate", regenerate, methods=["POST"]),
    Route("/chat/threads/{thread_id}/resume", resume, methods=["POST"]),
]
