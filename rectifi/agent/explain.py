"""Optional "Explain this" for one insight: a model writes one or two sentences of advice.

The model never decides a number. It is handed the insight's title, detail and numbers (account and card numbers
masked), and its reply is checked: every rupee figure in it must be one of the insight's own numbers. If it wrote any
other figure, the whole reply is dropped and the page says so. If no model is available the Insights page still works.
"""

from __future__ import annotations

from decimal import Decimal

from langchain_core.messages import HumanMessage, SystemMessage

from ..insights import Insight, is_money_key
from ..money import fmt_inr, parse_amount
from ..parsing import AMOUNT
from ..privacy import mask_sensitive
from .llm import explain_error, get_llm

SYSTEM = (
    "You are a calm money coach for a student in India. Below is ONE finding from the student's own expense tracker. "
    "Write one or two short sentences of practical advice about it, in plain words. "
    "Use only the rupee figures given. Never calculate, estimate, round or invent any other rupee amount. "
    "No lists, no headings, no greetings."
)


def prompt_for(insight: Insight) -> str:
    """What the model is shown. Money values are written as rupees, counts and percentages as plain numbers."""
    lines = [f"Finding: {insight.title}", f"Detail: {insight.detail}", "Numbers:"]
    for key, value in insight.numbers.items():
        shown = fmt_inr(value, paise=False) if is_money_key(key) else str(value.normalize())
        lines.append(f"- {key}: {shown}")
    return mask_sensitive("\n".join(lines))


def figures_in(text: str) -> list[Decimal]:
    """Every ₹ / Rs. / INR figure written in the text, in order, without repeats."""
    found: list[Decimal] = []
    for raw in AMOUNT.findall(text or ""):
        try:
            value = parse_amount(raw)
        except ValueError:
            continue
        if value not in found:
            found.append(value)
    return found


def not_from_numbers(text: str, insight: Insight) -> list[Decimal]:
    """The rupee figures in `text` that are not among the insight's money numbers."""
    allowed = {v for k, v in insight.numbers.items() if is_money_key(k)}
    return [v for v in figures_in(text) if v not in allowed]


def explain(insight: Insight, llm=None) -> dict:
    """{'text': str | None, 'dropped': [figures], 'error': str | None}. Exactly one of text / dropped / error is meaningful."""
    try:
        model = llm or get_llm()
        reply = model.invoke([SystemMessage(SYSTEM), HumanMessage(prompt_for(insight))])
    except Exception as exc:                          # a model that is off or out of credit must not break the page
        return {"text": None, "dropped": [], "error": explain_error(exc)}
    content = reply.content
    text = content if isinstance(content, str) else " ".join(
        part if isinstance(part, str) else str(part.get("text", "")) for part in content)
    text = " ".join(text.split())
    if not text:
        return {"text": None, "dropped": [], "error": "The assistant gave no answer."}
    odd = not_from_numbers(text, insight)
    if odd:
        return {"text": None, "dropped": odd, "error": None}
    return {"text": text, "dropped": [], "error": None}
