"""Layer 5 of the classifier: a small local model sorts what the fixed layers could not.

It runs AFTER the import, in the background (agent/jobs.py), and only on rows that are still pending in the catch-all.
The import itself never waits for it (CLAUDE.md rules 5 and 8: importer.py and tools.py never import this file).

What the model may do:
  - see the direction, the amount, and the payee and narration after privacy.mask_sensitive (no account names, no
    reference numbers, no raw SMS text, never a run of 9 or more digits);
  - pick a category ONLY from the list for that direction (a pydantic Literal built at run time, so a debit cannot be
    filed under Salary), and say how sure it is (0 to 1).
What happens next (thresholds are config.CLASSIFY_APPLY_MIN / CLASSIFY_SUGGEST_MIN, whole percents):
  - at or above APPLY: tools.apply_classifications stores it with source 'llm';
  - between SUGGEST and APPLY: stored only as a suggestion ("Maybe Food & Dining, 62%"), the row stays in Miscellaneous;
  - below SUGGEST, or the model says Miscellaneous: nothing changes.
A correction you make while the job runs wins: the UPDATE only touches rows that are still pending AND still the catch-all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, create_model

from .. import categorize, config, tools
from ..db import connect
from ..privacy import mask_sensitive

SYSTEM = (
    "You sort bank transactions of one person in India into spending categories for a personal expense tracker.\n"
    "Each line below is one transaction: its number, whether money went OUT (debit) or came IN (credit), the amount, the "
    "payee and the bank narration. Some lines also show how the same person labelled similar payments before.\n"
    "Rules:\n"
    "- Choose the category ONLY from this list: {categories}.\n"
    "- If you cannot tell, choose {fallback} with a low confidence. Never invent a category.\n"
    "- A payment to a private person (a name, no shop or service) is {fallback}.\n"
    "- confidence is a number from 0 to 1: how sure you are. why is at most 12 words.\n"
    "- Answer for every number, once each.{format}"
)

# (payee text, category, confidence) used as worked examples, shown only for categories that exist.
EXAMPLES = {
    "debit": [("UPI swiggy order", "Food & Dining", 0.95), ("BESCOM electricity bill", "Bills & Utilities", 0.9),
              ("UPI ramesh kumar", categorize.FALLBACK_EXPENSE, 0.2)],
    "credit": [("NEFT salary acme pvt ltd", "Salary & Allowance", 0.95), ("UPI rahul sharma", categorize.FALLBACK_INCOME, 0.2)],
}

NEIGHBOUR_MIN_COS = 0.5
_LONG_DIGITS = re.compile(r"\d{9,}")


class ModelUnavailable(RuntimeError):
    """The model could not be reached (not a bad answer): stop the job and say so."""


def mask_for_model(text: str | None) -> str:
    """privacy.mask_sensitive, then any digit run of 9 or more that is still inside a word is masked too."""
    masked = mask_sensitive(text or "")
    return _LONG_DIGITS.sub(lambda m: "X" * (len(m[0]) - 4) + m[0][-4:], masked)


def label_schema(names: list[str]) -> type[BaseModel]:
    """BatchLabels whose `category` can only be one of `names`."""
    row = create_model(
        "RowLabel",
        i=(int, Field(description="the transaction number")),
        category=(Literal[tuple(names)], Field(description="one category from the list")),  # type: ignore[valid-type]
        confidence=(float, Field(description="0 to 1")),
        why=(str, Field(default="", description="a few words")),
    )
    return create_model("BatchLabels", labels=(list[row], Field(default_factory=list)))  # type: ignore[valid-type]


def _confidence_percent(value: float) -> int:
    """0.82 -> 82. A model that answers 82 instead of 0.82 is understood too."""
    value = float(value)
    if value > 1:
        value /= 100
    return max(0, min(100, round(value * 100)))


def _row_lines(groups: list[dict], index: categorize.NgramIndex, direction: str) -> str:
    lines = []
    for n, g in enumerate(groups, 1):
        lines.append(f"{n}. {direction} | amount {g['amount']} | payee: {mask_for_model(g['payee'])[:60] or '-'} | "
                     f"narration: {mask_for_model(g['description'])[:120] or '-'}")
        hints = [f'"{near["label"]}" -> {near["category"]}' for near, cos in index.neighbours(g["key"], direction, 5)
                 if cos >= NEIGHBOUR_MIN_COS]
        if hints:
            lines.append("   past labels of similar payments: " + "; ".join(dict.fromkeys(hints)))
    return "\n".join(lines)


def _few_shot(direction: str, names: list[str]):
    from langchain_core.prompts import ChatPromptTemplate, FewShotChatMessagePromptTemplate

    shots = []
    for text, category, conf in EXAMPLES[direction]:
        if category in names:
            answer = json.dumps({"labels": [{"i": 1, "category": category, "confidence": conf, "why": "example"}]})
            shots.append({"input": f"1. {direction} | amount 100.00 | payee: {text} | narration: -", "output": answer})
    example = ChatPromptTemplate.from_messages([("human", "{input}"), ("ai", "{output}")])
    return FewShotChatMessagePromptTemplate(example_prompt=example, examples=shots)


def _prompt(direction: str, names: list[str], format_text: str = ""):
    from langchain_core.prompts import ChatPromptTemplate

    return ChatPromptTemplate.from_messages([("system", SYSTEM), _few_shot(direction, names), ("human", "{rows}")]).partial(
        categories=", ".join(names), fallback=categorize.fallback_name(direction), format=format_text)


def _is_unreachable(exc: BaseException) -> bool:
    name = type(exc).__name__
    return isinstance(exc, (httpx.HTTPError, ConnectionError, TimeoutError, OSError)) or name in (
        "ResponseError", "APIConnectionError", "APITimeoutError", "APIStatusError", "RateLimitError", "AuthenticationError")


def ask_model(llm, direction: str, names: list[str], rows_text: str) -> list[Any]:
    """One model call for one batch. Returns the parsed labels. Raises ModelUnavailable when the model cannot be reached
    and ValueError when it answered but not in the shape asked for."""
    schema = label_schema(names)
    last: BaseException | None = None
    for _ in range(2):                                          # a second try for a dropped connection or a timeout
        try:
            chain = _prompt(direction, names) | llm.with_structured_output(schema, method="json_schema", include_raw=True)
            result = chain.invoke({"rows": rows_text})
            parsed = result.get("parsed") if isinstance(result, dict) else result
            if parsed is not None:
                return list(parsed.labels)
            last = ValueError(str(result.get("parsing_error") or "empty answer") if isinstance(result, dict) else "empty answer")
            break
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last = exc
        except Exception as exc:                                # noqa: BLE001  (sorted into "unreachable" or "bad answer" below)
            if _is_unreachable(exc):
                last = exc
                continue
            last = exc
            break
    if last is not None and _is_unreachable(last):
        raise ModelUnavailable(str(last)[:200])
    return _plain_json_fallback(llm, direction, names, rows_text, schema, last)


def _plain_json_fallback(llm, direction, names, rows_text, schema, first_error):
    """For a model without native structured output: ask for JSON in the prompt and parse it."""
    from langchain_core.output_parsers import PydanticOutputParser

    parser = PydanticOutputParser(pydantic_object=schema)
    hint = ('\nReply with JSON only, like {"labels": [{"i": 1, "category": "Groceries", "confidence": 0.8, "why": "a few words"}]}.')
    chain = _prompt(direction, names, hint) | llm | parser
    try:
        return list(chain.invoke({"rows": rows_text}).labels)
    except Exception as exc:                                    # noqa: BLE001
        if _is_unreachable(exc):
            raise ModelUnavailable(str(exc)[:200]) from exc
        raise ValueError(f"The model's answer did not fit the list: {str(first_error or exc)[:160]}") from exc


def _model_name(llm) -> str:
    for attr in ("model", "model_name"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return getattr(llm, "_llm_type", "model")


def _groups(rows: list[dict]) -> tuple[dict[str, list[dict]], list[int]]:
    """Pending rows grouped by (direction, merchant key): a 300-row statement is about 60 questions."""
    by_direction: dict[str, dict[str, dict]] = {"debit": {}, "credit": {}}
    unkeyed: list[int] = []
    for r in rows:
        key = categorize.merchant_key(r["payee"], r["description"])
        if not key:
            unkeyed.append(r["id"])
            continue
        g = by_direction[r["direction"]].setdefault(
            key, {"key": key, "payee": r["payee"], "description": r["description"], "amount": str(r["amount"]), "ids": []})
        g["ids"].append(r["id"])
    return {d: list(g.values()) for d, g in by_direction.items()}, unkeyed


def classify_pending(batch_id: int | None, *, llm=None, progress: Callable[[int, int], None] | None = None,
                     job_id: int | None = None) -> dict:
    """Sort the pending rows of one import (or every pending row when batch_id is None) with the model.

    Returns {"total", "done", "by_source", "status", "model", "error"} where status is done | no_model | failed | cancelled.
    `llm` is a chat model with `with_structured_output` (tests pass a scripted one); None means the configured 'classify' role.
    `job_id` makes the call keep that classify_jobs row up to date and stop when the import is undone."""
    rows = tools.pending_classification(batch_id)
    out: dict[str, Any] = {"total": len(rows), "done": 0, "by_source": {}, "status": "done", "model": None, "error": None}

    def save(**extra):
        out.update(extra)
        if job_id is not None:
            tools.update_classify_job(job_id, status=out["status"], total=out["total"], done=out["done"],
                                      by_source=out["by_source"], model=out["model"], error=out["error"])
        if progress:
            progress(out["done"], out["total"])

    if not rows:
        save()
        return out
    if llm is None:
        from . import llm as llm_module

        ok, why = llm_module.llm_status()
        if not ok:
            save(status="no_model", error=why)
            return out
        llm = llm_module.get_llm("classify")
    out["model"] = _model_name(llm)

    groups, unkeyed = _groups(rows)
    tools.finish_classification(unkeyed)                       # nothing to read (no payee, no narration): leave them
    out["done"] += len(unkeyed)
    save(status="running")
    with connect() as conn:
        index = categorize.get_index(conn)
    counts = {"llm": 0, "suggested": 0, "fallback": 0}
    size = config.CLASSIFY_BATCH_SIZE
    failures = batches = 0

    for direction in ("debit", "credit"):
        names = tools.category_names_for(direction)
        for start in range(0, len(groups[direction]), size):
            if job_id is not None and (tools.get_classify_job(job_id) or {}).get("status") == "cancelled":
                out["status"] = "cancelled"
                out["by_source"] = {k: v for k, v in counts.items() if v}
                return out
            chunk = groups[direction][start:start + size]
            batches += 1
            try:
                labels = ask_model(llm, direction, names, _row_lines(chunk, index, direction))
            except ModelUnavailable as exc:
                out["by_source"] = {k: v for k, v in counts.items() if v}
                save(status="failed", error=_explain(exc))
                return out
            except ValueError:
                failures += 1                                   # a bad answer: these rows stay pending for the next try
                continue
            seen: dict[int, list] = {}
            for label in labels:
                seen.setdefault(label.i, []).append(label)
            items, finished = [], []
            for n, group in enumerate(chunk, 1):
                answer = seen.get(n, [])
                label = answer[0] if len(answer) == 1 else None      # missing or answered twice: counts as unsure
                percent = _confidence_percent(label.confidence) if label else 0
                pickable = label is not None and label.category not in categorize.FALLBACK_NAMES
                if pickable and percent >= config.CLASSIFY_SUGGEST_MIN:
                    apply = percent >= config.CLASSIFY_APPLY_MIN
                    reason = f"{out['model']}: {label.why.strip()}"[:120] if label.why.strip() else str(out["model"])
                    items.extend({"id": rid, "category": label.category, "confidence": percent, "reason": reason, "apply": apply}
                                 for rid in group["ids"])
                    counts["llm" if apply else "suggested"] += len(group["ids"])
                else:
                    counts["fallback"] += len(group["ids"])
                finished.extend(group["ids"])
            tools.apply_classifications(items)
            tools.finish_classification(finished)
            out["done"] += len(finished)
            out["by_source"] = {k: v for k, v in counts.items() if v}
            save(status="running")

    out["by_source"] = {k: v for k, v in counts.items() if v}
    if batches and failures == batches:
        save(status="failed", error="The model's answers could not be read. Try a different model for sorting (RECTIFI_MODEL_CLASSIFY).")
    else:
        save(status="done")
    return out


def _explain(exc: Exception) -> str:
    from .llm import explain_error

    try:
        return explain_error(exc)
    except Exception:                                           # noqa: BLE001
        return "The model could not be reached."
