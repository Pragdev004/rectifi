"""Test helpers for the RAG graph (not a test file): a rule-driven fake model that does not depend on call order.

The grading step runs one task per passage in parallel, so a model that pops answers from a list would give each passage a random
one. RuleModel answers from the TEXT it is shown instead, and records every call so tests can prove what ran and what did not."""

from __future__ import annotations

import threading

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field


def schema_name(schema) -> str:
    if isinstance(schema, dict):
        return schema.get("title", "")
    return getattr(schema, "__name__", str(schema))


class RuleModel(BaseChatModel):
    """structured: {schema name: fn(prompt_text) -> object | dict}. write: fn(prompt_text) -> reply text, or a list of AIMessages popped in order.
    `log` holds ("structured", name, prompt) / ("write", prompt) entries."""

    structured: dict = Field(default_factory=dict)
    write: object = None
    messages_script: list = Field(default_factory=list)      # AIMessages for tool-calling runs, popped in order
    log: list = Field(default_factory=list)
    bound: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "rule"

    def _record(self, entry) -> None:
        with _LOCK:
            self.log.append(entry)

    def calls(self, kind: str, name: str | None = None) -> list:
        return [e for e in self.log if e[0] == kind and (name is None or e[1] == name)]

    def bind_tools(self, tools, **kwargs):
        self.bound = [getattr(t, "name", str(t)) for t in tools]
        return self

    def with_structured_output(self, schema, *, include_raw: bool = False, **kwargs):
        name = schema_name(schema)

        def run(model_input):
            text = self._convert_input(model_input).to_string()
            self._record(("structured", name, text))
            fn = self.structured.get(name)
            if fn is None:
                raise AssertionError(f"no scripted grader for {name}")
            item = fn(text)
            if isinstance(item, BaseException):
                raise item
            if isinstance(schema, type) and hasattr(schema, "model_validate") and not isinstance(item, schema):
                item = schema.model_validate(item)
            return item

        return RunnableLambda(run)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        text = "\n".join(str(m.content) for m in messages)
        self._record(("write", text))
        if self.messages_script:
            with _LOCK:
                msg = self.messages_script.pop(0)
        else:
            reply = self.write(text) if callable(self.write) else self.write
            msg = reply if isinstance(reply, AIMessage) else AIMessage(str(reply))
        return ChatResult(generations=[ChatGeneration(message=msg)])


_LOCK = threading.Lock()
