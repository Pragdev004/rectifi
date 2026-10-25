"""The Ask page's engine: one agent graph for the whole process, one running turn per conversation, and the transcript.

Everything that touches the model is imported inside the functions, so the rest of the API (and its tests) never load
langchain. The amount guard, masking and the original-SMS swap live inside the graph's nodes (rectifi/agent/graph.py);
nothing here can bypass them."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import uuid

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from .. import config
from .errors import ApiError
from .wire import to_wire

TICK_SECONDS = 5.0
TEXT_MAX = 4000
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_graph = None
_graph_key: str | None = None
_graph_lock = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def new_thread_id() -> str:
    return str(uuid.uuid4())


def require_thread(thread_id: str) -> str:
    if not THREAD_ID.fullmatch(thread_id or ""):
        raise ApiError(404, "not_found", "There is no conversation with that id.")
    return thread_id


def get_graph():
    """The agent graph, built on first use for the current provider, with the conversation memory in Postgres."""
    global _graph, _graph_key
    key = f"{config.llm_provider()}|{config.database_url()}"
    with _graph_lock:
        if _graph is None or _graph_key != key:
            _close_saver()
            from ..agent.graph import build_graph, postgres_checkpointer

            try:
                saver = postgres_checkpointer()
                _graph = build_graph(checkpointer=saver)
            except Exception as exc:
                _graph = None
                raise ApiError(503, "model_unavailable", f"Couldn't start the assistant: {exc}") from None
            _graph_key = key
        return _graph


def _close_saver() -> None:
    global _graph
    saver = getattr(_graph, "checkpointer", None)
    conn = getattr(saver, "conn", None)
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass
    _graph = None


def reset() -> None:
    """Forget the graph (and close its database connection). Tests call this because every test wipes the tables."""
    global _graph_key
    with _graph_lock:
        _close_saver()
        _graph_key = None
    with _thread_locks_guard:
        _thread_locks.clear()


def thread_lock(thread_id: str) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(thread_id, threading.Lock())


def status() -> dict:
    from ..agent.llm import describe, llm_notice, llm_status

    try:
        ok, why = llm_status()
        return {"ok": bool(ok), "why": why or "", "notice": llm_notice() if ok else "", "model": describe()}
    except Exception as exc:                         # a broken model setting must never break the page
        return {"ok": False, "why": str(exc), "notice": "", "model": "?"}


def require_model() -> None:
    state = status()
    if not state["ok"]:
        raise ApiError(503, "model_unavailable", state["why"] or "The assistant is off.")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(part if isinstance(part, str) else str(part.get("text", "")) for part in content or [])


def transcript(messages: list, start: int = 0) -> list[dict]:
    """The chat as the page draws it: your messages, and Rectifi's replies each with the steps it took and any figure it
    can't trace to anything real. Same replay as the Streamlit Ask page, so both show the same "Not verified" notes.
    Only items from message number `start` on are returned; the earlier messages still count as context for the checks."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from ..agent.graph import unverified_amounts

    items: list[dict] = []
    steps: list[dict] = []
    seen: list = []
    for index, m in enumerate(messages):
        seen.append(m)
        if isinstance(m, HumanMessage):
            if index >= start:
                items.append({"role": "user", "text": _text_of(m.content)})
        elif isinstance(m, ToolMessage):
            steps.append({"tool": getattr(m, "name", None) or "tool", "output": _text_of(m.content),
                          "error": getattr(m, "status", "") == "error"})
        elif isinstance(m, AIMessage) and m.content:
            if index >= start:
                items.append({"role": "assistant", "text": _text_of(m.content),
                              "unverified": unverified_amounts(seen), "steps": steps})
            steps = []
    return items


def history(thread_id: str) -> list[dict]:
    require_model()
    graph = get_graph()
    messages = graph.get_state({"configurable": {"thread_id": thread_id}}).values.get("messages", [])
    return transcript(messages)


def _line(payload: dict) -> bytes:
    return (json.dumps(to_wire(payload), ensure_ascii=False) + "\n").encode("utf-8")


def _prepare() -> object:
    require_model()
    return get_graph()


async def start_turn(thread_id: str, text: str) -> StreamingResponse:
    """Check everything that can fail with a proper status code, then stream the turn as one JSON object per line:
    {"type":"step","tool","error"}, {"type":"tick"} every 5 s, and finally {"type":"done","items"} or {"type":"error","message"}."""
    lock = thread_lock(thread_id)
    if not lock.acquire(blocking=False):
        raise ApiError(409, "conflict", "Still answering your last message.")
    try:
        graph = await run_in_threadpool(_prepare)            # may wait on the model's status and the database: not on the event loop
    except BaseException:
        lock.release()
        raise
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(item) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:                         # the server is shutting down: nobody is listening
            pass

    def worker() -> None:
        from ..agent.graph import chat_events
        from ..agent.llm import explain_error

        try:
            cfg = {"configurable": {"thread_id": thread_id}}
            for kind, payload in chat_events(graph, text, thread_id):
                if kind == "tool":
                    emit({"type": "step", "tool": getattr(payload, "name", None) or "tool",
                          "error": getattr(payload, "status", "") == "error"})
                else:
                    full = graph.get_state(cfg).values.get("messages", [])
                    emit({"type": "done", "items": transcript(full, start=len(full) - len(payload))})
        except Exception as exc:                     # the run is saved in the checkpointer as far as it got
            emit({"type": "error", "message": explain_error(exc)})
        finally:
            lock.release()
            emit(None)

    threading.Thread(target=worker, name="rectifi-ask", daemon=True).start()

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

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

