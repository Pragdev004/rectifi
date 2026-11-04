"""The chat engine behind /api/ask (the original agent, kept as an alias) and /api/chat (the supervisor: routes, memory, tokens).

Both run through one streaming worker: one running turn per conversation, one place that turns graph events into NDJSON lines.
  legacy  /api/ask/*   the guarded agent graph on its own. Lines: step, tick, done(items of this turn), error.
  chat    /api/chat/*  the supervisor graph (agent, knowledge, chart, smalltalk, remember). Lines: token, node, step, trace,
                       interrupt, tick, done(the whole conversation), error.

Everything that touches the model is imported inside the functions, so the rest of the API (and its tests) never load
langchain. The amount guard, masking and the original-SMS swap live inside the graph's nodes (rectifi/agent/graph.py);
nothing here can bypass them."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid

from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from .. import config, userctx
from .errors import ApiError
from . import wire
from .wire import to_wire

log = logging.getLogger("rectifi.api.ask")

TICK_SECONDS = 5.0
TEXT_MAX = 4000
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_graph = None
_graph_key: str | None = None
_graph_lock = threading.Lock()
_supervisor = None
_supervisor_key: str | None = None
_turns: dict[str, "Turn"] = {}
_turns_guard = threading.Lock()
_last_prune = 0.0


class Turn:
    """What is running in one conversation: a lock (one turn at a time) and the Stop flag."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.partial = ""          # the answer text streamed so far, kept if the person presses Stop
        self.partial_id: str | None = None


def turn_for(thread_id: str) -> Turn:
    with _turns_guard:
        return _turns.setdefault(thread_id, Turn())


def new_thread_id() -> str:
    return str(uuid.uuid4())


def require_thread(thread_id: str) -> str:
    if not THREAD_ID.fullmatch(thread_id or ""):
        raise ApiError(404, "not_found", "There is no conversation with that id.")
    return thread_id


def _memory():
    """The shared saver + store pool, or the right API error."""
    from ..agent.checkpoint import get_memory

    return get_memory()          # DatabaseUnavailable is a 503 "db_unavailable" through the API's error handler


def get_graph():
    """The agent graph, built on first use for the current provider, with the conversation memory in Postgres (a pool)."""
    global _graph, _graph_key
    memory = _memory()
    key = f"{config.llm_provider()}|{config.database_url()}|{id(memory)}"
    with _graph_lock:
        if _graph is None or _graph_key != key:
            from ..agent.graph import build_graph

            try:
                _graph = build_graph(checkpointer=memory.saver)
            except Exception as exc:
                _graph = None
                raise ApiError(503, "model_unavailable", f"Couldn't start the assistant: {exc}") from None
            _graph_key = key
        return _graph


def get_supervisor():
    """The chat supervisor graph. Building it needs no model (models are looked up when a node first runs)."""
    global _supervisor, _supervisor_key
    memory = _memory()
    key = f"{config.llm_provider()}|{config.database_url()}|{id(memory)}"
    with _graph_lock:
        if _supervisor is None or _supervisor_key != key:
            from ..agent.supervisor import build_supervisor

            try:
                _supervisor = build_supervisor(checkpointer=memory.saver, store=memory.store)
            except Exception as exc:
                _supervisor = None
                raise ApiError(503, "model_unavailable", f"Couldn't start the assistant: {exc}") from None
            _supervisor_key = key
        return _supervisor


def reset() -> None:
    """Forget the graphs (and close the shared database pool). Tests call this because every test wipes the tables."""
    global _graph, _graph_key, _supervisor, _supervisor_key
    with _graph_lock:
        _graph, _graph_key, _supervisor, _supervisor_key = None, None, None, None
    with _turns_guard:
        _turns.clear()
    import sys

    module = sys.modules.get("rectifi.agent.checkpoint")
    if module is not None:
        module.close_memory()


def thread_lock(thread_id: str) -> threading.Lock:
    return turn_for(thread_id).lock


def status() -> dict:
    from ..agent.llm import describe, llm_notice, llm_status

    try:
        ok, why = llm_status()
        return {"ok": bool(ok), "why": why or "", "notice": llm_notice() if ok else "", "model": describe()}
    except Exception as exc:                         # a broken model setting must never break the page
        from ..agent.llm import redact          # lazy: rectifi.api must not load rectifi.agent at import time

        return {"ok": False, "why": redact(exc), "notice": "", "model": "?"}


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


async def stream_turn(thread_id: str, prepare, work) -> StreamingResponse:
    """Run one turn in a worker thread and stream what it emits as NDJSON, one JSON object per line.

    `prepare()` runs first (off the event loop) and may raise an ApiError; its result is passed to `work(graph, emit, turn)`.
    `work` calls emit(dict) for each line. Every 5 seconds without a line, a {"type":"tick"} keeps the connection alive.
    One turn per conversation: a second one gets 409 until the first has finished."""
    turn = turn_for(thread_id)
    if not turn.lock.acquire(blocking=False):
        raise ApiError(409, "conflict", "Still answering your last message.")
    turn.cancelled.clear()                 # BEFORE prepare: a stop that arrives while we prepare (an account delete) must survive
    turn.partial, turn.partial_id = "", None
    try:
        graph = await run_in_threadpool(prepare)            # may wait on the model's status and the database: not on the event loop
    except BaseException:
        turn.lock.release()
        raise
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(item) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:                         # the server is shutting down: nobody is listening
            pass

    def worker() -> None:
        from ..agent.llm import explain_error

        try:
            work(graph, emit, turn)
        except Exception as exc:                     # the run is saved in the checkpointer as far as it got
            emit({"type": "error", "message": explain_error(exc)})
        finally:
            turn.lock.release()
            emit(None)

    userctx.spawn(worker, name="rectifi-ask")          # keeps the signed-in person (and their key) in the worker thread

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


async def start_turn(thread_id: str, text: str) -> StreamingResponse:
    """The original Ask turn (alias of the old API): steps, then {"type":"done","items"} for this turn or {"type":"error","message"}."""

    def work(graph, emit, turn) -> None:
        from ..agent.graph import chat_events

        cfg = {"configurable": {"thread_id": thread_id}}
        for kind, payload in chat_events(graph, text, thread_id):
            if kind == "tool":
                emit({"type": "step", "tool": getattr(payload, "name", None) or "tool",
                      "error": getattr(payload, "status", "") == "error"})
            else:
                full = graph.get_state(cfg).values.get("messages", [])
                emit({"type": "done", "items": transcript(full, start=len(full) - len(payload))})

    return await stream_turn(thread_id, _prepare, work)


# ───────────────────────── the chat (supervisor) ─────────────────────────

def chat_config(thread_id: str, turn: Turn, checkpoint_id: str | None = None) -> dict:
    """Run config for the supervisor: thread, local trace spans (and LangSmith tags if opted in), and the Stop handler."""
    from ..agent.supervisor import StopHandler
    from ..agent.tracing import callbacks_for

    base = callbacks_for("chat", thread_id=thread_id, workstream="chat", role="chat")
    configurable = {"thread_id": thread_id}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return {**base, "callbacks": [*base["callbacks"], StopHandler(turn.cancelled)], "configurable": configurable, "recursion_limit": 40}


def interrupt_kind(value: dict) -> str:
    """The word the chat window switches on. Our own pauses say `kind`; the RAG graph's web-search question says type="web_search"."""
    return str(value.get("kind") or {"web_search": "web_consent"}.get(value.get("type"), "confirm"))


def _interrupts(snapshot) -> list[dict]:
    found = []
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            value = item.value if isinstance(item.value, dict) else {"value": item.value}
            found.append({"id": getattr(item, "id", None), "kind": interrupt_kind(value), "payload": value})
    return found


def chat_transcript(messages: list) -> list[dict]:
    """The conversation as the chat window draws it. Like `transcript`, plus the time, the route stamp, sources, the trace of
    how the answer was checked, and a chart reference. Stopped answers are marked."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from ..agent.graph import unverified_amounts
    from .wire import money

    items: list[dict] = []
    steps: list[dict] = []
    seen: list = []
    for m in messages:
        seen.append(m)
        kw = getattr(m, "additional_kwargs", None) or {}
        if isinstance(m, HumanMessage):
            items.append({"id": m.id, "role": "user", "text": _text_of(m.content), "ts": kw.get("ts")})
            steps = []
        elif isinstance(m, ToolMessage):
            steps.append({"tool": getattr(m, "name", None) or "tool", "output": _text_of(m.content), "error": getattr(m, "status", "") == "error"})
        elif isinstance(m, AIMessage) and m.content:
            odd = kw["unverified"] if "unverified" in kw else [money(v) for v in unverified_amounts(seen)]
            items.append({"id": m.id, "role": "assistant", "text": _text_of(m.content), "ts": kw.get("ts"), "route": kw.get("route"),
                          "unverified": odd, "steps": steps, "citations": [wire.citation(x) for x in kw.get("citations") or []], "trace": kw.get("trace") or [],
                          "checks": kw.get("turn_trace") or [], "artifact": kw.get("artifact"), "support": kw.get("support"),
                          "useful": kw.get("useful"), "abstained": bool(kw.get("abstained")), "stopped": bool(kw.get("stopped"))})
            steps = []
    return items


def _alternatives(graph, cfg: dict, messages: list) -> list[dict]:
    """Every finished answer to the newest question, oldest first (regenerate forks the thread, so earlier answers stay in its
    history). Only returned when there is more than one."""
    from langchain_core.messages import AIMessage, HumanMessage

    last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    if last_human is None:
        return []
    found: dict[str, dict] = {}
    try:
        for snap in graph.get_state_history(cfg, limit=80):
            msgs = (snap.values or {}).get("messages") or []
            if snap.next or not msgs:
                continue
            if next((m for m in reversed(msgs) if isinstance(m, HumanMessage)), None) is None or \
                    next(m for m in reversed(msgs) if isinstance(m, HumanMessage)).id != last_human.id:
                continue
            ai = next((m for m in reversed(msgs) if isinstance(m, AIMessage) and m.content), None)
            if ai is not None and ai.id not in found:
                found[ai.id] = {"id": ai.id, "text": _text_of(ai.content), "ts": (ai.additional_kwargs or {}).get("ts"),
                                "route": (ai.additional_kwargs or {}).get("route"), "created_at": snap.created_at}
    except Exception:      # noqa: BLE001 - the arrows are a bonus; the conversation itself must still open
        return []
    ordered = sorted(found.values(), key=lambda a: a["created_at"] or "")
    return ordered if len(ordered) > 1 else []


def chat_history(thread_id: str) -> dict:
    graph = get_supervisor()
    cfg = {"configurable": {"thread_id": thread_id}}
    snap = graph.get_state(cfg)
    messages = (snap.values or {}).get("messages", [])
    return {"items": chat_transcript(messages), "pending_interrupt": (_interrupts(snap) or [None])[0], "alternatives": _alternatives(graph, cfg, messages)}


def delete_chat(thread_id: str) -> bool:
    """Remove the conversation: its list row, its trace spans and every checkpoint."""
    turn = turn_for(thread_id)
    if turn.lock.locked():
        raise ApiError(409, "conflict", "Still answering. Press Stop first.")
    memory = _memory()
    memory.saver.delete_thread(thread_id)
    with _turns_guard:
        _turns.pop(thread_id, None)
    from .. import tools

    return tools.conversation_delete(thread_id)


def drain_turns(timeout: float = 15.0) -> None:
    """Stop every running chat turn and wait (up to `timeout` seconds in all) until each has let go of its lock. Used when the
    owner deletes the account, so no turn writes a checkpoint after the wipe. A turn that does not stop in time is logged (a
    count only: no id, no text); the next owner claim wipes whatever it writes later (users.confirm_signup)."""
    import time

    with _turns_guard:
        running = list(_turns.values())
    for turn in running:
        turn.cancelled.set()
    deadline = time.monotonic() + timeout
    stuck = 0
    for turn in running:
        if turn.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            turn.lock.release()
        else:
            stuck += 1
    if stuck:
        log.warning("account delete: %d chat turn(s) were still running after %.0f s", stuck, timeout)


def request_stop(thread_id: str) -> bool:
    """Ask the running turn to stop at the next model call. False if nothing is running."""
    turn = _turns.get(thread_id)
    if turn is None or not turn.lock.locked():
        return False
    turn.cancelled.set()
    return True


def _salvage(graph, cfg: dict) -> list:
    """After a stop: the agent's finished steps (calls with all their results) from inside the interrupted subgraph, so a
    payment that was saved is still in the conversation. A call whose results did not all arrive is left out."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    try:
        snap = graph.get_state(cfg)
        outer = {m.id for m in (snap.values or {}).get("messages", [])}
        for task in snap.tasks:
            # The agent's own steps are saved by the saver under the namespace "<node>:<task id>" while it runs.
            saved = graph.checkpointer.get_tuple({"configurable": {"thread_id": cfg["configurable"]["thread_id"], "checkpoint_ns": f"{task.name}:{task.id}"}})
            if saved is None:
                continue
            msgs = list((saved.checkpoint.get("channel_values") or {}).get("messages") or [])
            start = max((i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)), default=-1) + 1
            fresh = [m for m in msgs[start:] if m.id not in outer and not isinstance(m, SystemMessage)]
            kept: list = []
            i = 0
            while i < len(fresh):
                m = fresh[i]
                if isinstance(m, AIMessage) and m.tool_calls:
                    need = {c["id"] for c in m.tool_calls}
                    got = []
                    j = i + 1
                    while j < len(fresh) and isinstance(fresh[j], ToolMessage):
                        got.append(fresh[j])
                        j += 1
                    if need <= {t.tool_call_id for t in got}:
                        kept += [m, *got]
                        i = j
                        continue
                    break
                if isinstance(m, AIMessage):
                    kept.append(m)
                i += 1
            return kept
    except Exception:      # noqa: BLE001 - salvage is best effort
        return []
    return []


def _finish_stop(graph, cfg: dict, turn: Turn) -> None:
    from langchain_core.messages import AIMessage

    from ..agent.supervisor import now_iso

    kept = [m.model_copy(update={"additional_kwargs": {**(m.additional_kwargs or {}), "ts": (m.additional_kwargs or {}).get("ts") or now_iso(),
                                                         "route": "agent"}}) for m in _salvage(graph, cfg)]
    text = turn.partial.strip() or "(You stopped this answer.)"
    stopped = AIMessage(text, id=str(uuid.uuid4()), additional_kwargs={"ts": now_iso(), "stopped": True, "route": "agent"})   # no "unverified" key: chat_transcript works it out
    graph.update_state(cfg, {"messages": [*kept, stopped]}, as_node="verify_amounts")


def _translate(ns: tuple, mode: str, data, emit, turn: Turn, seen_interrupts: set) -> None:
    """One LangGraph stream item to the lines the page understands."""
    from langchain_core.messages import AIMessage, ToolMessage

    from ..agent.supervisor import FINAL_NODES

    if mode == "messages":
        chunk, meta = data
        node = (meta or {}).get("langgraph_node")
        if isinstance(chunk, AIMessage) and node in FINAL_NODES and not getattr(chunk, "tool_calls", None):
            text = _text_of(chunk.content)
            if text:
                if turn.partial_id != chunk.id:
                    turn.partial, turn.partial_id = "", chunk.id
                turn.partial += text
                emit({"type": "token", "id": chunk.id, "text": text})
    elif mode == "custom":
        if isinstance(data, dict) and "step" in data:
            emit({"type": "trace", "event": {"step": str(data.get("step")), "verdict": str(data.get("verdict", "")), "detail": str(data.get("detail", ""))}})
    elif mode == "updates":
        for node, payload in (data or {}).items():
            if node == "__interrupt__":
                for item in payload:
                    key = getattr(item, "id", None) or id(item)
                    if key in seen_interrupts:
                        continue
                    seen_interrupts.add(key)
                    value = item.value if isinstance(item.value, dict) else {"value": item.value}
                    emit({"type": "interrupt", "id": getattr(item, "id", None), "kind": interrupt_kind(value), "payload": value})
            elif node == "tools" and ns:
                for message in (payload or {}).get("messages", []):
                    if isinstance(message, ToolMessage):
                        emit({"type": "step", "tool": message.name or "tool", "error": getattr(message, "status", "") == "error"})
            elif not ns:
                emit({"type": "node", "node": node})


def _maybe_prune() -> None:
    global _last_prune
    import time

    if time.monotonic() - _last_prune < 1800:
        return
    _last_prune = time.monotonic()
    try:
        from ..agent.tracing import prune

        prune()
    except Exception:      # noqa: BLE001
        pass


def run_chat(graph, thread_id: str, payload, emit, turn: Turn, *, checkpoint_id: str | None = None, user_text: str | None = None) -> None:
    """Drive the supervisor for one turn and emit lines. `payload` is the turn's input, a Command to resume an interrupt, or
    None to re-run from `checkpoint_id` (regenerate)."""
    from ..agent.supervisor import TurnStopped
    from .. import tools

    cfg = chat_config(thread_id, turn, checkpoint_id)
    seen: set = set()
    stopped = False
    try:
        for ns, mode, data in graph.stream(payload, cfg, stream_mode=["updates", "messages", "custom"], subgraphs=True):
            _translate(ns, mode, data, emit, turn, seen)
    except TurnStopped:
        stopped = True
    except Exception as exc:      # a stop raised inside a model call can arrive wrapped
        if not turn.cancelled.is_set() or "TurnStopped" not in repr(exc) + repr(exc.__cause__):
            raise
        stopped = True
    read_cfg = {"configurable": {"thread_id": thread_id}}
    if stopped:
        _finish_stop(graph, read_cfg, turn)
    snap = graph.get_state(read_cfg)
    messages = (snap.values or {}).get("messages", [])
    items = chat_transcript(messages)
    last_assistant = next((i["text"] for i in reversed(items) if i["role"] == "assistant"), None)
    row = tools.conversation_touch(thread_id, user_text=user_text, assistant_text=last_assistant)
    _maybe_prune()
    emit({"type": "done", "stopped": stopped, "items": items, "pending_interrupt": (_interrupts(snap) or [None])[0],
          "alternatives": _alternatives(graph, read_cfg, messages), "thread": {"thread_id": row["thread_id"], "title": row["title"],
                                                                                 "preview": row["preview"], "updated_at": row["updated_at"]}})


def _chat_prepare() -> object:
    require_model()
    return get_supervisor()


async def start_chat_turn(thread_id: str, text: str, *, uid: str = "owner", route: str = "", mode: str = "auto", review: bool = False):
    from ..agent.supervisor import new_human

    def work(graph, emit, turn) -> None:
        payload = {"messages": [new_human(text)], "uid": uid, "thread_id": thread_id, "forced": route, "mode": mode, "review": review}
        run_chat(graph, thread_id, payload, emit, turn, user_text=text)

    return await stream_turn(thread_id, _chat_prepare, work)


async def start_resume(thread_id: str, value):
    from langgraph.types import Command

    def prepare():
        graph = _chat_prepare()
        if not _interrupts(graph.get_state({"configurable": {"thread_id": thread_id}})):
            raise ApiError(409, "conflict", "There is nothing waiting for an answer.")
        return graph

    def work(graph, emit, turn) -> None:
        run_chat(graph, thread_id, Command(resume=value), emit, turn)

    return await stream_turn(thread_id, prepare, work)


def fork_point(graph, thread_id: str):
    """The saved moment just after your last message arrived and before anything answered it, or None."""
    from langchain_core.messages import HumanMessage

    cfg = {"configurable": {"thread_id": thread_id}}
    messages = (graph.get_state(cfg).values or {}).get("messages", [])
    last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    if last_human is None:
        return None, None
    for snap in graph.get_state_history(cfg, limit=200):
        msgs = (snap.values or {}).get("messages") or []
        if msgs and msgs[-1].id == last_human.id and tuple(snap.next) == ("load_context",):
            return snap, last_human
    return None, last_human


async def start_regenerate(thread_id: str):
    """Answer the last question again from the saved moment before the first answer. The old answer stays in the history."""

    def prepare():
        graph = _chat_prepare()
        snap, _ = fork_point(graph, thread_id)
        if snap is None:
            raise ApiError(409, "conflict", "There is nothing to answer again.")
        return graph, snap.config["configurable"]["checkpoint_id"]

    def work(prepared, emit, turn) -> None:
        graph, checkpoint_id = prepared
        run_chat(graph, thread_id, None, emit, turn, checkpoint_id=checkpoint_id)

    return await stream_turn(thread_id, prepare, work)
