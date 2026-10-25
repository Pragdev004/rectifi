"""CRAG + Self-RAG as one LangGraph (a subgraph the chat supervisor calls).

    START -> decide_retrieval --tools----> tools_answer (read-only ReAct subgraph: ToolNode + tools_condition) --> check_support
                |-- direct ----> generate_direct ----------------------------------------------------------> check_support
                |-- decline ---> decline --> finalize
                '-- retrieve --> [multi_query] --> retrieve --> (Send per passage) grade_one x N --> grade_join
                                                    ^                                                   |
                      rewrite_query <---------------+-- incorrect / ambiguous (CRAG)                    |-- correct ---> refine --> generate
                        '--> [web_consent --> web_search] --> retrieve (broader)                         '-- (rag / self_rag skip CRAG) --> generate
    generate --> finalize                       (modes rag, crag)
    generate --> check_support --> check_useful --> loop edge  (modes self_rag, full, auto)
         loop edge: supported and useful -> finalize
                    not supported, regenerations left -> generate (strict prompt that lists the unsupported claims)
                    not useful, re-retrievals left    -> rewrite_query -> retrieve
                    else                              -> finalize (with a caveat, or no answer if a rupee figure is untraceable)

MODES (state.mode):   rag       retrieve -> generate
                      crag      always retrieve, multi-query, grade, correct (refine / rewrite + retrieve again / web), generate
                      self_rag  decide whether to retrieve, grade (filter only), generate, reflect (support, usefulness), loop
                      full/auto everything above; "auto" is the default and lets the router decide

CRAG verdict from the passage grades (as in the CRAG paper): correct = at least one "yes"; incorrect = every grade "no" (or nothing
was found); ambiguous = in between (some "partial", no "yes").

HONEST NOTE: Self-RAG's reflection here is PROMPT-BASED. Real Self-RAG fine-tunes a model to emit reflection tokens; Rectifi uses
ordinary structured-output prompts to a small model (see graders.py), one candidate per round. The rupee check is plain code.

RULE 1 (the LLM never decides a number): a rupee figure in an answer must come from a read-only tool result, the person's own
words, or a cited note / entry chunk. Guides contain none. Plain code (agent.graph.unverified_amounts) checks this BEFORE any
model grader runs. A failure forces regeneration, then the answer is withheld. Loops are bounded by config.RAG_MAX_REGENERATE
and config.RAG_MAX_RETRIEVE, read each time the graph runs.
"""

from __future__ import annotations

import re
from typing import Annotated, TypedDict

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Send, interrupt

from .. import config
from ..agent.contracts import RagInput, RagOutput, TraceEvent
from ..agent.graph import READ_ONLY_TOOLS, _masked, unverified_amounts
from ..money import fmt_inr
from ..privacy import mask_sensitive
from . import graders, web
from .retrieve import CORPORA, StripCompressor, multi_query, retrieve_many
from .store import PgVectorStore

MODES = ("auto", "rag", "crag", "self_rag", "full")
REFLECT_MODES = ("auto", "self_rag", "full")
RECURSION_LIMIT = 80
HISTORY_KEEP = 4

LABELS = {"guide": "GUIDE", "note": "YOUR NOTE", "txn": "YOUR ENTRY", "upload": "YOUR FILE", "web": "WEB", "tool": "YOUR NUMBERS"}
DECLINE_TEXT = "I can only help with your money and Rectifi's guides. Try asking about your spending, budgets, or a money topic like UPI safety or SIPs."
NOT_FOUND_TEXT = "I couldn't find that in Rectifi's guides or your notes, so I won't guess. You could add a note about it in the Library, or ask in a different way."
WITHHELD_TEXT = ("I wrote an answer, but it had a rupee amount I could not match to your records or to what you typed, so I am not showing it. "
                 "Ask again with the numbers you mean, or ask for a spending report.")


class RagResult(RagOutput):
    """RagOutput plus what the supervisor and the UI also want."""

    route: str
    mode: str
    abstained: bool


def _grades_reducer(left: list | None, right: list | None) -> list:
    """Append grades; an item {"reset": True} clears the list (a new retrieval round starts fresh)."""
    out = list(left or [])
    for item in right or []:
        if item.get("reset"):
            out = []
        else:
            out.append(item)
    return out


def _trace_reducer(left: list | None, right: list | None) -> list:
    """Append trace events; {"reset": True} starts a new run's trace (a thread that is asked twice must not mix them)."""
    out = list(left or [])
    for item in right or []:
        if item.get("reset"):
            out = []
        else:
            out.append(item)
    return out


class RagState(TypedDict, total=False):
    question: str
    mode: str
    history: list
    uid: str
    route: str
    queries: list[str]
    documents: list[Document]
    carry: list[Document]
    grades: Annotated[list, _grades_reducer]
    crag: str | None
    kept: list[Document]
    answer: str
    citations: list[dict]
    support: str | None
    unsupported: list[str]
    unverified: list[str]
    useful: bool | None
    retrieval_rounds: int
    generation_rounds: int
    allowed_text: str
    web_ok: bool
    rewritten: str
    abstained: bool
    trace: Annotated[list, _trace_reducer]


def ev(step: str, verdict: str, detail: str = "") -> TraceEvent:
    return {"step": step, "verdict": verdict, "detail": detail}


def normalize_mode(mode: str | None) -> str:
    m = (mode or "auto").strip().lower().replace("-", "_").replace(" ", "_")
    m = {"plain": "rag", "selfrag": "self_rag"}.get(m, m)
    return m if m in MODES else "auto"          # "library", "web", "off" (older contract wording) and unknown words mean auto


def history_messages(history: list | None) -> list[BaseMessage]:
    """The last few earlier messages as LangChain messages, masked. Accepts messages or {"role", "content"} dicts."""
    out: list[BaseMessage] = []
    for item in (history or [])[-HISTORY_KEEP:]:
        if isinstance(item, BaseMessage):
            role, text = ("human" if isinstance(item, HumanMessage) else "ai" if isinstance(item, AIMessage) else ""), item.content
        elif isinstance(item, dict):
            role, text = {"user": "human", "human": "human", "assistant": "ai", "ai": "ai"}.get(str(item.get("role")), ""), item.get("content", "")
        else:
            continue
        if role and isinstance(text, str) and text.strip():
            out.append(HumanMessage(mask_sensitive(text)) if role == "human" else AIMessage(mask_sensitive(text)))
    return out


def _context(docs: list[Document]) -> str:
    return "\n\n".join(f"[{i}] {d.metadata.get('title', 'Source')} ({LABELS.get(d.metadata.get('corpus'), 'SOURCE')})\n{d.page_content}"
                       for i, d in enumerate(docs, start=1))


def _citations(answer: str, docs: list[Document]) -> list[dict]:
    """The sources the answer actually cites with [n], in the order first cited."""
    out, seen = [], set()
    for m in re.finditer(r"\[(\d{1,2})\]", answer or ""):
        n = int(m.group(1))
        if n in seen or not 1 <= n <= len(docs):
            continue
        seen.add(n)
        d = docs[n - 1]
        corpus = d.metadata.get("corpus", "guide")
        out.append({"n": n, "source": d.metadata.get("source", ""), "title": d.metadata.get("title", ""), "chunk": d.metadata.get("chunk_index", 0),
                    "quote": d.page_content[:240], "corpus": corpus, "label": LABELS.get(corpus, "SOURCE")})
    return out


def _figure_text(state: RagState) -> str:
    """The text whose rupee figures are allowed: tool results, plus the cited note and entry chunks (the person's own words and
    the database's own rows). Guides, files and web pages are NOT in here."""
    parts = [state.get("allowed_text") or ""]
    parts += [c["quote_full"] for c in state.get("citations", []) if c.get("corpus") in ("note", "txn") and c.get("quote_full")]
    return "\n".join(p for p in parts if p)


def _untraced(state: RagState, answer: str) -> list[str]:
    """Rupee figures in `answer` that trace to nothing. Plain code, no model: agent.graph.unverified_amounts on a tiny transcript."""
    messages = [HumanMessage(state.get("question", "")), ToolMessage(_figure_text(state) or "-", tool_call_id="rag"), AIMessage(answer or "")]
    return [fmt_inr(v) for v in unverified_amounts(messages)]


def _counts(grades: list[dict]) -> tuple[int, int, int]:
    g = [x["grade"] for x in grades]
    return g.count("yes"), g.count("partial"), g.count("no")


# ───────────── prompts ─────────────

GENERATE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You are Rectifi's helper for a student in India. Answer ONLY from the numbered sources below. Cite the source after each claim "
               "like [1]. Use short plain English. Do not write any rupee amount unless a source shows it. If the sources do not answer the "
               "question, say so in one sentence.\n\nSources:\n{context}"),
    MessagesPlaceholder("history"),
    ("human", "{question}"),
])
STRICT_SUFFIX = ("\n\nYour last answer had claims the sources do not back: {claims}\nWrite the answer again. Use only what the sources say. "
                 "Remove every claim and every rupee amount that is not in a source.")
DIRECT_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You are Rectifi, a friendly money helper for a student in India. Reply in one or two short sentences. Do not state any "
               "rupee amount and do not make up facts about the person's money."),
    MessagesPlaceholder("history"),
    ("human", "{question}"),
])
REWRITE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "The first search found nothing useful for this question. Rewrite it as one clear, standalone search question in plain "
               "words. Keep names and dates. Reply with the question only."),
    ("human", "Question: {question}\nTried before: {tried}"),
])
TOOLS_SYSTEM = ("You are Rectifi. Answer the question using the tools. They only read the person's records. Quote every figure exactly "
                "as a tool returned it. Never add, subtract or estimate amounts. If a tool returns nothing, say so plainly. Keep it short.")


def _first_line(text: str, limit: int = 200) -> str:
    return (text or "").strip().splitlines()[0].strip(" \"'")[:limit] if (text or "").strip() else ""


# ───────────── the read-only tools subgraph ─────────────

def build_readonly_agent(llm):
    """A tiny ReAct loop: agent -> ToolNode(READ_ONLY_TOOLS) -> agent. The model is only shown the read-only tools, and the ToolNode
    only has those, so a write tool is impossible (a made-up call gets an error message back, nothing runs)."""
    model = llm.bind_tools(READ_ONLY_TOOLS)

    def agent(state: MessagesState):
        return {"messages": [model.invoke([SystemMessage(TOOLS_SYSTEM), *[_masked(m) for m in state["messages"]]])]}

    g = StateGraph(MessagesState)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(READ_ONLY_TOOLS))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile()


# ───────────── the graph ─────────────

def build_rag_subgraph(*, llms: dict, embeddings, checkpointer=None, store=None, vector_store: PgVectorStore | None = None,
                       embed_model: str | None = None):
    """`llms`: {"grade": small model for graders and the router, "chat": model that writes answers and calls tools}. `store` is the
    LangGraph long-term store (unused here: long-term memories are never an allowed source of rupee figures). `vector_store` and
    `embed_model` are for tests; by default the PgVectorStore over rag_chunks with config.embed_model()."""
    grade_llm = llms.get("grade") or llms["chat"]
    chat_llm = llms.get("chat") or llms["grade"]
    vs = vector_store or PgVectorStore(embeddings, embed_model=embed_model or config.embed_model())
    readonly_agent = build_readonly_agent(chat_llm)
    to_text = StrOutputParser()

    # ----- routing -----

    def decide_retrieval(state: RagState):
        mode = normalize_mode(state.get("mode"))
        q = mask_sensitive(state.get("question", ""))
        if mode in ("rag", "crag"):
            route, why, by = "retrieve", "this answer style always looks things up", "mode"
        else:
            d = graders.decide_route(grade_llm, q)
            route, why, by = d["route"], d["reason"], d["by"]
        shown = {"tools": "use your numbers", "retrieve": "look it up", "direct": "answer directly", "decline": "not for me"}[route]
        return {"mode": mode, "route": route, "retrieval_rounds": 0, "generation_rounds": 0, "citations": [], "unverified": [], "unsupported": [],
                "carry": [], "kept": [], "documents": [], "allowed_text": "", "support": None, "useful": None, "abstained": False,
                "answer": "", "queries": [], "rewritten": "", "crag": None, "web_ok": False, "grades": [{"reset": True}],
                "trace": [{"reset": True}, ev("ROUTED", shown, f"{why} ({by})".strip())]}

    def after_route(state: RagState):
        route = state["route"]
        if route == "tools":
            return "tools_answer"
        if route == "direct":
            return "generate_direct"
        if route == "decline":
            return "decline"
        return "multi_query" if state["mode"] in ("crag", "full", "auto") else "retrieve"

    def decline(state: RagState):
        return {"answer": DECLINE_TEXT, "abstained": True}

    # ----- the tools route -----

    def tools_answer(state: RagState):
        q = mask_sensitive(state["question"])
        result = readonly_agent.invoke({"messages": [*history_messages(state.get("history")), HumanMessage(q)]}, {"recursion_limit": 14})
        msgs = result["messages"]
        answer = next((m.content for m in reversed(msgs) if isinstance(m, AIMessage) and isinstance(m.content, str) and m.content.strip()), "")
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        used = [m.name or "tool" for m in tool_msgs if getattr(m, "status", "success") != "error"]
        cites = [{"n": i, "source": f"tool:{m.name}", "title": m.name or "tool", "chunk": 0, "quote": str(m.content)[:240], "corpus": "tool",
                  "label": LABELS["tool"], "quote_full": str(m.content)} for i, m in enumerate(tool_msgs, start=1)
                 if getattr(m, "status", "success") != "error"]
        return {"answer": answer.strip(), "citations": cites, "generation_rounds": 1,
                "allowed_text": "\n".join(str(m.content) for m in tool_msgs if getattr(m, "status", "success") != "error"),
                "trace": [ev("TOOLS", "read-only", ", ".join(dict.fromkeys(used)) or "no tool was needed")]}

    def generate_direct(state: RagState):
        chain = DIRECT_PROMPT | chat_llm | to_text
        answer = chain.invoke({"history": history_messages(state.get("history")), "question": mask_sensitive(state["question"])})
        return {"answer": answer.strip(), "generation_rounds": 1, "trace": [ev("GENERATED", "direct", "no lookup needed")]}

    # ----- retrieval -----

    def multi_query_node(state: RagState):
        q = mask_sensitive(state["question"])
        try:
            queries = multi_query(grade_llm).invoke({"question": q})
        except Exception:
            queries = [q]
        return {"queries": queries, "trace": [ev("EXPANDED", f"{len(queries)} queries", " | ".join(queries))]}

    def retrieve(state: RagState):
        rounds = state.get("retrieval_rounds", 0)
        q = mask_sensitive(state["question"])
        queries = state.get("queries") or [q]
        broader = rounds > 0
        k = config.RAG_TOP_K + (2 if broader else 0)
        docs = retrieve_many(queries, vs, CORPORA, k=k, fetch_k=k * 4, limit=k + 2)
        merged: list[Document] = []
        seen: set = set()
        for d in [*state.get("carry", []), *docs]:
            key = d.metadata.get("chunk_id") or d.page_content
            if key not in seen:
                seen.add(key)
                merged.append(d)
        return {"documents": merged, "carry": [], "retrieval_rounds": rounds + 1, "grades": [{"reset": True}],
                "trace": [ev("RETRIEVED", f"{len(merged)} passages", ("broader search" if broader else "first search"))]}

    def fan_out(state: RagState):
        docs = state.get("documents", [])
        if state["mode"] == "rag" or not docs:
            return "grade_join"
        q = mask_sensitive(state["question"])
        return [Send("grade_one", {"question": q, "doc": d, "idx": i}) for i, d in enumerate(docs)]

    def grade_one(payload: dict):
        g = graders.grade_document(grade_llm, payload["question"], payload["doc"].page_content)
        return {"grades": [{"idx": payload["idx"], **g}]}

    def grade_join(state: RagState):
        docs = state.get("documents", [])
        mode = state["mode"]
        cap = config.RAG_TOP_K + 2
        if mode == "rag":
            return {"kept": docs[: config.RAG_TOP_K], "crag": None, "trace": []}
        by_idx = {g["idx"]: g for g in state.get("grades", [])}
        ordered = [(by_idx.get(i, {"grade": "partial"})["grade"], d) for i, d in enumerate(docs)]
        yes, partial, no = _counts([{"grade": g} for g, _ in ordered])
        kept = [d for g, d in ordered if g == "yes"] + [d for g, d in ordered if g == "partial"]
        trace = [ev("GRADED", f"{yes} relevant · {partial} partly · {no} not", f"{len(docs)} passages checked one by one")]
        crag = None
        if mode != "self_rag":
            crag = "correct" if yes else ("ambiguous" if partial else "incorrect")
            trace.append(ev("CRAG", crag.upper(), {"correct": "at least one passage answers the question",
                                                  "ambiguous": "passages are related but none answers it",
                                                  "incorrect": "nothing found is relevant"}[crag]))
        return {"kept": kept[:cap], "crag": crag, "trace": trace}

    def after_grade(state: RagState):
        mode, crag, kept = state["mode"], state.get("crag"), state.get("kept", [])
        again = state.get("retrieval_rounds", 0) <= config.RAG_MAX_RETRIEVE
        if mode == "rag":
            return "generate" if kept else "finalize"
        if mode == "self_rag":
            return "generate" if kept else "finalize"
        if crag == "correct":
            return "refine"
        if crag == "ambiguous":
            return "rewrite_query" if again else "refine"
        return "rewrite_query" if again else "finalize"        # incorrect: try again, or stop and say so

    def rewrite_query(state: RagState):
        q = mask_sensitive(state["question"])
        tried = "; ".join(state.get("queries") or [q])
        text = (REWRITE_PROMPT | grade_llm | to_text).invoke({"question": q, "tried": tried})
        rewritten = _first_line(mask_sensitive(text)) or q
        carry = state.get("kept", []) if state.get("crag") == "ambiguous" else []
        return {"rewritten": rewritten, "queries": [rewritten], "carry": carry, "web_ok": False,
                "trace": [ev("REWROTE", "asked again", f"Asked again as: {rewritten}")]}

    def web_allowed(state: RagState) -> bool:
        if state.get("crag") != "incorrect" or not web.available() or graders.is_personal(state["question"]):
            return False
        return not any(d.metadata.get("corpus") in ("note", "txn") for d in state.get("kept", []))

    def after_rewrite(state: RagState):
        return "web_consent" if web_allowed(state) else "retrieve"

    def web_consent(state: RagState):
        answer = interrupt({"type": "web_search", "query": state["rewritten"],
                            "message": f"Search the web for '{state['rewritten']}'? Only this query is sent to DuckDuckGo. Nothing about your money is sent.",
                            "choices": ["Search", "Answer without it"]})
        allowed = answer is True or (isinstance(answer, dict) and answer.get("search") is True) or (isinstance(answer, str) and answer.lower() in ("search", "yes", "true"))
        return {"web_ok": bool(allowed), "trace": [ev("WEB", "asked" if allowed else "declined", "you allowed one generic search" if allowed else "you chose to answer without the web")]}

    def after_consent(state: RagState):
        return "web_search" if state.get("web_ok") else "retrieve"

    def web_search(state: RagState):
        docs = web.search(state["rewritten"])
        return {"carry": [*state.get("carry", []), *docs], "trace": [ev("WEB", f"{len(docs)} results", "only the rewritten, masked question was sent")]}

    def refine(state: RagState):
        kept = state.get("kept", [])
        refined = StripCompressor(embeddings=embeddings).compress_documents(kept, mask_sensitive(state["question"]))
        before = sum(d.metadata.get("strips_total", 1) for d in refined)
        after = sum(d.metadata.get("strips_kept", 1) for d in refined)
        return {"kept": refined, "trace": [ev("REFINED", f"kept {after} of {before} sentences", "dropped sentences that are far from the question")]}

    # ----- writing -----

    def generate(state: RagState):
        docs = state.get("kept", [])
        rounds = state.get("generation_rounds", 0)
        q = mask_sensitive(state["question"])
        system_context = _context(docs)
        question = q
        if rounds > 0 and state.get("unsupported"):
            question = q + STRICT_SUFFIX.format(claims="; ".join(state["unsupported"])[:600])
        elif rounds > 0:
            question = q + "\n\nCheck every claim against the sources and cite each one like [1]."
        answer = (GENERATE_PROMPT | chat_llm | to_text).invoke({"context": system_context, "history": history_messages(state.get("history")), "question": question}).strip()
        cites = _citations(answer, docs)
        for c in cites:
            c["quote_full"] = docs[c["n"] - 1].page_content
        return {"answer": answer, "citations": cites, "generation_rounds": rounds + 1,
                "trace": [ev("GENERATED", f"attempt {rounds + 1}", f"{len(cites)} sources cited")]}

    # ----- reflection (Self-RAG, prompt-based) -----

    def check_support(state: RagState):
        answer = state.get("answer", "")
        trace = []
        odd = _untraced(state, answer)                       # plain code first: no model has judged anything yet
        figures = bool(re.search(r"(?i)(?:rs\.?|inr|₹)\s*[\d,]", answer))
        if odd:
            trace.append(ev("AMOUNTS", "NOT TRACED", "not found in your records or your question: " + ", ".join(odd)))
            return {"support": "no", "unsupported": [f"{o} is not in the sources" for o in odd], "unverified": odd, "trace": [ev("SUPPORTED", "NO", "a rupee amount could not be traced"), *trace]}
        trace.append(ev("AMOUNTS", "TRACED" if figures else "NONE", "every ₹ figure traced to your records" if figures else "no ₹ figures in the answer"))
        if state.get("route") == "direct":
            return {"support": None, "unverified": [], "trace": [ev("SUPPORTED", "N/A", "a direct reply has no sources"), *trace]}
        context = _context(state.get("kept", [])) if state.get("route") == "retrieve" else (state.get("allowed_text") or "(no tool output)")
        g = graders.check_groundedness(grade_llm, mask_sensitive(state["question"]), answer, context)
        shown = {"fully": "FULLY", "partially": "PARTLY", "no": "NO"}[g["supported"]]
        detail = "; ".join(g["unsupported"])[:300] if g["unsupported"] else (g.get("error") or "every claim is in the sources")
        return {"support": g["supported"], "unsupported": g["unsupported"], "unverified": [], "trace": [ev("SUPPORTED", shown, detail), *trace]}

    def after_support(state: RagState):
        return "reflect_edge" if state.get("support") == "no" else "check_useful"

    def check_useful(state: RagState):
        u = graders.check_usefulness(grade_llm, mask_sensitive(state["question"]), state.get("answer", ""))
        verdict = "UNSURE" if u["useful"] is None else ("YES" if u["useful"] else "NO")
        return {"useful": u["useful"], "trace": [ev("USEFUL", verdict, u.get("error") or (f"score {u['score']} of 5" if u.get("score") else ""))]}

    def reflect_edge(state: RagState):
        supported = state.get("support") in ("fully", None)
        useful = state.get("useful") is not False
        retrieving = state.get("route") == "retrieve" and bool(state.get("kept"))
        if supported and useful:
            return "finalize"
        if not supported:
            return "generate" if retrieving and state.get("generation_rounds", 0) <= config.RAG_MAX_REGENERATE else "finalize"
        return "rewrite_query" if state.get("route") == "retrieve" and state.get("retrieval_rounds", 0) <= config.RAG_MAX_RETRIEVE else "finalize"

    def reflect_hub(state: RagState):          # a no-op node so check_support and check_useful can share one loop edge
        return {}

    # ----- the end -----

    def finalize(state: RagState):
        route, mode = state.get("route", "retrieve"), state["mode"]
        answer, trace = state.get("answer", ""), []
        abstained = bool(state.get("abstained"))
        support, useful, unverified = state.get("support"), state.get("useful"), list(state.get("unverified", []))
        if route == "decline":
            answer, abstained = DECLINE_TEXT, True
        elif route == "retrieve" and not answer:
            answer, abstained = NOT_FOUND_TEXT, True
            trace.append(ev("ANSWER", "NOT FOUND", "nothing relevant was found after searching"))
        else:
            if mode not in REFLECT_MODES or route == "decline":
                unverified = _untraced(state, answer)            # rag / crag: no reflection loop, but the rupee check still runs
            elif unverified:
                answer, abstained = WITHHELD_TEXT, True
                trace.append(ev("ANSWER", "WITHHELD", "a rupee amount could not be traced, so the answer is not shown"))
            elif support in ("partially", "no"):
                claims = state.get("unsupported") or []
                answer += "\n\n_Not fully supported by your sources." + (" Unchecked: " + "; ".join(claims[:3]) + "._" if claims else "_")
                trace.append(ev("ANSWER", "WITH CAVEAT", "the checks ran out of tries before every claim was backed"))
        shown_support = {"fully": "full", "partially": "partial", "no": "none"}.get(support or "", None)
        return {"answer": answer, "support": shown_support, "useful": useful, "unverified": unverified, "abstained": abstained,
                "citations": [{k: v for k, v in c.items() if k != "quote_full"} for c in state.get("citations", [])], "trace": trace}

    g = StateGraph(RagState, input_schema=RagInput, output_schema=RagResult)
    for name, fn in [("decide_retrieval", decide_retrieval), ("decline", decline), ("tools_answer", tools_answer), ("generate_direct", generate_direct),
                     ("multi_query", multi_query_node), ("retrieve", retrieve), ("grade_one", grade_one), ("grade_join", grade_join),
                     ("rewrite_query", rewrite_query), ("web_consent", web_consent), ("web_search", web_search), ("refine", refine),
                     ("generate", generate), ("check_support", check_support), ("check_useful", check_useful), ("reflect_hub", reflect_hub),
                     ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "decide_retrieval")
    g.add_conditional_edges("decide_retrieval", after_route, ["tools_answer", "generate_direct", "decline", "multi_query", "retrieve"])
    g.add_edge("decline", "finalize")
    g.add_edge("tools_answer", "check_support")
    g.add_edge("generate_direct", "check_support")
    g.add_edge("multi_query", "retrieve")
    g.add_conditional_edges("retrieve", fan_out, ["grade_one", "grade_join"])
    g.add_edge("grade_one", "grade_join")
    g.add_conditional_edges("grade_join", after_grade, ["generate", "refine", "rewrite_query", "finalize"])
    g.add_conditional_edges("rewrite_query", after_rewrite, ["web_consent", "retrieve"])
    g.add_conditional_edges("web_consent", after_consent, ["web_search", "retrieve"])
    g.add_edge("web_search", "retrieve")
    g.add_edge("refine", "generate")
    g.add_conditional_edges("generate", lambda s: "check_support" if s["mode"] in REFLECT_MODES else "finalize", ["check_support", "finalize"])
    g.add_conditional_edges("check_support", after_support, {"reflect_edge": "reflect_hub", "check_useful": "check_useful"})
    g.add_edge("check_useful", "reflect_hub")
    g.add_conditional_edges("reflect_hub", reflect_edge, ["generate", "rewrite_query", "finalize"])
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver()).with_config(recursion_limit=RECURSION_LIMIT)
