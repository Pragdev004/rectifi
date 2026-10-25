"""Memory for the chat, both kinds (CampusX LangGraph lectures 23 to 25).

SHORT-TERM memory is the messages of one thread. They live in the Postgres checkpointer, in full, forever. What changes is
only what the model is SHOWN each turn: `plan_window` keeps the last few turns, trims to a token budget, and (after many
turns) asks a small model for a short summary of the older ones. The checkpoint is never shortened.

LONG-TERM memory is a LangGraph Store (Postgres) with the namespace ("users", uid, "memories"). An item is
{kind: preference|fact|correction, text, created_at, updated_at, thread_id}. Things go in only when the person says
"remember ..." (and confirms) or types them on the Memory page.

Rule 1 still holds: the model never decides a number. So memory text is masked on the way in, every rupee figure in it is
replaced by "[amount: check your records]" before it reaches a prompt (`redact_amounts`), and nothing here is ever counted
as a source for `graph.unverified_amounts` (that function only trusts what you typed and what a tool returned).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, trim_messages
from langchain_core.tools import InjectedToolArg, StructuredTool

from .. import config
from ..parsing import AMOUNT
from ..privacy import mask_sensitive

KINDS = ("preference", "fact", "correction")
TEXT_MAX = 500
MAX_ITEMS = 200
AMOUNT_PLACEHOLDER = "[amount: check your records]"

SUMMARY_AFTER_TURNS = 12      # more user turns than this: the older ones are summarised for the model
KEEP_TURNS = 6                # ...and the newest this-many turns are always sent word for word
CONTEXT_SHARE = 0.4           # the history sent to the model may use this share of the context window
CHARS_PER_TOKEN = 4


# ───────────────────────── long-term memory (the Store) ─────────────────────────

def namespace(uid: str) -> tuple[str, str, str]:
    return ("users", uid, "memories")


_BARE_RUPEES = re.compile(r"(?i)\b\d[\d,]*(?:\.\d{1,2})?\s*(?:rupees|rs\.?|inr|/-)(?!\w)")


def redact_amounts(text: str) -> str:
    """Replace every rupee figure with a placeholder. Memory may say "I get a stipend", never how much."""
    text = AMOUNT.sub(AMOUNT_PLACEHOLDER, text or "")
    return _BARE_RUPEES.sub(AMOUNT_PLACEHOLDER, text)


def clean_text(text: str | None) -> str:
    """One line of plain text, account numbers masked. Raises ValueError with a sentence that is safe to show."""
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        raise ValueError("Write what Rectifi should remember.")
    if len(value) > TEXT_MAX:
        raise ValueError(f"That is too long to remember (the limit is {TEXT_MAX} characters).")
    return mask_sensitive(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_dict(item) -> dict:
    value = dict(item.value)
    return {"id": item.key, "kind": value.get("kind", "fact"), "text": value.get("text", ""), "created_at": value.get("created_at"),
            "updated_at": value.get("updated_at") or value.get("created_at"), "thread_id": value.get("thread_id")}


def list_memories(store, uid: str) -> list[dict]:
    """Newest first."""
    items = [_as_dict(i) for i in store.search(namespace(uid), limit=MAX_ITEMS)]
    return sorted(items, key=lambda m: m["created_at"] or "", reverse=True)


def add_memory(store, uid: str, kind: str, text: str, thread_id: str | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError("A memory is a preference, a fact or a correction.")
    value = {"kind": kind, "text": clean_text(text), "created_at": _now(), "thread_id": thread_id}
    value["updated_at"] = value["created_at"]
    if len(store.search(namespace(uid), limit=MAX_ITEMS)) >= MAX_ITEMS:
        raise ValueError(f"Rectifi can remember {MAX_ITEMS} things. Delete one first.")
    key = uuid.uuid4().hex
    store.put(namespace(uid), key, value)
    return _as_dict(store.get(namespace(uid), key))


def update_memory(store, uid: str, key: str, *, text: str | None = None, kind: str | None = None) -> dict | None:
    item = store.get(namespace(uid), key)
    if item is None:
        return None
    value = dict(item.value)
    if text is not None:
        value["text"] = clean_text(text)
    if kind is not None:
        if kind not in KINDS:
            raise ValueError("A memory is a preference, a fact or a correction.")
        value["kind"] = kind
    value["updated_at"] = _now()
    store.put(namespace(uid), key, value)
    return _as_dict(store.get(namespace(uid), key))


def delete_memory(store, uid: str, key: str) -> bool:
    if store.get(namespace(uid), key) is None:
        return False
    store.delete(namespace(uid), key)
    return True


def forget_all(store, uid: str) -> int:
    count = 0
    while True:                                  # search returns a page at a time
        items = store.search(namespace(uid), limit=100)
        if not items:
            return count
        for i in items:
            store.delete(namespace(uid), i.key)
            count += 1


_REMEMBER = re.compile(r"(?is)^\s*(?:please\s+)?(?:remember|note|keep in mind)\b[\s,:-]*(?:that\s+)?(?P<fact>.+?)\s*$")
_PREFERENCE = re.compile(r"(?i)\b(prefer|always|never|i like|i don't like|i do not like|call me|use (?:short|plain|simple)|keep (?:it|replies|answers))\b")
_CORRECTION = re.compile(r"(?i)\b(not|wrong|actually|instead|correction|rather than)\b")


def detect_remember(text: str) -> tuple[str, str] | None:
    """"remember that I prefer short answers" -> ("preference", "I prefer short answers"). None if it is not a remember request."""
    m = _REMEMBER.match(text or "")
    if not m or len(m["fact"].split()) < 2:
        return None
    fact = m["fact"]
    kind = "preference" if _PREFERENCE.search(fact) else "correction" if _CORRECTION.search(fact) else "fact"
    return kind, fact


def _remember_fact(kind: str, text: str, uid: Annotated[str, InjectedToolArg] = "owner", store: Annotated[Any, InjectedToolArg] = None,
                   thread_id: Annotated[str | None, InjectedToolArg] = None) -> str:
    """Save one thing the person asked Rectifi to remember. Only call this after they confirmed."""
    saved = add_memory(store, uid, kind, text, thread_id)
    return f"Remembered: {saved['text']}"


remember_fact = StructuredTool.from_function(_remember_fact, name="remember_fact")   # uid, store and thread_id are injected, never the model's


# ───────────────────────── what goes into a prompt ─────────────────────────

def memory_lines(items: list[dict]) -> list[str]:
    return [f"- ({m['kind']}) {redact_amounts(m['text'])}" for m in items]


def context_text(*, memories: list[str] | None = None, rules: list[str] | None = None, summary: str = "") -> str:
    """The extra system note for one turn, or "" when there is nothing to add. Memory is background, never a source of numbers."""
    parts = []
    if memories:
        parts.append("What the person asked you to remember (background only; it never supplies an amount):\n" + "\n".join(memories))
    if rules:
        parts.append("Category rules the person taught Rectifi:\n" + "\n".join(rules))
    if summary:
        parts.append("Summary of the earlier part of this conversation (no amounts; look them up with a tool):\n" + summary)
    return "\n\n".join(parts)


# ───────────────────────── short-term memory: trim and summarise ─────────────────────────

def _text_of(m: BaseMessage) -> str:
    c = m.content
    return c if isinstance(c, str) else " ".join(p if isinstance(p, str) else str(p.get("text", "")) for p in c or [])


def approx_tokens(messages) -> int:
    """About 4 characters per token. Good enough to stay inside the window; the model's real counter is not needed."""
    if isinstance(messages, BaseMessage):
        messages = [messages]
    total = 0
    for m in messages:
        total += len(_text_of(m)) + len(str(getattr(m, "tool_calls", "") or "")) + 16
    return total // CHARS_PER_TOKEN + 1


def token_budget() -> int:
    return int(config.ollama_num_ctx() * CONTEXT_SHARE)


def trim_for_context(messages: list[BaseMessage], max_tokens: int | None = None) -> list[BaseMessage]:
    """The newest messages that fit the budget, starting on a user message so a tool result never loses its call. The newest
    user message is always kept, even if it alone is over the budget."""
    budget = max_tokens or token_budget()
    kept = trim_messages(messages, max_tokens=budget, token_counter=approx_tokens, strategy="last", start_on="human", include_system=True)
    if kept:
        return list(kept)
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=0)
    return list(messages[last_human:])


def _human_positions(messages: list) -> list[int]:
    return [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]


def summary_cut(messages: list) -> int:
    """Index where the 'recent' part starts when the chat is long enough to summarise, else 0."""
    humans = _human_positions(messages)
    return humans[-KEEP_TURNS] if len(humans) > SUMMARY_AFTER_TURNS else 0


SUMMARY_PROMPT = (
    "Summarise the earlier part of a conversation between a person and Rectifi, their expense assistant, in at most 6 short "
    "lines. Keep what they asked for, decisions, and names of categories or merchants. Do NOT write any rupee amount or any "
    "number of rupees: say 'an amount' instead. Reply with the summary only.")


def summarise(llm, older: list[BaseMessage], previous: str = "") -> str:
    """One model call. Account numbers are masked first; rupee figures are stripped from what comes back."""
    lines = []
    for m in older:
        if isinstance(m, (HumanMessage, AIMessage)) and _text_of(m).strip():
            who = "Person" if isinstance(m, HumanMessage) else "Rectifi"
            lines.append(f"{who}: {mask_sensitive(_text_of(m))[:600]}")
        elif isinstance(m, ToolMessage):
            lines.append(f"(Rectifi used the tool {getattr(m, 'name', 'tool')})")
    body = ("Earlier summary:\n" + previous + "\n\n" if previous else "") + "New messages:\n" + "\n".join(lines)
    reply = llm.invoke([SystemMessage(SUMMARY_PROMPT), HumanMessage(body[-6000:])])
    return redact_amounts(re.sub(r"\s+", " ", _text_of(reply)).strip())[:1200]


def plan_window(messages: list[BaseMessage], *, summary: str, summarised_to: int, llm=None) -> dict:
    """Decide what the model sees this turn. Returns {"window": n, "summary": str, "summarised_to": k}: the model gets the
    newest `n` messages plus `summary`. Nothing is deleted from the thread. If the summary call fails the old summary stays."""
    cut = summary_cut(messages)
    if cut and llm is not None and cut > summarised_to:
        try:
            summary = summarise(llm, messages[summarised_to:cut], summary)
            summarised_to = cut
        except Exception:                    # noqa: BLE001 - a failed summary must never fail the chat; trim instead
            cut = summarised_to
    start = max(cut if summary else 0, summarised_to if summary else 0)
    recent = messages[start:]
    kept = trim_for_context(recent) if recent else recent
    return {"window": len(kept), "summary": summary, "summarised_to": summarised_to}
