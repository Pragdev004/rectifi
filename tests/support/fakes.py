"""Fakes for tests that need a model or embeddings. No network, no key, fully deterministic.

    ScriptedModel       returns the next pre-written AIMessage on every call (the agent tests use it)
    ScriptedStructured  the same, plus with_structured_output(): returns the next scripted object, checked by the schema
    BowEmbeddings       bag-of-words vectors: texts that share words are close, so retrieval tests can predict results
"""

from __future__ import annotations

import hashlib
import re

import numpy as np
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, Field


class ScriptedModel(BaseChatModel):
    """Returns the next pre-written AIMessage on every call."""

    script: list
    seen: list = Field(default_factory=list)   # every message the model was shown, for privacy tests

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.extend(messages)
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


class ScriptedStructured(ScriptedModel):
    """A ScriptedModel that also answers `with_structured_output(Schema)` from a queue of scripted objects.

        model = ScriptedStructured(script=[AIMessage("hi")], structured=[Verdict(relevant=True), {"relevant": False}])
        grader = model.with_structured_output(Verdict)
        grader.invoke("is this relevant?")      # -> Verdict(relevant=True), then Verdict(relevant=False)

    A scripted item may be an instance of the schema, a dict (checked by the schema), or an Exception (raised, to test the
    error path). A pydantic schema rejects a bad object just as the real parser would. `schemas` records what was asked for."""

    script: list = Field(default_factory=list)
    structured: list = Field(default_factory=list)
    schemas: list = Field(default_factory=list)

    def with_structured_output(self, schema, *, include_raw: bool = False, **kwargs):
        def run(model_input):
            self.seen.extend(self._convert_input(model_input).to_messages())
            self.schemas.append(schema)
            if not self.structured:
                raise AssertionError("ScriptedStructured ran out of scripted objects.")
            item = self.structured.pop(0)
            if isinstance(item, BaseException):
                raise item
            parsed = _validate(schema, item)
            return {"raw": AIMessage(""), "parsed": parsed, "parsing_error": None} if include_raw else parsed

        return RunnableLambda(run)


def _validate(schema, item):
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return item if isinstance(item, schema) else schema.model_validate(item)
    return item          # a JSON-schema dict or a TypedDict: return what was scripted


# ───────────── bag-of-words embeddings ─────────────

BOW_DIMS = 64
_WORD = re.compile(r"[a-z0-9]+")


def bow_vector(text: str, dims: int = BOW_DIMS) -> list[float]:
    """A unit-length vector with one slot per hashed word. The same words always give the same vector; texts that share
    words have a high cosine. An empty text gives the zero vector."""
    vec = np.zeros(dims, dtype=np.float64)
    for word in _WORD.findall((text or "").lower()):
        slot = int.from_bytes(hashlib.md5(word.encode(), usedforsecurity=False).digest()[:4], "big") % dims
        vec[slot] += 1.0
    norm = float(np.linalg.norm(vec))
    return (vec / norm).tolist() if norm else vec.tolist()


class BowEmbeddings(Embeddings):
    """Deterministic fake embeddings (see bow_vector). `calls` records every text embedded."""

    def __init__(self, dims: int = BOW_DIMS) -> None:
        self.dims = dims
        self.calls: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.extend(texts)
        return [bow_vector(t, self.dims) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.calls.append(text)
        return bow_vector(text, self.dims)
