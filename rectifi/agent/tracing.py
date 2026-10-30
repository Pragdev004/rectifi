"""Local traces: every step of a model run (chain, model call, tool, retriever) is written to `trace_spans`, so the Traces
page can answer "which step was slow, what did the grader say, why did it fail" with nothing leaving this laptop.

Privacy: previews are masked with `privacy.mask_sensitive` and cut to 2 KB before they are stored. A failure to write a
span never breaks the run. LangSmith is separate and opt-in (`config.enforce_langsmith_privacy`).

    cfg = callbacks_for("ask", thread_id=tid, workstream="chat", role="chat")
    graph.invoke(inputs, {**cfg, "configurable": {"thread_id": tid}})
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from .. import config
from ..db import connect
from ..privacy import mask_sensitive

log = logging.getLogger("rectifi.tracing")

_PRE_MASK_CHARS = 20_000       # cut long text before masking (regex cost); the stored preview is cut again after masking


def preview(value: Any) -> str | None:
    """A masked, truncated text preview of anything a step received or returned."""
    if value is None:
        return None
    if isinstance(value, dict) and isinstance(value.get("messages"), list) and value["messages"]:
        text = getattr(value["messages"][-1], "content", None)     # a graph turn: the list shows what was asked, not the dict
        if isinstance(text, str) and text.strip():
            value = text
    if not isinstance(value, str):
        try:
            value = json.dumps(value, default=_describe, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
    return mask_sensitive(value[:_PRE_MASK_CHARS])[:config.TRACE_PREVIEW_CHARS]


def _describe(obj: Any) -> Any:
    content = getattr(obj, "content", None)           # a LangChain message: show its role and text
    if content is not None:
        return f"{type(obj).__name__}: {content}"
    return str(obj)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tokens(response: Any) -> tuple[int | None, int | None]:
    try:
        message = response.generations[0][0].message
        usage = getattr(message, "usage_metadata", None) or {}
        if usage:
            return usage.get("input_tokens"), usage.get("output_tokens")
    except (AttributeError, IndexError, TypeError):
        pass
    usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
    return usage.get("prompt_tokens"), usage.get("completion_tokens")


class LocalTraceHandler(BaseCallbackHandler):
    """Writes one row to trace_spans when each step ends (or fails)."""

    raise_error = False            # a tracing problem must never stop a chat turn

    def __init__(self, thread_id: str | None = None) -> None:
        self.thread_id = thread_id
        self._open: dict[UUID, dict] = {}       # run_id -> what we knew at the start
        self._root: dict[UUID, UUID] = {}

    # ── bookkeeping ──
    def _start(self, run_id: UUID, parent: UUID | None, name: str, run_type: str, inputs: Any, model: str | None,
               metadata: dict | None) -> None:
        root = self._root.get(parent, parent) if parent else run_id
        self._root[run_id] = root
        thread = self.thread_id or (metadata or {}).get("thread_id")
        self._open[run_id] = {"parent": parent, "root": root, "name": name or run_type, "type": run_type, "started": _now(),
                              "input": preview(inputs), "model": model or (metadata or {}).get("ls_model_name"),
                              "thread": str(thread) if thread else None}

    def _end(self, run_id: UUID, output: Any = None, error: BaseException | None = None, tokens: tuple = (None, None)) -> None:
        info = self._open.pop(run_id, None)
        if info is None:
            return
        if info["root"] == run_id:
            self._root = {k: v for k, v in self._root.items() if v != run_id}
        try:
            with connect() as conn:
                conn.execute(
                    """INSERT INTO trace_spans (run_id, parent_run_id, root_run_id, thread_id, name, run_type, started_at,
                                                ended_at, status, model, input_tokens, output_tokens, input_preview,
                                                output_preview, error)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (run_id, info["parent"], info["root"], info["thread"], info["name"][:200], info["type"], info["started"], _now(),
                     "error" if error else "ok", info["model"], tokens[0], tokens[1], info["input"],
                     None if error else preview(output),
                     preview(f"{type(error).__name__}: {error}") if error else None),
                )
        except Exception as exc:      # noqa: BLE001 - never break the run; log the type only (no text, no keys)
            log.warning("Could not write a trace span (%s).", type(exc).__name__)

    @staticmethod
    def _name(serialized: dict | None, kwargs: dict, fallback: str) -> str:
        if kwargs.get("name"):
            return kwargs["name"]
        if serialized:
            return serialized.get("name") or (serialized.get("id") or [fallback])[-1]
        return fallback

    # ── chains (graphs and graph nodes) ──
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        self._start(run_id, parent_run_id, self._name(serialized, kwargs, "chain"), "chain", inputs, None, metadata)

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._end(run_id, outputs)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=error)

    # ── models ──
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        model = (kwargs.get("invocation_params") or {}).get("model") or (kwargs.get("invocation_params") or {}).get("model_name")
        self._start(run_id, parent_run_id, self._name(serialized, kwargs, "chat_model"), "llm", messages, model, metadata)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        model = (kwargs.get("invocation_params") or {}).get("model")
        self._start(run_id, parent_run_id, self._name(serialized, kwargs, "llm"), "llm", prompts, model, metadata)

    def on_llm_end(self, response, *, run_id, **kwargs):
        try:
            text = response.generations[0][0].text
        except (AttributeError, IndexError):
            text = None
        self._end(run_id, text, tokens=_tokens(response))

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=error)

    # ── tools ──
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        self._start(run_id, parent_run_id, self._name(serialized, kwargs, "tool"), "tool", kwargs.get("inputs") or input_str, None, metadata)

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._end(run_id, getattr(output, "content", output))

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=error)

    # ── retrievers ──
    def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        self._start(run_id, parent_run_id, self._name(serialized, kwargs, "retriever"), "retriever", query, None, metadata)

    def on_retriever_end(self, documents, *, run_id, **kwargs):
        self._end(run_id, [getattr(d, "page_content", str(d))[:200] for d in documents])

    def on_retriever_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=error)


def callbacks_for(run_name: str, **meta: Any) -> dict:
    """A RunnableConfig fragment for one graph run: the local trace handler, a run name, tags and metadata (LangSmith uses
    the tags and metadata only if the person opted in). Merge it into the config you pass to invoke/stream:
        cfg = {**callbacks_for("ask", thread_id=tid), "configurable": {"thread_id": tid}}"""
    config.enforce_langsmith_privacy()
    thread_id = meta.get("thread_id")
    tags = [str(meta[k]) for k in ("workstream", "role") if meta.get(k)]
    return {
        "callbacks": [LocalTraceHandler(thread_id=str(thread_id) if thread_id else None)],
        "run_name": run_name,
        "tags": tags,
        "metadata": {k: v for k, v in meta.items() if v is not None},
    }


def prune(retention_days: int | None = None, max_rows: int | None = None) -> int:
    """Delete spans older than the retention, then the oldest beyond the row cap. Returns how many rows went."""
    days = retention_days or config.TRACE_RETENTION_DAYS
    cap = max_rows or config.TRACE_MAX_ROWS
    with connect() as conn:
        old = conn.execute("DELETE FROM trace_spans WHERE started_at < now() - make_interval(days => %s)", (days,)).rowcount
        extra = conn.execute(
            "DELETE FROM trace_spans WHERE id IN (SELECT id FROM trace_spans ORDER BY started_at DESC, id DESC OFFSET %s)", (cap,)
        ).rowcount
    return old + extra
