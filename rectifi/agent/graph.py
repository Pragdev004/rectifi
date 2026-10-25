"""The LangGraph agent.

    START → agent ──(tool calls?)──► guarded_tools ──► agent ──► … ──► END
                 └──(plain reply)──────────────────────────────────────►

The LLM decides WHAT to do (log, look up, budget, ask a question). It never decides a number:
`guarded_tools` refuses any amount that doesn't appear in what the user actually typed.
"""

from __future__ import annotations

import re
from datetime import date

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, BaseToolkit
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from .. import config
from .. import tools as core
from ..money import amounts_in_text, fmt_inr, parse_amount
from ..parsing import AMOUNT, parse_sms
from ..privacy import mask_sensitive
from .llm import get_llm
from .tools import AGENT_TOOLS, TOOLS_BY_NAME

# The tools that only READ. The knowledge graph's "tools" route gets exactly these and nothing else, so a write (log, budget,
# recategorize, commitment) is impossible there: the model is never shown a write tool and the ToolNode has none to run.
READ_ONLY_NAMES = ("spending_report", "find_transactions", "check_budgets", "weekly_review", "insights", "list_categories")
READ_ONLY_TOOLS = [TOOLS_BY_NAME[n] for n in READ_ONLY_NAMES]


class RectifiReadOnlyToolkit(BaseToolkit):
    """A LangChain toolkit: a named group of tools handed to an agent together. This one holds the read-only tools."""

    def get_tools(self) -> list[BaseTool]:
        return list(READ_ONLY_TOOLS)

HISTORY_TURNS = 8        # how many recent user turns the model sees (keeps cost flat as chats grow)
GUARD_LOOKBACK = 3       # an amount may come from any of the user's last N messages (clarification flows)


def system_prompt() -> str:
    categories = ", ".join(c["name"] for c in core.list_categories())
    return f"""You are Rectifi, a personal finance assistant for one person in India. Today is {date.today():%A, %d %B %Y}.
Currency is Indian rupees (₹).

What you do:
- When the user mentions money they spent or received, record it with log_transaction (one call per transaction).
- When they paste a bank SMS, call log_sms with the SMS text as you see it. (Long numbers in it are hidden from you on purpose.)
- For questions about totals, savings or categories, call spending_report. For specific payments, find_transactions.
- Budgets: set_budget and check_budgets.
- For "how was my week" or a weekly review, call weekly_review. Only call save_commitment when the user tells you their commitment.
- For "how can I manage my money better", "what should I fix" or "am I wasting money", call insights and quote its titles and figures exactly.

Hard rules:
1. Never invent, estimate, convert or add up amounts. Pass amounts exactly as the user typed them. Every total you
   mention must be copied from a tool result.
2. If the amount, or whether money went OUT (debit) or came IN (credit), is unclear — ask one short question first.
3. If the category is ambiguous, pick the closest one from: {categories}. If none fits, leave it empty.
4. After recording, confirm in one line, using the 'saved' text from the tool result.
5. If a tool returns an error, explain it plainly and ask what the user wants to do.
6. Keep replies short. No markdown tables unless the user asks for a breakdown."""


def _recent_window(messages: list) -> list:
    """Messages from the HISTORY_TURNS-th last user message onward — never splits a tool call from its result."""
    human_positions = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(human_positions) <= HISTORY_TURNS:
        return messages
    return messages[human_positions[-HISTORY_TURNS]:]


def _masked(message):
    """A copy of the message with account/card/reference numbers masked. The saved chat state stays raw."""
    content = message.content
    if isinstance(content, str):
        masked = mask_sensitive(content)
        if masked != content:
            return message.model_copy(update={"content": masked})
    return message


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _original_sms(llm_text: str, messages: list) -> str | None:
    """Find the real SMS the user pasted, for a log_sms call whose text came back from the (masked) model.

    The model never sees raw SMS text, so we never trust the text it sends. We look in the user's last
    few messages for one that parses as a bank SMS and whose masked form contains what the model sent.
    """
    wanted = _squash(llm_text)
    if not wanted:
        return None
    texts = [m.content for m in messages if isinstance(m, HumanMessage) and isinstance(m.content, str)]
    for raw in reversed(texts[-GUARD_LOOKBACK:]):
        if parse_sms(raw) is None:
            continue
        if wanted in _squash(mask_sensitive(raw)) or wanted in _squash(raw):
            return raw
    return None


def _user_amounts(messages: list) -> set:
    texts = [m.content for m in messages if isinstance(m, HumanMessage) and isinstance(m.content, str)]
    allowed = set()
    for text in texts[-GUARD_LOOKBACK:]:
        allowed |= amounts_in_text(text)
    return allowed


def _guard(call: dict, allowed: set) -> str | None:
    """Return a refusal message if this tool call carries an amount the user never typed."""
    name, args = call["name"], call.get("args") or {}
    if name == "save_commitment":
        invented = sorted(amounts_in_text(args.get("text", "")) - allowed)
        if invented:
            numbers = ", ".join(str(n.normalize()) for n in invented)
            return (f"GUARD: refused — the commitment mentions {numbers}, which the user never typed. "
                    "Use only the user's own words and numbers, or ask them what they want to commit to.")
        return None
    try:
        if name in ("log_transaction", "set_budget"):
            amount = parse_amount(str(args.get("amount", "")))
        elif name == "log_sms":
            parsed = parse_sms(args.get("sms_text", ""))
            if parsed is None:
                return None  # the tool itself will explain the SMS couldn't be parsed
            amount = parsed.amount
        else:
            return None
    except ValueError:
        return f"GUARD: {args.get('amount')!r} is not a plain number. Ask the user for the exact amount."
    if amount not in allowed:
        return (
            f"GUARD: refused — {fmt_inr(amount)} does not appear in anything the user typed. "
            "Do not guess amounts. Ask the user to confirm the exact figure."
        )
    return None


def _rupee_figures(text: str) -> set:
    """Every ₹ / Rs. / INR figure written in the text, as Decimals."""
    figures = set()
    for raw in AMOUNT.findall(text or ""):
        try:
            figures.add(parse_amount(raw))
        except ValueError:
            continue
    return figures


def unverified_amounts(messages: list) -> list:
    """Rupee figures in the LAST assistant reply that Rectifi can't trace to anything real.

    A figure is fine if the user typed it (in their last few messages) or if a tool returned it during this turn.
    Anything else was made up or computed by the model, which is exactly what Rectifi promises never happens,
    so the UI shows a warning under the reply. Returns the odd figures in the order they appear.
    """
    last_ai = next((i for i in range(len(messages) - 1, -1, -1)
                    if isinstance(messages[i], AIMessage) and isinstance(messages[i].content, str) and messages[i].content), None)
    if last_ai is None:
        return []
    before = messages[:last_ai]
    allowed = _user_amounts(before)
    for m in reversed(before):                       # tool results from this turn only (back to the user's message)
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, ToolMessage):
            allowed |= _rupee_figures(str(m.content))
    odd = []
    for raw in AMOUNT.findall(messages[last_ai].content):
        try:
            value = parse_amount(raw)
        except ValueError:
            continue
        if value not in allowed and value not in odd:
            odd.append(value)
    return odd


def build_graph(llm=None, checkpointer=None, tools=None):
    """`tools` defaults to every agent tool. Pass a smaller list (for example READ_ONLY_TOOLS) to build a more limited agent."""
    chosen = list(tools) if tools is not None else AGENT_TOOLS
    by_name = {t.name: t for t in chosen}
    model = (llm or get_llm()).bind_tools(chosen)

    def agent(state: MessagesState):
        window = [_masked(m) for m in _recent_window(state["messages"])]
        reply = model.invoke([SystemMessage(system_prompt()), *window])
        return {"messages": [reply]}

    def guarded_tools(state: MessagesState):
        last = state["messages"][-1]
        allowed = _user_amounts(state["messages"])
        results = []
        for call in last.tool_calls:
            if call["name"] == "log_sms":
                raw = _original_sms((call.get("args") or {}).get("sms_text", ""), state["messages"])
                if raw is None:
                    results.append(ToolMessage(
                        "GUARD: I can only record an SMS the user pasted. Ask them to paste it again.",
                        tool_call_id=call["id"], name=call["name"], status="error"))
                    continue
                call = {**call, "args": {**(call.get("args") or {}), "sms_text": raw}}
            refusal = _guard(call, allowed)
            if refusal:
                results.append(ToolMessage(refusal, tool_call_id=call["id"], name=call["name"], status="error"))
                continue
            tool = by_name.get(call["name"])
            if tool is None:
                results.append(ToolMessage(f"Unknown tool {call['name']}", tool_call_id=call["id"], name=call["name"], status="error"))
                continue
            try:
                output = tool.invoke(call.get("args") or {})
            except Exception as exc:  # bad arguments from the model: let it see the error and recover
                output = f"Tool error: {exc}"
            results.append(ToolMessage(str(output), tool_call_id=call["id"], name=call["name"]))
        return {"messages": results}

    def route(state: MessagesState):
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", guarded_tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def postgres_checkpointer():
    """Conversation memory in the same Postgres database, so chats survive app restarts."""
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    conn = psycopg.connect(config.database_url(), autocommit=True, prepare_threshold=0, row_factory=dict_row)
    saver = PostgresSaver(conn)
    saver.setup()
    return saver


def chat(graph, text: str, thread_id: str) -> list:
    """Send one user message; return only the new messages produced in this turn."""
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 16}
    before = len(graph.get_state(cfg).values.get("messages", []))
    state = graph.invoke({"messages": [HumanMessage(text)]}, cfg)
    return state["messages"][before:]


def chat_events(graph, text: str, thread_id: str):
    """Like chat(), but a generator: yields ("tool", ToolMessage) as each tool finishes, then ("done", new_messages).

    The web page uses it to show "✓ log_transaction" while the model is still working. The guard, the masking and
    the original-SMS swap live inside the graph's nodes, so they run exactly as in chat()."""
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 16}
    before = len(graph.get_state(cfg).values.get("messages", []))
    for update in graph.stream({"messages": [HumanMessage(text)]}, cfg, stream_mode="updates"):
        for node, payload in update.items():
            if node == "tools":
                for message in (payload or {}).get("messages", []):
                    yield ("tool", message)
    yield ("done", graph.get_state(cfg).values["messages"][before:])
