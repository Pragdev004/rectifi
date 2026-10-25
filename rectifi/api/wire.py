"""What crosses the wire. Money is a string with exactly 2 decimals ("1250.50", "-12.00", ASCII minus), a percentage is a
string with 1 decimal or null, dates are "YYYY-MM-DD". The browser only ever formats these strings; it never adds them up.

Quantizing here is formatting, not arithmetic: tools.py already worked every figure out in Decimal. (A SQL
COALESCE(SUM(...), 0) comes back as Decimal('0'), which would print as "0" and not "0.00".)
"""

from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal

from ..categorize import FALLBACK_EXPENSE
from ..insights import DATA_QUALITY_KINDS, Insight, is_money_key
from ..money import PAISE

ONE_DECIMAL = Decimal("0.1")


def money(value) -> str | None:
    if value is None:
        return None
    d = Decimal(value).quantize(PAISE)
    return "0.00" if d == 0 else format(d, "f")


def pct(value) -> str | None:
    if value is None:
        return None
    d = Decimal(value).quantize(ONE_DECIMAL)
    return "0.0" if d == 0 else format(d, "f")


def month_text(value: date | None) -> str | None:
    return value.strftime("%Y-%m") if value else None


def to_wire(obj):
    """Plain data for JSON. Any Decimal still in the structure is money (percentages are turned into text by pct() first)."""
    if isinstance(obj, Decimal):
        return money(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_wire(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_wire(v) for v in obj]
    return obj


# ───────────────────────── ledger rows ─────────────────────────

def txn(t: dict) -> dict:
    """A transaction as the lists show it. upi_ref and raw_text stay on the laptop."""
    return {
        "id": t["id"], "date": t["txn_date"], "amount": t["amount"], "direction": t["direction"], "payee": t["payee"],
        "description": t["description"], "note": t["note"], "source": t["source"], "account_id": t["account_id"],
        "account": t["account"], "category_id": t["category_id"], "category": t["category"], "category_kind": t["category_kind"],
    }


# ───────────────────────── summaries and budgets ─────────────────────────

def summary(s: dict) -> dict:
    return {
        "income": s["income"], "spending": s["spending"], "saved": s["saved"], "savings_rate": pct(s["savings_rate"]),
        "count": s["count"],
        "by_category": [{"category": c["category"], "amount": c["amount"], "count": c["count"], "share": pct(c["share"])}
                        for c in s["by_category"]],
    }


def budget_status(s: dict, today: date) -> dict:
    """pace_day is which day of the month the bar's tick stands on (0 before the month starts, the last day after it ends)."""
    first = s["month"]
    days_in_month = calendar.monthrange(first.year, first.month)[1]
    last = first.replace(day=days_in_month)
    pace_day = 0 if today < first else (days_in_month if today > last else today.day)
    return {
        "month": month_text(first), "pace": pct(s["pace"]), "pace_day": pace_day, "days_in_month": days_in_month,
        "categories": [
            {"category": b["category"], "limit": b["limit"], "spent": b["spent"], "remaining": b["remaining"],
             "percent": pct(b["percent"]), "state": b["state"]}
            for b in s["categories"]
        ],
        "total_limit": s["total_limit"], "total_spent": s["total_spent"],
    }


def pace(p: dict) -> dict:
    return {"through": p["through"], "total_limit": p["total_limit"],
            "days": [{"day": d["day"], "spent": d["spent"], "cumulative": d["cumulative"], "pace": d["pace"]} for d in p["days"]]}


def recon(r: dict | None) -> dict | None:
    if r is None:
        return None
    return {
        "account_id": r["account_id"], "opening_date": r["opening_date"], "opening": r["opening"], "credits": r["credits"],
        "debits": r["debits"], "count": r["count"], "expected_closing": r["expected_closing"], "closing_date": r["closing_date"],
        "closing": r["closing"], "gap": r["gap"], "balanced": r["balanced"], "explanation": r["explanation"],
    }


# ───────────────────────── insights ─────────────────────────

def insight_link(i: Insight) -> dict | None:
    """Where the insight's button goes. Built from the kind and key, so the browser never parses prose."""
    key = i.key
    if i.kind == "unlinked":
        return {"page": "ledger", "params": {"account": "none", "period": "all"}}
    if i.kind == "other_pile":
        return {"page": "ledger", "params": {"category": FALLBACK_EXPENSE, "period": "all"}}
    if key.startswith(("leak:", "spike:")):
        return {"page": "ledger", "params": {"category": key.split(":", 1)[1]}}
    if i.action_page:
        return {"page": i.action_page, "params": {}}
    return None


def _insight_number(key: str, value: Decimal) -> str:
    return money(value) if is_money_key(key) else format(value.normalize(), "f")


def insight(i: Insight) -> dict:
    return {
        "key": i.key, "kind": i.kind, "severity": i.severity, "title": i.title, "detail": i.detail,
        "numbers": {k: _insight_number(k, v) for k, v in i.numbers.items()},
        "action_label": i.action_label, "action_page": i.action_page,
        "is_data_quality": i.kind in DATA_QUALITY_KINDS,
        "can_apply": bool(i.apply and i.apply.get("tool") == "set_budget"),
        "link": insight_link(i),
    }


# ───────────────────────── weekly review ─────────────────────────

def review(r: dict) -> dict:
    return {
        "week_start": r["week_start"], "week_end": r["week_end"], "through": r["through"], "days_elapsed": r["days_elapsed"],
        "is_current": r["is_current"], "count": r["count"], "income": r["income"], "spending": r["spending"], "saved": r["saved"],
        "savings_rate": pct(r["savings_rate"]), "prev_start": r["prev_start"], "prev_end": r["prev_end"],
        "prev_spending": r["prev_spending"], "spending_change": r["spending_change"],
        "spending_change_pct": pct(r["spending_change_pct"]), "trend": r["trend"],
        "top_categories": [{"category": c["category"], "amount": c["amount"], "count": c["count"], "share": pct(c["share"])}
                           for c in r["top_categories"]],
        "biggest_debits": [txn(t) for t in r["biggest_debits"]],
        "uncategorised_count": r["uncategorised_count"], "unlinked_count": r["unlinked_count"],
        "account_count": r["account_count"], "all_clear": r["all_clear"], "budget_month": month_text(r["budget_month"]),
        "budgets_needing_attention": [
            {"category": b["category"], "limit": b["limit"], "spent": b["spent"], "percent": pct(b["percent"]), "state": b["state"]}
            for b in r["budgets_needing_attention"]
        ],
        "accounts_needing_attention": [{"id": a["id"], "name": a["name"], "reasons": a["reasons"]}
                                       for a in r["accounts_needing_attention"]],
        "last_commitment": r["last_commitment"], "commitment": r["commitment"],
    }


# ───────────────────────── next phase: one block per workstream ─────────────────────────

# ═══ WS-CLASSIFY: begin ═══
# wire shapes for classification: txn category_source / confidence / suggestion, classify job status.
# ═══ WS-CLASSIFY: end ═══

# ═══ WS-CHARTS: begin ═══
# wire shapes for saved charts (images as URLs or base64, never money maths).
# ═══ WS-CHARTS: end ═══

# ═══ WS-CHAT: begin ═══
# Conversations, memories and trace rows. Timestamps are ISO date-times with an offset (to_wire does that); no money here.

def conversation(row: dict) -> dict:
    return {"thread_id": row["thread_id"], "title": row["title"], "preview": row["preview"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def memory_item(item: dict) -> dict:
    return {"id": item["id"], "kind": item["kind"], "text": item["text"], "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"), "thread_id": item.get("thread_id")}


def learned_rule(row: dict) -> dict:
    return {"id": row["id"], "pattern": row["pattern"], "category": row["category"], "created_at": row["created_at"]}
# ═══ WS-CHAT: end ═══

# ═══ WS-RAG: begin ═══
# wire shapes for knowledge documents, citations and the RAG trace.
# ═══ WS-RAG: end ═══
