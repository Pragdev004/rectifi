"""Helpers for the chat tests (not a test file): a supervisor on the real Postgres saver + store, with scripted models."""

from __future__ import annotations

import json
import uuid

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage

from rectifi.agent.checkpoint import get_memory
from rectifi.agent.supervisor import build_supervisor, new_human
from support.chat_fakes import fake_chart, fake_rag
from support.fakes import ScriptedModel, ScriptedStructured


def tid() -> str:
    return str(uuid.uuid4())


def make(agent=None, chat=None, grade=None, rag=None, chart=None, real=False):
    """A supervisor wired to the real saver/store. `agent`, `chat`: lists of AIMessage for the scripted models.
    real=True hands in no RAG / chart graph at all, so the supervisor builds the REAL ones itself through `contracts`
    (the caller patches agent.llm.get_llm / get_embeddings); `rag` / `chart` may still be a real graph built by the test."""
    memory = get_memory()
    if real:
        rag_graph, chart_graph = rag, chart
    else:
        rag_graph, chart_graph = (rag if rag is not None else fake_rag()), (chart if chart is not None else fake_chart())
    return build_supervisor(
        checkpointer=memory.saver, store=memory.store,
        agent_llm=agent if isinstance(agent, BaseChatModel) else ScriptedModel(script=list(agent or [])),
        chat_llm=chat if isinstance(chat, BaseChatModel) else ScriptedModel(script=list(chat or [])),
        grade_llm=grade if grade is not None else ScriptedStructured(),
        rag_graph=rag_graph, chart_graph=chart_graph)


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
