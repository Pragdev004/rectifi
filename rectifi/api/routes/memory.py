"""The Memory page: what Rectifi remembers (long-term memory in the LangGraph store), and the category rules you taught it.

    GET    /memory               {memories: [...], rules: [...]}
    POST   /memory               {kind, text}  add one by hand
    PATCH  /memory/{id}          {text?, kind?}
    DELETE /memory/{id}
    DELETE /memory               forget everything (the rules you taught are separate)
    DELETE /memory/rules/{id}    forget one taught category rule

A memory never supplies an amount: prompts get its text with every rupee figure replaced (agent/memory.py)."""

from __future__ import annotations

import re

from starlette.routing import Route

from ... import tools
from .. import wire
from ..errors import ApiError
from ..inputs import api, path_int, text_field
from .chat import uid_of

KEY = re.compile(r"[0-9a-f]{32}")


def _store():
    from ...agent.checkpoint import get_memory

    return get_memory().store


def _key(request) -> str:
    key = str(request.path_params.get("memory_id", ""))
    if not KEY.fullmatch(key):
        raise ApiError(404, "not_found", "There is no memory with that id.")
    return key


def _kind(data: dict, required: bool = True):
    value = data.get("kind")
    if value is None and not required:
        return None
    if value not in ("preference", "fact", "correction"):
        raise ApiError(422, "invalid", "A memory is a preference, a fact or a correction.", "kind")
    return value


@api()
def overview(request):
    from ...agent import memory

    return {"memories": [wire.memory_item(m) for m in memory.list_memories(_store(), uid_of(request))],
            "rules": [wire.learned_rule(r) for r in tools.memory_learned_rules()]}


@api(body=True, status=201)
def add(request, data):
    from ...agent import memory

    text = text_field(data, "text", max_len=memory.TEXT_MAX)
    try:
        return wire.memory_item(memory.add_memory(_store(), uid_of(request), _kind(data), text))
    except ValueError as exc:
        raise ApiError(422, "invalid", str(exc), "text") from None


@api(body=True)
def edit(request, data):
    from ...agent import memory

    text = text_field(data, "text", required=False, max_len=memory.TEXT_MAX)
    try:
        item = memory.update_memory(_store(), uid_of(request), _key(request), text=text, kind=_kind(data, required=False))
    except ValueError as exc:
        raise ApiError(422, "invalid", str(exc), "text") from None
    if item is None:
        raise ApiError(404, "not_found", "There is no memory with that id.")
    return wire.memory_item(item)


@api()
def delete(request):
    from ...agent import memory

    if not memory.delete_memory(_store(), uid_of(request), _key(request)):
        raise ApiError(404, "not_found", "There is no memory with that id.")
    return {"deleted": True}


@api()
def forget_everything(request):
    from ...agent import memory

    return {"deleted": memory.forget_all(_store(), uid_of(request))}


@api()
def remove_rule(request):
    if not tools.memory_remove_learned_rule(path_int(request, "rule_id")):
        raise ApiError(404, "not_found", "There is no taught rule with that id.")
    return {"deleted": True}


routes = [
    Route("/memory", overview, methods=["GET"]),
    Route("/memory", add, methods=["POST"]),
    Route("/memory", forget_everything, methods=["DELETE"]),
    Route("/memory/rules/{rule_id}", remove_rule, methods=["DELETE"]),
    Route("/memory/{memory_id}", edit, methods=["PATCH"]),
    Route("/memory/{memory_id}", delete, methods=["DELETE"]),
]
