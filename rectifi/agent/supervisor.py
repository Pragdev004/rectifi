"""The chat supervisor: one graph that decides what kind of message this is and hands it to the right worker.

    START → load_context ─┬─► fetch_memories ──┐                      (parallel: both read, neither writes the same key)
                          └─► trim_or_summarize ┴─► route ─┬─► agent       the existing guarded ReAct graph, as a subgraph
                                                           ├─► knowledge   CRAG + Self-RAG graph (WS-RAG), as a subgraph
                                                           ├─► chart       the chart harness (WS-CHARTS), as a subgraph
                                                           ├─► smalltalk   one model call, no tools
                                                           └─► remember    asks "Save this to memory?" (interrupt), then saves
                                       every worker ──► verify_amounts ──► END

Workflow patterns (CampusX LangGraph 6 to 9, 21, 22): sequential (load_context → route), parallel (fetch_memories ∥
trim_or_summarize, joined before route), conditional (route), iterative (the agent ↔ tools loop inside the agent subgraph),
human-in-the-loop (remember, and whatever the knowledge and chart subgraphs interrupt for), subgraphs (agent, knowledge, chart).

Rules that hold here as everywhere:
- The model never decides a number. The agent subgraph keeps `graph._guard`. `verify_amounts` checks the final reply with
  `graph.unverified_amounts`, which trusts only what you typed and what a tool returned this turn. Memory, summaries and
  retrieved passages are never a source of figures.
- Anything that reaches a model is masked (`graph._masked` inside the agent, `mask_sensitive` here for context text).
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import RetryPolicy, interrupt
from pydantic import BaseModel, Field

from .. import tools as core
from ..money import parse_amount
from ..privacy import mask_sensitive
from . import memory as mem
from .contracts import ChartInput, RagInput

ROUTES = ("agent", "knowledge", "chart", "smalltalk", "remember")
ANSWER_STYLES = {"auto": "auto", "rag": "rag", "plain": "rag", "crag": "crag", "self_rag": "self_rag", "full": "full"}
CHIPS = {"do": "agent", "agent": "agent", "lookup": "knowledge", "knowledge": "knowledge", "chart": "chart"}
# Nodes whose model tokens are the answer the person reads. Graders, rewriters and the router stay out of the stream.
FINAL_NODES = frozenset({"agent", "smalltalk", "generate"})
DEFAULT_PERIOD = "last-90-days"
RECENT_FOR_RAG = 6


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_of(message: BaseMessage) -> str:
    return mem._text_of(message)


# ───────────────────────── stopping a turn ─────────────────────────

class TurnStopped(Exception):
    """Raised by StopHandler when the person pressed Stop."""


class StopHandler(BaseCallbackHandler):
    """Stop = set the event. The next time any model starts, or sends a token, this raises, which closes the model's HTTP
    stream (generation stops) and ends the run. Tools are never interrupted: a tool that has begun finishes, so a payment is
    either saved or not, never half-saved."""

    raise_error = True
    run_inline = True

    def __init__(self, cancelled) -> None:
        self.cancelled = cancelled      # a threading.Event

    def _check(self) -> None:
        if self.cancelled.is_set():
            raise TurnStopped()

    def on_chat_model_start(self, *args, **kwargs):
        self._check()

    def on_llm_start(self, *args, **kwargs):
        self._check()

    def on_llm_new_token(self, *args, **kwargs):
        self._check()


# ───────────────────────── routing (deterministic first) ─────────────────────────

_CHART = re.compile(r"(?i)\b(chart|plot|graph|draw|visuali[sz]e|histogram|bar chart|pie)\b")
_FIRST_PERSON = re.compile(r"(?i)\b(my|mine|me|i|i'm|i've|i'd|we)\b|₹|\brs\.?\b|\binr\b")
_DIGIT = re.compile(r"\d")
_AGENT = re.compile(r"(?i)\b(how much|total|spent|spend|spending|when did i|budget|paid|received|got paid|salary|stipend|log|record|add|saved?|"
                    r"balance|transactions?|expenses?|income|category|categories|review|insights?|over budget|commitment)\b")
_KNOWLEDGE = re.compile(r"(?i)^\s*(what is|what are|what's|whats|explain|how does|how do|how should|why|difference between|"
                        r"tips? (?:on|for)|advice|define|tell me about|is it (?:good|better|worth))\b")
_SMALLTALK = re.compile(r"(?i)^\s*(hi|hello|hey|yo|thanks|thank you|thx|ok|okay|cool|nice|great|good (?:morning|afternoon|evening|night)|bye|"
                        r"who are you|what can you do|help)\b[\s!.?,]*$")
_CHIP = re.compile(r"^\s*/(do|lookup|chart|agent|knowledge)\b\s*(.*)$", re.S | re.I)


def parse_chip(text: str) -> tuple[str, str]:
    """"/chart food by month" -> ("chart", "food by month"). No chip: ("", text)."""
    m = _CHIP.match(text or "")
    if not m:
        return "", text
    return CHIPS[m.group(1).lower()], m.group(2).strip() or text


def decide_route(text: str, forced: str = "") -> tuple[str, str] | None:
    """(route, reason) when fixed rules are sure, else None (the router model decides)."""
    if forced in ROUTES:
        return forced, "you picked it"
    if mem.detect_remember(text):
        return "remember", "asked to remember"
    if _SMALLTALK.match(text or ""):
        return "smalltalk", "greeting"
    if _CHART.search(text or ""):
        return "chart", "asked for a chart"
    if _KNOWLEDGE.match(text or "") and not _FIRST_PERSON.search(text or ""):
        return "knowledge", "a general question"
    if _AGENT.search(text or "") or _FIRST_PERSON.search(text or "") or _DIGIT.search(text or ""):
        return "agent", "about your records"
    return None


class RouteDecision(BaseModel):
    """Where to send a message that the fixed rules could not place."""

    route: Literal["agent", "knowledge", "chart", "smalltalk"] = Field(
        description="agent: the person's own records (log, totals, budgets). knowledge: a general money question answered from "
                    "guides. chart: draw a picture of their spending. smalltalk: greeting or chat.")
    reason: str = Field(default="", description="One short phrase.")


ROUTER_PROMPT = ("Pick where to send the person's message in an expense-tracking app. agent = anything about their own spending, "
                 "budgets or entries. knowledge = a general question about money habits or how something works. chart = they want "
                 "a picture. smalltalk = greeting or small talk.")

_PERIODS = (
    (re.compile(r"(?i)\bthis month\b"), lambda: date.today().strftime("%Y-%m")),
    (re.compile(r"(?i)\blast month\b"), lambda: (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")),
    (re.compile(r"(?i)\b(all time|everything|ever)\b"), lambda: "all"),
)
_LAST_N = re.compile(r"(?i)\blast (\d{1,3}) (day|week|month)s?\b")


def guess_period(text: str) -> str:
    """A period string for the chart worker, from plain words. The default is the last 90 days."""
    m = _LAST_N.search(text or "")
    if m:
        days = int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2).lower()]
        return f"last-{max(days, 1)}-days"
    for pattern, make in _PERIODS:
        if pattern.search(text or ""):
            return make()
    return DEFAULT_PERIOD


# ───────────────────────── state ─────────────────────────

def _events(old: list | None, new: list | None) -> list:
    """Trace events of THIS turn: a {"step": "turn"} event starts a fresh list; anything else is appended."""
    new = list(new or [])
    if new and new[0].get("step") == "turn":
        return new
    return list(old or []) + new


class SupervisorState(MessagesState):
    uid: str                  # whose long-term memory ("owner", or the signed-in person)
    thread_id: str            # for the memory the person asked to keep ("from this conversation")
    forced: str               # a chip: "agent" | "knowledge" | "chart" | ""
    mode: str                 # answer style: auto | plain | crag | self_rag | full
    review: bool              # chart: show the code before it runs
    route: str
    summary: str              # short-term memory: summary of the older turns (what the model sees; the thread keeps all)
    summarised_to: int        # how many messages that summary covers
    window: int               # how many of the newest messages the model is shown
    memories: list            # long-term memory lines, amounts already redacted
    rules: list               # category rules the person taught, as text
    events: Annotated[list, _events]


def _last_human(state: dict) -> HumanMessage | None:
    return next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)


def _stamp(message: BaseMessage, route: str) -> BaseMessage:
    extra = {"ts": (message.additional_kwargs or {}).get("ts") or now_iso(), "route": route}
    return message.model_copy(update={"additional_kwargs": {**(message.additional_kwargs or {}), **extra}})


def model_input(state: dict) -> list[BaseMessage]:
    """What a worker model is shown: the newest `window` messages, plus one system note (memory, taught rules, summary) placed
    just before the newest user message. The note is never saved in the thread."""
    messages = state["messages"]
    window = list(messages[-state["window"]:]) if state.get("window") else list(messages)
    note = mem.context_text(memories=state.get("memories"), rules=state.get("rules"), summary=state.get("summary") or "")
    if note:
        at = max((i for i, m in enumerate(window) if isinstance(m, HumanMessage)), default=0)
        window.insert(at, SystemMessage(mask_sensitive(note), id=str(uuid.uuid4())))
    return window


def _transient(exc: BaseException) -> bool:
    """Worth one more try: the model server hiccuped. A stop, a bad request or a bug is not."""
    if isinstance(exc, TurnStopped):
        return False
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return getattr(exc, "status_code", None) in (429, 500, 502, 503, 504)


SMALLTALK_PROMPT = ("You are Rectifi, a friendly assistant inside a personal expense tracker. Reply in one or two short sentences. "
                    "You have no tools in this reply, so do not claim to have saved or looked up anything, and never write an "
                    "amount of money. If they want something done, tell them to say it (for example 'spent 250 on swiggy').")


# ───────────────────────── the graph ─────────────────────────

def build_supervisor(*, checkpointer, store=None, agent_graph=None, agent_llm=None, chat_llm=None, grade_llm=None,
                     rag_graph=None, chart_graph=None):
    """Compile the supervisor. Models are looked up when a node first needs one, so building needs no model at all.

    agent_graph / rag_graph / chart_graph let tests (and the integrator) hand in a ready worker; by default the agent is the
    existing `graph.build_graph`, and RAG and charts come from `contracts` (they raise NotImplementedError until built).
    """
    lazy: dict[str, Any] = {}

    def chat_model():
        if chat_llm is not None:
            return chat_llm
        if "chat" not in lazy:
            from .llm import get_llm
            lazy["chat"] = get_llm("chat")
        return lazy["chat"]

    def grade_model():
        if grade_llm is not None:
            return grade_llm
        if "grade" not in lazy:
            from .llm import get_llm
            lazy["grade"] = get_llm("grade")
        return lazy["grade"]

    def agent_sub():
        if "agent" not in lazy:
            from .graph import build_graph

            full = agent_graph if agent_graph is not None else build_graph(llm=agent_llm)
            builder = getattr(full, "builder", None)
            # Recompiled WITHOUT a checkpointer of its own, so it inherits ours: its steps are saved inside the thread
            # (a stopped or crashed turn keeps the tool results) and its interrupts could resume.
            lazy["agent"] = builder.compile() if builder is not None else full
        return lazy["agent"]

    def rag_sub():
        if "rag" not in lazy:
            if rag_graph is not None:
                lazy["rag"] = rag_graph
            else:
                from .contracts import build_rag_subgraph
                from .llm import get_embeddings
                lazy["rag"] = build_rag_subgraph(llms={"grade": grade_model(), "chat": chat_model()}, embeddings=get_embeddings(),
                                                 checkpointer=None, store=store)
        return lazy["rag"]

    def chart_sub():
        if "chart" not in lazy:
            if chart_graph is not None:
                lazy["chart"] = chart_graph
            else:
                from .contracts import build_chart_subgraph
                from .llm import get_llm
                lazy["chart"] = build_chart_subgraph(llm=get_llm("code"), checkpointer=None)
        return lazy["chart"]

    def say(step: str, verdict: str, detail: str = "") -> dict:
        event = {"step": step, "verdict": verdict, "detail": detail}
        try:
            get_stream_writer()(event)
        except Exception:      # noqa: BLE001 - not inside a stream: nothing to tell
            pass
        return event

    # ── sequential: load_context ──
    def load_context(state: SupervisorState):
        update: dict[str, Any] = {"events": [{"step": "turn", "verdict": "start", "detail": ""}], "route": ""}
        last = _last_human(state)
        if last is not None:
            chip, stripped = parse_chip(text_of(last))
            if chip:
                update["forced"] = chip
                update["messages"] = [last.model_copy(update={"content": stripped})]     # same id: replaces the typed "/chart ..."
        update.setdefault("forced", state.get("forced") or "")
        return update

    # ── parallel branch 1: long-term memory ──
    def fetch_memories(state: SupervisorState):
        memories: list[str] = []
        rules: list[str] = []
        try:
            if store is not None:
                memories = mem.memory_lines(mem.list_memories(store, state.get("uid") or "owner"))
        except Exception:      # noqa: BLE001 - memory is a bonus; the chat works without it
            memories = []
        try:
            rules = [f"- '{mask_sensitive(r['pattern'])}' goes to {r['category']}" for r in core.memory_learned_rules()[:20]]
        except Exception:      # noqa: BLE001
            rules = []
        return {"memories": memories, "rules": rules}

    # ── parallel branch 2: short-term memory ──
    def trim_or_summarize(state: SupervisorState):
        plan = mem.plan_window(state["messages"], summary=state.get("summary") or "", summarised_to=state.get("summarised_to") or 0,
                               llm=grade_model() if mem.summary_cut(state["messages"]) > (state.get("summarised_to") or 0) else None)
        return plan

    # ── conditional: route ──
    def route(state: SupervisorState):
        last = _last_human(state)
        text = text_of(last) if last else ""
        decided = decide_route(text, state.get("forced") or "")
        how = "rules"
        if decided is None:
            how = "model"
            try:
                verdict = grade_model().with_structured_output(RouteDecision).invoke(
                    [SystemMessage(ROUTER_PROMPT), HumanMessage(mask_sensitive(text)[:1000])])
                if isinstance(verdict, dict):
                    verdict = RouteDecision.model_validate(verdict)
                decided = (verdict.route, verdict.reason or "the model chose")
            except Exception:      # noqa: BLE001 - the agent can do everything and keeps the amount guard: the safe default
                decided = ("agent", "the router was unsure")
                how = "default"
        chosen, reason = decided
        return {"route": chosen, "events": [say("route", chosen, f"{reason} ({how})")]}

    def pick(state: SupervisorState) -> str:
        return state["route"] if state.get("route") in ROUTES else "agent"

    # ── workers ──
    def agent(state: SupervisorState, config: RunnableConfig):
        shown = model_input(state)
        out = agent_sub().invoke({"messages": shown})
        new = [_stamp(m, "agent") for m in out["messages"][len(shown):]]
        return {"messages": new, "events": [say("agent", "done", f"{sum(isinstance(m, ToolMessage) for m in new)} tool step(s)")]}

    def knowledge(state: SupervisorState, config: RunnableConfig):
        last = _last_human(state)
        history = [{"role": "user" if isinstance(m, HumanMessage) else "assistant", "text": mask_sensitive(text_of(m))}
                   for m in state["messages"][:-1] if isinstance(m, (HumanMessage, AIMessage)) and text_of(m).strip()][-RECENT_FOR_RAG:]
        try:
            worker = rag_sub()
        except NotImplementedError as exc:
            reply = AIMessage("Looking things up in the guides isn't available in this build yet.", id=str(uuid.uuid4()))
            return {"messages": [_stamp(reply, "knowledge")], "events": [say("knowledge", "unavailable", str(exc)[:200])]}
        payload = RagInput(question=mask_sensitive(text_of(last)), mode=ANSWER_STYLES.get(state.get("mode") or "auto", "auto"),
                           history=history, uid=state.get("uid") or "owner")
        out = worker.invoke(payload, config)
        reply = AIMessage(out.get("answer") or "I couldn't find that in the guides.", id=str(uuid.uuid4()), additional_kwargs={
            "citations": out.get("citations") or [], "trace": out.get("trace") or [], "unverified_claims": out.get("unverified") or [],
            "support": out.get("support"), "useful": out.get("useful"), "rag_mode": out.get("mode"), "abstained": bool(out.get("abstained"))})
        return {"messages": [_stamp(reply, "knowledge")], "events": [say("knowledge", "answered", f"{len(out.get('citations') or [])} source(s)")]}

    def chart(state: SupervisorState, config: RunnableConfig):
        last = _last_human(state)
        try:
            worker = chart_sub()
        except NotImplementedError as exc:
            reply = AIMessage("Drawing charts isn't available in this build yet.", id=str(uuid.uuid4()))
            return {"messages": [_stamp(reply, "chart")], "events": [say("chart", "unavailable", str(exc)[:200])]}
        request = text_of(last)
        out = worker.invoke(ChartInput(request=mask_sensitive(request), period=guess_period(request), review=bool(state.get("review"))), config)
        if out.get("ok") and out.get("chart_id") is not None:
            reply = AIMessage(f"I drew your chart. It is saved in Charts as number {out['chart_id']}.", id=str(uuid.uuid4()),
                              additional_kwargs={"artifact": {"chart_id": out["chart_id"]}, "trace": out.get("trace") or []})
        else:
            reply = AIMessage(out.get("error") or "I couldn't draw that chart. Try describing the period and the categories.",
                              id=str(uuid.uuid4()), additional_kwargs={"trace": out.get("trace") or []})
        return {"messages": [_stamp(reply, "chart")], "events": [say("chart", "ok" if out.get("ok") else "failed", out.get("error") or "")]}

    def smalltalk(state: SupervisorState):
        shown = model_input(state)
        reply = chat_model().invoke([SystemMessage(SMALLTALK_PROMPT), *[mem_masked(m) for m in shown]], config={"tags": ["final"]})
        return {"messages": [_stamp(reply, "smalltalk")], "events": [say("smalltalk", "replied")]}

    def remember(state: SupervisorState):
        last = _last_human(state)
        found = mem.detect_remember(text_of(last) if last else "")
        if found is None:
            reply = AIMessage("Tell me what to remember, for example: remember that I prefer short answers.", id=str(uuid.uuid4()))
            return {"messages": [_stamp(reply, "remember")]}
        kind, fact = found
        # Everything above this line runs again after the person answers; nothing above writes anything.
        answer = interrupt({"kind": "memory", "prompt": "Save this to memory?", "memory_kind": kind, "text": mem.clean_text(fact)})
        approved = answer is True or (isinstance(answer, dict) and bool(answer.get("approve")))
        if approved and store is not None:
            result = mem.remember_fact.invoke({"kind": kind, "text": fact, "uid": state.get("uid") or "owner", "store": store,
                                               "thread_id": state.get("thread_id") or None})
            text, verdict = f"{result}. You can see or change it on the Memory page.", "saved"
        else:
            text, verdict = "OK, I won't save that.", "declined"
        return {"messages": [_stamp(AIMessage(text, id=str(uuid.uuid4())), "remember")], "events": [say("remember", verdict)]}

    # ── deterministic check of the final reply ──
    def verify_amounts(state: SupervisorState):
        from .graph import unverified_amounts

        messages = state["messages"]
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage) and text_of(m)), None)
        if last_ai is None:
            return {}
        kw = last_ai.additional_kwargs or {}
        if kw.get("route") == "knowledge" and "unverified_claims" in kw:
            # The tool results and the cited entries that back a figure live inside the RAG subgraph, out of sight here. Its own
            # plain-code check (rag.graph._untraced, no model) already ran with them, so its list is the answer. Same text as wire.money().
            odd = []
            for item in kw.get("unverified_claims") or []:
                try:
                    odd.append(format(parse_amount(item), "f"))
                except ValueError:
                    odd.append(str(item))
        else:
            odd = [format(v, "f") for v in unverified_amounts(messages)]
        event = say("amounts", "unverified" if odd else "ok",
                    f"{len(odd)} figure(s) could not be traced" if odd else "no figure to trace, or every figure traced to your words or a tool")
        events = list(state.get("events") or []) + [event]
        kwargs = {**(last_ai.additional_kwargs or {}), "unverified": odd, "turn_trace": events}
        return {"messages": [last_ai.model_copy(update={"additional_kwargs": kwargs})], "events": [event]}

    g = StateGraph(SupervisorState)
    g.add_node("load_context", load_context)
    g.add_node("fetch_memories", fetch_memories)
    g.add_node("trim_or_summarize", trim_or_summarize)
    g.add_node("route", route)
    g.add_node("agent", agent)
    g.add_node("knowledge", knowledge)
    g.add_node("chart", chart)
    g.add_node("smalltalk", smalltalk, retry_policy=RetryPolicy(max_attempts=2, initial_interval=0.2, retry_on=_transient))
    g.add_node("remember", remember)
    g.add_node("verify_amounts", verify_amounts)
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "fetch_memories")
    g.add_edge("load_context", "trim_or_summarize")
    g.add_edge(["fetch_memories", "trim_or_summarize"], "route")
    g.add_conditional_edges("route", pick, {name: name for name in ROUTES})
    for worker in ROUTES:
        g.add_edge(worker, "verify_amounts")
    g.add_edge("verify_amounts", END)
    return g.compile(checkpointer=checkpointer, store=store)


def mem_masked(message: BaseMessage) -> BaseMessage:
    content = message.content
    if isinstance(content, str):
        masked = mask_sensitive(content)
        if masked != content:
            return message.model_copy(update={"content": masked})
    return message


def new_human(text: str, *, ts: str | None = None) -> HumanMessage:
    """The message a turn starts with: it carries its time and an id of its own (regenerate finds the turn by it)."""
    return HumanMessage(text, id=str(uuid.uuid4()), additional_kwargs={"ts": ts or now_iso()})
