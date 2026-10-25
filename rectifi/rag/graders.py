"""The graders: small prompted checks that judge retrieval and answers.

HONEST NOTE ON SELF-RAG. Real Self-RAG (Asai et al., 2023) trains one model to write special "reflection tokens" while it
generates: [Retrieve], [IsRel], [IsSup], [IsUse]. It also ranks several candidate answers in parallel by those tokens.
Rectifi does NOT do that. It has no fine-tuned model. It asks a normal small model (Ollama, role "grade") to answer a short
structured question after the fact:

    ISREL  -> DocGrade       Pydantic model            "is this passage relevant?"
    ISSUP  -> GroundCheck    TypedDict + Annotated     "is every claim in the answer backed by the passages?"
    ISUSE  -> UseCheck       JSON-schema dict          "does the answer help with the question?"
    Retrieve? -> RouteDecision  JSON-schema dict       "look it up, call a tool, answer directly, or decline?"

One candidate per round, no token-level scoring. Small models are noisy graders, so the ₹ check in graph.py (plain code, no
model) always runs FIRST, and the evals (`python -m rectifi.evals.rag`) measure how noisy the graders are.
CRAG (Yan et al., 2024) is the same: its trained retrieval evaluator is replaced by DocGrade.

Every grader fails safe: if the model errors or returns junk, the answer is "unsure" (partial / partially), never a silent pass.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

# ───────────── schemas ─────────────


class DocGrade(BaseModel):
    """Is one retrieved passage useful for the question? (CRAG's evaluator, Self-RAG's ISREL.)"""

    relevance: Literal["yes", "partial", "no"] = Field(description="yes: it answers the question. partial: related but incomplete. no: unrelated.")
    reason: str = Field(default="", description="one short sentence")


class GroundCheck(TypedDict):
    """Is every claim in the answer backed by the passages? (Self-RAG's ISSUP.)"""

    supported: Annotated[Literal["fully", "partially", "no"], ..., "fully: every claim is in the passages. partially: some are not. no: mostly not."]
    unsupported_claims: Annotated[list[str], ..., "short quotes of the claims the passages do not back (empty when fully supported)"]


USE_CHECK_SCHEMA = {
    "title": "UseCheck",
    "description": "Does the answer actually help the person with their question? (Self-RAG's ISUSE.)",
    "type": "object",
    "properties": {
        "useful": {"type": "boolean", "description": "true if the answer addresses the question"},
        "score": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1 useless, 5 very helpful"},
    },
    "required": ["useful", "score"],
}

ROUTE_SCHEMA = {
    "title": "RouteDecision",
    "description": "How should this question be answered? (Self-RAG's Retrieve? decision.)",
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["tools", "retrieve", "direct", "decline"],
                  "description": "tools: needs the person's own numbers. retrieve: a money question the guides or notes can answer. "
                                 "direct: small talk or a simple question that needs no lookup. decline: not about money or Rectifi."},
        "reason": {"type": "string", "description": "one short sentence"},
    },
    "required": ["route"],
}

ROUTES = ("tools", "retrieve", "direct", "decline")

# ───────────── prompts ─────────────

DOC_GRADE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You grade whether one passage helps answer a question. Answer yes only if the passage contains the answer or a key part "
               "of it. Answer partial if it is on the topic but does not answer. Answer no if it is about something else."),
    ("human", "Question: {question}\n\nPassage:\n{passage}"),
])
GROUND_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You check an answer against its sources. List every claim in the answer that the sources do not say. "
               "Claims in the answer that cite a source number must match that source. Rupee amounts count as claims."),
    ("human", "Question: {question}\n\nSources:\n{context}\n\nAnswer:\n{answer}"),
])
USE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You judge whether an answer helps. useful is true when the answer addresses what was asked, even if short. "
               "An answer that only says it cannot help is not useful."),
    ("human", "Question: {question}\n\nAnswer:\n{answer}"),
])
ROUTE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You decide how to answer a question in a personal money app for a student in India. Choose one route. "
               "tools: the question needs the person's own spending, budget, income or entries. "
               "retrieve: general money knowledge (budgeting, UPI safety, SIPs, credit cards, tax basics, rent) or how Rectifi works. "
               "direct: a greeting or thanks. decline: anything not about money or Rectifi."),
    ("human", "{question}"),
])

# ───────────── deterministic pre-router ─────────────

_PERSONAL = re.compile(r"(?i)\b(my|mine|i|i've|i'm|me|did i|have i)\b")
_DATA = re.compile(r"(?i)\b(spen[dt]|spending|paid|pay|paying|payments?|earn(?:ed)?|income|saved|savings?|budgets?|transactions?|expenses?|balance|salary|"
                   r"bought|purchases?|entries|insights?|commitment|last (?:week|month)|this (?:week|month))\b")
_GENERAL = re.compile(r"(?i)\b(how (?:do|does|should|can|to|would)|what is|what's|what are|explain|why|tips?|guide|difference between|meaning of|should i)\b")
_SMALL_TALK = re.compile(r"(?i)^\s*(hi|hello|hey|thanks|thank you|ok|okay|cool|great|bye)\b[\s!.?]*$")


def is_personal(question: str) -> bool:
    """A question about the person's own numbers: 'how much did I spend on food last month'. Deterministic."""
    return bool(_PERSONAL.search(question) and _DATA.search(question) and not _GENERAL.search(question))


def pre_route(question: str) -> str | None:
    """Rules first, model second. Returns a route when the question is obvious, else None (ask the model)."""
    q = (question or "").strip()
    if not q:
        return "decline"
    if _SMALL_TALK.match(q):
        return "direct"
    if is_personal(q):
        return "tools"
    return None


# ───────────── running the graders (all fail safe) ─────────────

def _field(result, name, default=None):
    if result is None:
        return default
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def grade_document(llm, question: str, passage: str) -> dict:
    try:
        result = (DOC_GRADE_PROMPT | llm.with_structured_output(DocGrade)).invoke({"question": question, "passage": passage})
        grade = str(_field(result, "relevance", "partial")).lower()
        return {"grade": grade if grade in ("yes", "partial", "no") else "partial", "reason": str(_field(result, "reason", "") or "")}
    except Exception as exc:  # a broken grader must not hide a good passage
        return {"grade": "partial", "reason": f"grader unavailable ({type(exc).__name__})"}


def check_groundedness(llm, question: str, answer: str, context: str) -> dict:
    try:
        result = (GROUND_PROMPT | llm.with_structured_output(GroundCheck)).invoke({"question": question, "answer": answer, "context": context})
        verdict = str(_field(result, "supported", "partially")).lower()
        claims = [str(c) for c in (_field(result, "unsupported_claims", []) or []) if str(c).strip()]
        if verdict not in ("fully", "partially", "no"):
            verdict = "partially"
        if verdict == "fully" and claims:
            verdict = "partially"            # the grader contradicted itself: listing an unsupported claim means not fully supported
        return {"supported": verdict, "unsupported": claims}
    except Exception as exc:
        return {"supported": "partially", "unsupported": [], "error": f"grader unavailable ({type(exc).__name__})"}


def check_usefulness(llm, question: str, answer: str) -> dict:
    try:
        result = (USE_PROMPT | llm.with_structured_output(USE_CHECK_SCHEMA)).invoke({"question": question, "answer": answer})
        score = _field(result, "score", None)
        useful = _field(result, "useful", None)
        if not isinstance(useful, bool):
            return {"useful": None, "score": None, "error": "grader gave no verdict"}
        return {"useful": useful, "score": score if isinstance(score, int) else None}
    except Exception as exc:
        return {"useful": None, "score": None, "error": f"grader unavailable ({type(exc).__name__})"}


def decide_route(llm, question: str) -> dict:
    """The Retrieve? decision. Rules first (pre_route); the model decides only the unclear ones; on any trouble: retrieve."""
    rule = pre_route(question)
    if rule:
        return {"route": rule, "reason": "decided by a fixed rule", "by": "rule"}
    try:
        result = (ROUTE_PROMPT | llm.with_structured_output(ROUTE_SCHEMA)).invoke({"question": question})
        route = str(_field(result, "route", "retrieve")).lower()
        if route not in ROUTES:
            route = "retrieve"
        return {"route": route, "reason": str(_field(result, "reason", "") or ""), "by": "model"}
    except Exception as exc:
        return {"route": "retrieve", "reason": f"router unavailable ({type(exc).__name__}), so I looked it up", "by": "fallback"}
