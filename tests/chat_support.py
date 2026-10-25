"""Helpers for the chat tests (not a test file): a supervisor on the real Postgres saver + store, with scripted models."""

from __future__ import annotations

import json
import uuid

from langchain_core.messages import AIMessage

from rectifi.agent.checkpoint import get_memory
from rectifi.agent.supervisor import build_supervisor, new_human
from support.chat_fakes import fake_chart, fake_rag
from support.fakes import ScriptedModel, ScriptedStructured


def tid() -> str:
    return str(uuid.uuid4())


def make(agent=None, chat=None, grade=None, rag=None, chart=None):
    """A supervisor wired to the real saver/store. `agent`, `chat`: lists of AIMessage for the scripted models."""
    memory = get_memory()
    return build_supervisor(
        checkpointer=memory.saver, store=memory.store,
        agent_llm=ScriptedModel(script=list(agent or [])) if not isinstance(agent, ScriptedModel) else agent,
        chat_llm=ScriptedModel(script=list(chat or [])) if not isinstance(chat, ScriptedModel) else chat,
        grade_llm=grade if grade is not None else ScriptedStructured(),
        rag_graph=rag if rag is not None else fake_rag(), chart_graph=chart if chart is not None else fake_chart())


def turn(graph, thread_id, text, **extra):
    """One turn with invoke(); returns the final state values."""
    payload = {"messages": [new_human(text)], "uid": "owner", "thread_id": thread_id, "forced": "", "mode": "auto", "review": False, **extra}
    return graph.invoke(payload, {"configurable": {"thread_id": thread_id}, "recursion_limit": 40})


def cfg(thread_id):
    return {"configurable": {"thread_id": thread_id}}


def last_ai(values):
    return next(m for m in reversed(values["messages"]) if isinstance(m, AIMessage) and m.content)


def ndjson(response):
    return [json.loads(x) for x in response.text.splitlines() if x.strip()]
