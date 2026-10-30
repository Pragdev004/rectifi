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
        **classify_fields(t),
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
# Percent fields are plain ints (to_wire would turn a Decimal into a money string). Rows from before this feature have source None.

def classify_fields(t: dict) -> dict:
    """How a transaction got its category. `.get` so a dict from an older query still works."""
    return {
        "category_source": t.get("category_source"), "category_confidence": t.get("category_confidence"),
        "category_reason": t.get("category_reason"), "suggested_category": t.get("suggested_category"),
        "suggested_confidence": t.get("suggested_confidence"),
    }


def classify_job(job: dict) -> dict:
    return {
        "id": job["id"], "batch_id": job["batch_id"], "status": job["status"], "total": job["total"], "done": job["done"],
        "by_source": job["by_source"] or {}, "model": job["model"], "error": job["error"],
        "finished": job["finished_at"] is not None,
    }


def batch_summary(counts: dict) -> dict:
    return {"total": counts["total"], "by_source": counts["by_source"], "miscellaneous": counts["miscellaneous"]}
# ═══ WS-CLASSIFY: end ═══

# ═══ WS-CHARTS: begin ═══
# Saved charts. Images travel as base64 text in the JSON (an <img> tag can't send the X-Rectifi-Request header, so a URL
# would be refused); the page shows them as data: URLs, which the CSP allows. There is no money here: every rupee figure is
# already inside the PNG, written by the chart's own inr() calls and checked by rectifi/charts/checks.py.

def chart_summary(row: dict, *, thumb: str | None = None) -> dict:
    checks = row.get("checks") or {}
    return {
        "id": row["id"], "title": row["title"], "prompt": row["prompt"], "kind": row["kind"], "pinned": bool(row["pinned"]),
        "period_start": row["period_start"], "period_end": row["period_end"], "model": row["model"], "attempts": row["attempts"],
        "created_at": row["created_at"], "refreshed_at": row["refreshed_at"], "ok": bool(checks.get("ok", True)),
        "thumb": thumb,
    }


def chart_detail(row: dict, alt: str, *, b64) -> dict:
    """`b64` turns PNG bytes into text (rectifi.charts.run.b64), passed in so this module stays free of the charts package."""
    checks = row.get("checks") or {}
    return {
        **chart_summary(row),
        "code": row.get("code", ""), "trace": row.get("trace") or [],
        "checks": [{"name": c["name"], "ok": c["ok"], "detail": c["detail"]} for c in checks.get("checks", [])],
        "entries": checks.get("entries", 0), "alt": alt,
        "image_light": b64(bytes(row["image_light"])) if row.get("image_light") else None,
        "image_dark": b64(bytes(row["image_dark"])) if row.get("image_dark") else None,
    }
# ═══ WS-CHARTS: end ═══

# ═══ WS-CHAT: begin ═══
# wire shapes for conversations and messages.
# ═══ WS-CHAT: end ═══

# ═══ WS-RAG: begin ═══
# Wire shapes for knowledge documents, citations and the RAG trace. No money maths: a ₹ figure in an answer is already text.

SECTION_LABELS = {"guide": "GUIDE", "note": "YOUR NOTE", "txn": "YOUR ENTRY", "upload": "YOUR FILE", "web": "WEB", "tool": "YOUR NUMBERS"}


def knowledge_doc(d: dict) -> dict:
    """A library document row. Built-in guides are listed with id None until the first index has stored them."""
    return {"id": d.get("id"), "corpus": d["corpus"], "label": SECTION_LABELS.get(d["corpus"], "FILE"), "title": d["title"], "source": d["source"],
            "chunks": int(d.get("chunks") or 0), "created_at": d.get("created_at"), "builtin": d["corpus"] == "guide",
            "indexed": bool(d.get("chunks"))}


def knowledge_status(status: dict, job: dict, guides_on_disk: int) -> dict:
    return {"embed_model": status["embed_model"], "chunks": status["chunks"], "chunks_total": status["chunks_total"],
            "documents": status["documents"], "transactions": status["transactions"], "other_model_chunks": status["other_model_chunks"],
            "updated_at": status["updated_at"], "guides_on_disk": guides_on_disk, "job": job}


def citation(c: dict) -> dict:
    return {"n": c.get("n"), "source": c.get("source", ""), "title": c.get("title", ""), "chunk": c.get("chunk", 0), "quote": c.get("quote", ""),
            "corpus": c.get("corpus", "guide"), "label": c.get("label") or SECTION_LABELS.get(c.get("corpus", ""), "SOURCE")}


def rag_answer(out: dict) -> dict:
    """RagOutput as the chat window draws it: answer, citations, the 'How this answer was checked' list, and the verdicts."""
    return {"answer": out.get("answer", ""), "citations": [citation(c) for c in out.get("citations", [])],
            "trace": [{"step": e["step"], "verdict": e["verdict"], "detail": e.get("detail", "")} for e in out.get("trace", [])],
            "unverified": list(out.get("unverified", [])), "support": out.get("support"), "useful": out.get("useful"),
            "route": out.get("route"), "mode": out.get("mode"), "abstained": bool(out.get("abstained"))}
# ═══ WS-RAG: end ═══
