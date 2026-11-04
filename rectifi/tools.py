"""The deterministic core. Every number Rectifi shows you is computed here, in SQL or Decimal.

The LLM agent, the Streamlit UI and the MCP server all call these same functions.
Nothing in this file talks to an LLM.
"""

# Multi-user plan: this file is WS-CORE's, except the block between the WS-AI begin and end markers further down.

from __future__ import annotations

import calendar
import hashlib
import hmac
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import categorize, config, merchant_allowlist, userctx, vault
from .categorize import FALLBACK_EXPENSE, FALLBACK_INCOME, fallback_name, merchant_key
from .db import connect, connect_system
from .money import D0, PAISE, fmt_inr, parse_amount
from .parsing import best_effort_sms, classify_sms, parse_sms


class RectifiError(ValueError):
    """A user-facing error: the message is safe to show in the UI or return to the agent."""


TXN_SELECT = """
SELECT t.id, t.txn_date, t.amount, t.direction, t.payee, t.description, t.note, t.source,
       COALESCE(t.upi_ref_sealed, CASE WHEN left(t.upi_ref, 3) = 'b1:' THEN NULL ELSE t.upi_ref END) AS upi_ref,
       t.account_id, a.name AS account, t.category_id, c.name AS category, c.kind AS category_kind,
       t.category_source, t.category_confidence, t.category_reason, t.classify_state,
       t.suggested_category_id, sc.name AS suggested_category, t.suggested_confidence
FROM transactions t
LEFT JOIN categories c  ON c.id  = t.category_id
LEFT JOIN categories sc ON sc.id = t.suggested_category_id
LEFT JOIN accounts   a  ON a.id  = t.account_id
"""

DIRECTIONS = {"debit", "credit"}
LEARN_MIN_KEY = 3           # a correction teaches a rule only when the merchant key is at least this long


# ─────────────────────────── dates ───────────────────────────

def today() -> date:
    return date.today()


def resolve_date(value: str | date | None) -> date:
    """'today', 'yesterday', '2026-10-21', '21-10-2026', '21/10/26' or a date -> date. Rejects the future."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "today", "now")):
        return today()
    if isinstance(value, datetime):
        resolved = value.date()
    elif isinstance(value, date):
        resolved = value
    else:
        text = value.strip().lower()
        if text == "yesterday":
            return today() - timedelta(days=1)
        resolved = None
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y", "%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%d %B %Y"):
            try:
                resolved = datetime.strptime(value.strip(), fmt).date()
                break
            except ValueError:
                continue
        if resolved is None:
            raise RectifiError(f"I couldn't read the date {value!r}. Use YYYY-MM-DD, 'today' or 'yesterday'.")
    if resolved > today():
        raise RectifiError(f"{resolved.isoformat()} is in the future — Rectifi only records money that already moved.")
    return resolved


def parse_month(value: str | date | None) -> date:
    """'2026-10', 'this_month', 'last_month', a date, or None -> first day of that month."""
    if value is None:
        d = today()
    elif isinstance(value, date):
        d = value
    else:
        text = value.strip().lower().replace(" ", "_")
        if text in ("", "this_month", "current", "now"):
            d = today()
        elif text in ("last_month", "previous_month"):
            d = today().replace(day=1) - timedelta(days=1)
        else:
            match = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
            if not match:
                raise RectifiError(f"I couldn't read the month {value!r}. Use YYYY-MM, 'this_month' or 'last_month'.")
            year, month = int(match.group(1)), int(match.group(2))
            if not 1 <= month <= 12:
                raise RectifiError(f"{value!r} isn't a real month.")
            d = date(year, month, 1)
    return d.replace(day=1)


def month_bounds(month: str | date | None) -> tuple[date, date]:
    first = parse_month(month)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


# ─────────────────────────── lookups ───────────────────────────

def list_categories(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        if kind:
            return conn.execute("SELECT id, name, kind FROM categories WHERE kind = %s ORDER BY name", (kind,)).fetchall()
        return conn.execute("SELECT id, name, kind FROM categories ORDER BY kind, name").fetchall()


def _category_id(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM categories WHERE lower(name) = lower(%s)", (name.strip(),)).fetchone()
    if row:
        return row["id"]
    # forgiving prefix match: "food" -> "Food & Dining"
    rows = conn.execute("SELECT id, name FROM categories WHERE name ILIKE %s", (name.strip() + "%",)).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    names = [r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY name").fetchall()]
    raise RectifiError(f"Unknown category {name!r}. Pick one of: {', '.join(names)}.")


def _check_kind(conn, category_id: int, direction: str) -> None:
    """Money out needs an expense (or transfer) category; money in needs an income (or transfer) one.
    Otherwise the row would silently vanish from every summary."""
    row = conn.execute("SELECT name, kind FROM categories WHERE id = %s", (category_id,)).fetchone()
    allowed = {"debit": ("expense", "transfer"), "credit": ("income", "transfer")}[direction]
    if row["kind"] not in allowed:
        way = "going out" if direction == "debit" else "coming in"
        wanted = "an expense" if direction == "debit" else "an income"
        raise RectifiError(
            f"{row['name']!r} is {'a' if row['kind'] == 'transfer' else 'an'} {row['kind']} category, "
            f"but this money is {way}. Pick {wanted} category (or Transfers)."
        )


def can_read() -> bool:
    """False while a person's data key is not here (an SMS that arrived signed out): their learned rules and past payments are
    sealed, so only the built-in rules and word heuristics can run. They run again, in full, at the next sign-in (hooks.py)."""
    return not config.multiuser() or userctx.current().key is not None


def _categorize_detail(conn, payee: str | None, description: str | None, direction: str,
                       ctx: "categorize.Context | None" = None) -> tuple[int, categorize.Decision]:
    """The fixed layers (learned rule, built-in rule, heuristic, look-alike past payment, else the catch-all).
    Returns (category id, why). Pass a `ctx` from categorize.load_context(conn) to reuse it across many rows."""
    ctx = ctx or categorize.load_context(conn, with_index=can_read())
    decision = categorize.decide_with(ctx, payee, description, direction)
    return ctx.category_ids[decision.category], decision


def _categorize(conn, payee: str | None, description: str | None, direction: str) -> int:
    """The category id for a new payment. See _categorize_detail for the reason too."""
    return _categorize_detail(conn, payee, description, direction)[0]


def _suggestion_id(ctx_or_conn, decision: categorize.Decision) -> int | None:
    if not decision.suggested:
        return None
    row = ctx_or_conn.execute("SELECT id FROM categories WHERE name = %s", (decision.suggested,)).fetchone()
    return row["id"] if row else None


def _account_id(conn, account: str | int | None) -> int | None:
    if account in (None, ""):
        return None
    if isinstance(account, int):
        return account
    text = str(account).strip()
    row = conn.execute(
        "SELECT id FROM accounts WHERE lower(name) = lower(%s) OR last4 = %s OR right(last4, 4) = right(%s, 4)",
        (text, text, text),
    ).fetchone()
    if row:
        return row["id"]
    raise RectifiError(f"Unknown account {account!r}. Add it on the Accounts page first.")


def only_account_id() -> int | None:
    """The id of the one account, if exactly one exists. Otherwise None. Used to attach agent entries by default."""
    accounts = list_accounts()
    return accounts[0]["id"] if len(accounts) == 1 else None


def default_account() -> dict | None:
    """The account a manual form should preselect: the only one, else the one used most recently, else the first.

    Returns None only when there are no accounts at all.
    """
    accounts = list_accounts()
    if not accounts:
        return None
    if len(accounts) == 1:
        return accounts[0]
    with connect() as conn:
        row = conn.execute(
            "SELECT account_id FROM transactions WHERE account_id IS NOT NULL ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    if row:
        for a in accounts:
            if a["id"] == row["account_id"]:
                return a
    return accounts[0]


def account_for_statement(last4: str | None) -> dict:
    """Which account should a statement go into? Returns {'account': row or None, 'by_digits': bool, 'reason': str or None}.

    - the statement's letterhead gave account digits and an account has them  -> that account (by_digits True)
    - digits given, nobody has them, but the only account has no digits saved -> that account (nothing contradicts it)
    - digits given and they match no account -> no account, with a reason: importing one bank's rows into another
      bank's account would make the balance check lie, so the person must choose
    - no digits in the statement -> default_account() (the only one, else the most recently used)
    """
    accounts = list_accounts()
    if not accounts:
        return {"account": None, "by_digits": False, "reason": "There is no account yet. Add one on the Accounts page first."}
    if last4:
        for a in accounts:
            if a["last4"] and a["last4"][-4:] == last4:
                return {"account": a, "by_digits": True, "reason": None}
        if len(accounts) == 1 and not accounts[0]["last4"]:
            return {"account": accounts[0], "by_digits": False, "reason": None}
        return {"account": None, "by_digits": False,
                "reason": f"This statement is for an account ending {last4}, but none of your accounts has those last 4 digits. "
                          "Pick the account yourself, or add it (with its last 4 digits) on the Accounts page."}
    return {"account": default_account(), "by_digits": False, "reason": None}


def get_transaction(txn_id: int, conn=None) -> dict | None:
    if conn is None:
        with connect() as c:
            return get_transaction(txn_id, c)
    return conn.execute(TXN_SELECT + " WHERE t.id = %s", (txn_id,)).fetchone()


# ─────────────────────────── writes ───────────────────────────

def add_transaction(
    amount,
    direction: str = "debit",
    txn_date: str | date | None = None,
    payee: str | None = None,
    category: str | None = None,
    account: str | int | None = None,
    note: str | None = None,
    source: str = "manual",
    upi_ref: str | None = None,
    raw_text: str | None = None,
    description: str | None = None,
    fingerprint: str | None = None,
    _conn=None,
) -> dict:
    """Insert one transaction. Returns {'status': 'added'|'duplicate', 'transaction': {...}}.

    `_conn` lets a caller (ingest_sms) do this insert in its own database transaction."""
    try:
        value = parse_amount(amount)
    except ValueError as exc:
        raise RectifiError(str(exc)) from exc
    if value <= 0:
        raise RectifiError("Amount must be more than ₹0.")
    direction = (direction or "").strip().lower()
    if direction not in DIRECTIONS:
        raise RectifiError("Direction must be 'debit' (money out) or 'credit' (money in).")
    when = resolve_date(txn_date)
    payee = payee.strip() if payee else None
    upi_ref = upi_ref.strip() if upi_ref else None
    # Dedupe keys are keyed hashes (laptop mode: the text itself). upi_ref works signed out too (ingest salt); fingerprint
    # needs the session key. The plain reference is kept only sealed, in upi_ref_sealed.
    upi_key = vault.blind_ingest(upi_ref)
    fingerprint = vault.blind(fingerprint)
    brand = merchant_allowlist.plain_key(payee, description)       # an allow-listed brand stays plain (D3); None for every other payee
    arrived_locked = config.multiuser() and userctx.current().key is None      # an SMS that came in while the person was signed out

    def insert(conn) -> dict:
        suggested_id = None
        if category:
            category_id = _category_id(conn, category)
            _check_kind(conn, category_id, direction)
            why = categorize.Decision(category, "user", 100, "you chose")
        else:
            category_id, why = _categorize_detail(conn, payee, description, direction)
            suggested_id = _suggestion_id(conn, why)
        account_id = _account_id(conn, account)
        row = conn.execute(
            """INSERT INTO transactions
                 (account_id, amount, direction, txn_date, payee, description, upi_ref, upi_ref_sealed, fingerprint,
                  category_id, source, raw_text, note,
                  category_source, category_confidence, category_reason, suggested_category_id, suggested_confidence,
                  merchant_key, unlocked)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (account_id, value, direction, when, vault.seal(payee), vault.seal(description), upi_key, vault.seal(upi_ref), fingerprint,
             category_id, source, vault.seal(raw_text), vault.seal(note),
             why.source, why.confidence, vault.seal(why.reason), suggested_id, why.suggested_confidence if suggested_id else None,
             brand, not arrived_locked),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                TXN_SELECT + " WHERE (t.upi_ref = %s AND t.direction = %s) OR t.fingerprint = %s LIMIT 1",
                (upi_key, direction, fingerprint),
            ).fetchone()
            return {"status": "duplicate", "transaction": existing}
        return {"status": "added", "transaction": get_transaction(row["id"], conn)}

    if _conn is not None:
        return insert(_conn)
    with connect() as conn:
        return insert(conn)


def set_category(txn_id: int, category: str, remember: bool = False) -> dict:
    """Recategorise a transaction. The row now says the category is yours (source 'user', 100%) and any model suggestion
    is dropped. remember=True teaches Rectifi: future payments that look like this merchant get the same category."""
    with connect() as conn:
        category_id = _category_id(conn, category)
        current = conn.execute("SELECT direction FROM transactions WHERE id = %s", (txn_id,)).fetchone()
        if current is None:
            raise RectifiError(f"No transaction #{txn_id}.")
        _check_kind(conn, category_id, current["direction"])
        row = conn.execute(
            """UPDATE transactions SET category_id = %s, category_source = 'user', category_confidence = 100,
                      category_reason = %s, suggested_category_id = NULL, suggested_confidence = NULL,
                      classify_state = CASE WHEN classify_state = 'pending' THEN 'done' ELSE classify_state END
               WHERE id = %s RETURNING payee, description""",
            (category_id, vault.seal("you chose"), txn_id),
        ).fetchone()
        learned = None
        pattern = merchant_key(row["payee"], row["description"])
        if remember and len(pattern) >= LEARN_MIN_KEY:
            conn.execute(
                """INSERT INTO merchant_rules (pattern, pattern_blind, category_id, source) VALUES (%s, %s, %s, 'learned')
                   ON CONFLICT (user_id, pattern_blind) DO UPDATE SET category_id = EXCLUDED.category_id, source = 'learned'""",
                (vault.seal(pattern), vault.blind(pattern), category_id),
            )
            learned = pattern
        return {"transaction": get_transaction(txn_id, conn), "learned_rule": learned}


def update_note(txn_id: int, note: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE transactions SET note = %s WHERE id = %s", (vault.seal(note or None), txn_id))


def set_account(txn_id: int, account_id: int | None) -> dict:
    """Link a transaction to an account, or unlink it (account_id None). Returns the updated transaction.

    A row that came from a bank statement already belongs to that statement's account, so it can't be moved:
    undo the import instead. Everything else (manual, chat, sms) can be linked after the fact, which is how an
    entry made without an account gets covered by the balance check."""
    with connect() as conn:
        row = conn.execute("SELECT source FROM transactions WHERE id = %s", (txn_id,)).fetchone()
        if row is None:
            raise RectifiError(f"No transaction #{txn_id}.")
        if account_id is not None and conn.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,)).fetchone() is None:
            raise RectifiError(f"No account #{account_id}.")
        if row["source"] == "statement":
            raise RectifiError("This entry came from a bank statement, so it belongs to that statement's account. "
                               "Undo the import to move it.")
        conn.execute("UPDATE transactions SET account_id = %s WHERE id = %s", (account_id, txn_id))
        return get_transaction(txn_id, conn)


def delete_transaction(txn_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM transactions WHERE id = %s RETURNING id", (txn_id,)).fetchone() is not None


# ─────────────────────────── reads ───────────────────────────

SEARCH_WINDOW = 20_000      # how many of the newest rows the ledger search reads (it runs in Python: the text is sealed)
CATEGORY_SOURCES = ("user", "learned", "rule", "heuristic", "retrieval", "llm", "fallback")      # how a category was chosen


def list_transactions(
    start: date | str | None = None,
    end: date | str | None = None,
    category: str | None = None,
    search: str | None = None,
    direction: str | None = None,
    account: str | int | None = None,
    limit: int = 200,
    unlinked: bool = False,
    source: str | None = None,
) -> list[dict]:
    """`source` filters by how the category was chosen: one of CATEGORY_SOURCES, or "earlier" for rows that have no recorded source."""
    clauses, params = [], []
    if source:
        if source == "earlier":
            clauses.append("t.category_source IS NULL")
        elif source in CATEGORY_SOURCES:
            clauses.append("t.category_source = %s")
            params.append(source)
        else:
            raise RectifiError(f"Unknown category source {source!r}. Use one of: {', '.join(CATEGORY_SOURCES)}, earlier.")
    if unlinked:
        clauses.append("t.account_id IS NULL")
    if start:
        clauses.append("t.txn_date >= %s")
        params.append(resolve_date(start) if isinstance(start, str) else start)
    if end:
        clauses.append("t.txn_date <= %s")
        params.append(resolve_date(end) if isinstance(end, str) else end)
    if direction:
        clauses.append("t.direction = %s")
        params.append(direction)
    with connect() as conn:
        if category:
            clauses.append("t.category_id = %s")
            params.append(_category_id(conn, category))
        if account not in (None, ""):
            clauses.append("t.account_id = %s")
            params.append(_account_id(conn, account))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 5000))
        # Payee, description and note are sealed, so the database cannot search them: search runs here, in Python, over the
        # newest SEARCH_WINDOW rows that pass the other filters (decision in the multi-user plan, section 3 point 4).
        params.append(SEARCH_WINDOW if search else limit)
        rows = conn.execute(TXN_SELECT + where + " ORDER BY t.txn_date DESC, t.id DESC LIMIT %s", params).fetchall()
    if search:
        needle = search.casefold()
        rows = [r for r in rows if any(needle in (r[k] or "").casefold() for k in ("payee", "description", "note"))][:limit]
    return rows


def spending_summary(start: date | None = None, end: date | None = None) -> dict:
    """Income, spending, savings and the category breakdown for a date range (default: this month).

    Transfers (moving money between your own accounts) count as neither income nor spending.
    """
    if start is None or end is None:
        start, end = month_bounds(None)
    with connect() as conn:
        totals = conn.execute(
            """SELECT
                 COALESCE(SUM(t.amount) FILTER (WHERE t.direction = 'credit' AND c.kind = 'income'), 0)  AS income,
                 COALESCE(SUM(t.amount) FILTER (WHERE t.direction = 'debit'  AND c.kind = 'expense'), 0) AS spending,
                 COUNT(*) AS count
               FROM transactions t JOIN categories c ON c.id = t.category_id
               WHERE t.txn_date BETWEEN %s AND %s""",
            (start, end),
        ).fetchone()
        by_category = conn.execute(
            """SELECT c.name AS category, SUM(t.amount) AS amount, COUNT(*) AS count
               FROM transactions t JOIN categories c ON c.id = t.category_id
               WHERE t.txn_date BETWEEN %s AND %s AND t.direction = 'debit' AND c.kind = 'expense'
               GROUP BY c.name ORDER BY SUM(t.amount) DESC""",
            (start, end),
        ).fetchall()
    income, spending = Decimal(totals["income"]), Decimal(totals["spending"])
    saved = income - spending
    savings_rate = (saved / income * 100).quantize(Decimal("0.1")) if income > 0 else None
    for row in by_category:
        row["share"] = (row["amount"] / spending * 100).quantize(Decimal("0.1")) if spending > 0 else D0
    return {
        "start": start,
        "end": end,
        "income": income,
        "spending": spending,
        "saved": saved,
        "savings_rate": savings_rate,
        "count": totals["count"],
        "by_category": by_category,
    }


def daily_spending(start: date, end: date) -> list[dict]:
    """One row per day in the range (zero-filled) with that day's spending and the running total."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT d::date AS day,
                      COALESCE(SUM(t.amount) FILTER (WHERE t.direction = 'debit' AND c.kind = 'expense'), 0) AS spent
               FROM generate_series(%s::date, %s::date, interval '1 day') d
               LEFT JOIN transactions t ON t.txn_date = d::date
               LEFT JOIN categories c ON c.id = t.category_id
               GROUP BY d ORDER BY d""",
            (start, end),
        ).fetchall()
    running = D0
    for row in rows:
        running += row["spent"]
        row["cumulative"] = running
    return rows


def spending_pace(month: str | date | None = None) -> dict:
    """Cumulative spending against an even pace through the month's total budget, one row per day so far.

    pace on day n = total budget x n / days in the month, rounded to paise. `through` is the last day that has
    happened (today, or the month's last day for a past month); `days` is empty for a month that hasn't started.
    This is the Overview chart's data, worked out in Decimal here so no screen does its own sums.
    """
    first, last = month_bounds(month)
    total_limit = budget_status(first)["total_limit"]
    through = min(last, today())
    days = []
    if through >= first:
        for row in daily_spending(first, through):
            index = (row["day"] - first).days + 1
            pace = (total_limit * index / last.day).quantize(PAISE, rounding=ROUND_HALF_UP)
            days.append({"day": row["day"], "spent": row["spent"], "cumulative": row["cumulative"], "pace": pace})
    return {"month": first, "through": through, "total_limit": total_limit, "days": days}


# ─────────────────────────── budgets ───────────────────────────

def set_budget(category: str, limit, month: str | date | None = None) -> dict:
    try:
        value = parse_amount(limit)
    except ValueError as exc:
        raise RectifiError(str(exc)) from exc
    if value <= 0:
        raise RectifiError("A budget must be more than ₹0.")
    first = parse_month(month)
    with connect() as conn:
        category_id = _category_id(conn, category)
        kind = conn.execute("SELECT kind FROM categories WHERE id = %s", (category_id,)).fetchone()["kind"]
        if kind != "expense":
            raise RectifiError("Budgets are for spending categories only.")
        conn.execute(
            """INSERT INTO budgets (category_id, month, limit_amount) VALUES (%s, %s, %s)
               ON CONFLICT (user_id, category_id, month) DO UPDATE SET limit_amount = EXCLUDED.limit_amount""",
            (category_id, first, value),
        )
    return {"category": category, "month": first, "limit": value}


def delete_budget(category: str, month: str | date | None = None) -> None:
    first = parse_month(month)
    with connect() as conn:
        conn.execute("DELETE FROM budgets WHERE category_id = %s AND month = %s", (_category_id(conn, category), first))


def copy_budgets(from_month: str | date, to_month: str | date) -> int:
    """Carry last month's budgets forward without overwriting ones already set."""
    with connect() as conn:
        result = conn.execute(
            """INSERT INTO budgets (category_id, month, limit_amount)
               SELECT category_id, %s, limit_amount FROM budgets WHERE month = %s
               ON CONFLICT DO NOTHING""",
            (parse_month(to_month), parse_month(from_month)),
        )
        return result.rowcount


NEAR_LIMIT = Decimal("80")


def budget_status(month: str | date | None = None) -> dict:
    """Per-category: limit, spent, remaining, percent used and a state of ok / near / over.

    'pace' is how far through the month we are, so 60% spent on day 10 of 30 reads as ahead of pace.
    """
    first, last = month_bounds(month)
    with connect() as conn:
        rows = conn.execute(
            """SELECT c.name AS category, b.limit_amount AS limit,
                      COALESCE(SUM(t.amount) FILTER (WHERE t.direction = 'debit'), 0) AS spent
               FROM budgets b
               JOIN categories c ON c.id = b.category_id
               LEFT JOIN transactions t ON t.category_id = b.category_id AND t.txn_date BETWEEN %s AND %s
               WHERE b.month = %s
               GROUP BY c.name, b.limit_amount
               ORDER BY c.name""",
            (first, last, first),
        ).fetchall()
    now = today()
    if now < first:
        pace = D0
    elif now > last:
        pace = Decimal(100)
    else:
        pace = (Decimal(now.day) / Decimal(last.day) * 100).quantize(Decimal("0.1"))
    total_limit = total_spent = D0
    for row in rows:
        row["remaining"] = row["limit"] - row["spent"]
        row["percent"] = (row["spent"] / row["limit"] * 100).quantize(Decimal("0.1"))
        row["state"] = "over" if row["spent"] > row["limit"] else ("near" if row["percent"] >= NEAR_LIMIT else "ok")
        total_limit += row["limit"]
        total_spent += row["spent"]
    return {
        "month": first,
        "pace": pace,
        "categories": rows,
        "total_limit": total_limit,
        "total_spent": total_spent,
    }


# ─────────────────────────── accounts & reconciliation ───────────────────────────

def add_account(name: str, last4: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise RectifiError("Give the account a name, like 'SBI Savings'.")
    last4 = re.sub(r"\D", "", last4 or "")[-4:] or None
    with connect() as conn:
        row = conn.execute(
            """INSERT INTO accounts (name, last4) VALUES (%s, %s)
               ON CONFLICT (user_id, name) DO UPDATE SET last4 = COALESCE(EXCLUDED.last4, accounts.last4)
               RETURNING id, name, last4""",
            (name, last4),
        ).fetchone()
    return row


def list_accounts() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """SELECT a.id, a.name, a.last4,
                      (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id) AS transactions,
                      (SELECT max(as_of) FROM balance_snapshots s WHERE s.account_id = a.id) AS last_snapshot
               FROM accounts a ORDER BY a.name"""
        ).fetchall()


def record_balance(account: str | int, as_of: str | date, balance, source: str = "manual") -> dict:
    try:
        value = parse_amount(balance) if not (isinstance(balance, str) and balance.strip().startswith("-")) else -parse_amount(balance.strip()[1:])
    except ValueError as exc:
        raise RectifiError(str(exc)) from exc
    when = resolve_date(as_of)
    with connect() as conn:
        account_id = _account_id(conn, account)
        if account_id is None:
            raise RectifiError("Pick an account.")
        conn.execute(
            """INSERT INTO balance_snapshots (account_id, as_of, balance, source) VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id, account_id, as_of) DO UPDATE SET balance = EXCLUDED.balance, source = EXCLUDED.source""",
            (account_id, when, value, source),
        )
    return {"account_id": account_id, "as_of": when, "balance": value}


def list_snapshots(account: str | int) -> list[dict]:
    with connect() as conn:
        account_id = _account_id(conn, account)
        return conn.execute(
            "SELECT as_of, balance, source FROM balance_snapshots WHERE account_id = %s ORDER BY as_of", (account_id,)
        ).fetchall()


def reconcile(account: str | int, from_date: date | None = None, to_date: date | None = None) -> dict:
    """Prove completeness with arithmetic.

        opening balance + credits − debits (that Rectifi knows about) = expected closing
        expected closing vs the bank's real closing  →  gap

    A gap of ₹0 means nothing is missing. A non-zero gap means some transaction slipped through.
    """
    with connect() as conn:
        account_id = _account_id(conn, account)
        if account_id is None:
            raise RectifiError("Pick an account to reconcile.")
        opening_sql = "SELECT as_of, balance FROM balance_snapshots WHERE account_id = %s"
        if from_date:
            opening = conn.execute(opening_sql + " AND as_of <= %s ORDER BY as_of DESC LIMIT 1", (account_id, from_date)).fetchone()
        else:
            opening = conn.execute(opening_sql + " ORDER BY as_of ASC LIMIT 1", (account_id,)).fetchone()
        if to_date:
            closing = conn.execute(opening_sql + " AND as_of <= %s ORDER BY as_of DESC LIMIT 1", (account_id, to_date)).fetchone()
        else:
            closing = conn.execute(opening_sql + " ORDER BY as_of DESC LIMIT 1", (account_id,)).fetchone()
        if opening is None or closing is None or opening["as_of"] >= closing["as_of"]:
            raise RectifiError(
                "Reconciliation needs two balances from the bank on different dates — "
                "import a statement with a balance column, or record an opening and closing balance."
            )
        movement = conn.execute(
            """SELECT COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0) AS credits,
                      COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0)  AS debits,
                      COUNT(*) AS count
               FROM transactions WHERE account_id = %s AND txn_date > %s AND txn_date <= %s""",
            (account_id, opening["as_of"], closing["as_of"]),
        ).fetchone()
    expected = opening["balance"] + movement["credits"] - movement["debits"]
    gap = closing["balance"] - expected
    if gap == 0:
        explanation = "Every rupee is accounted for."
    elif gap > 0:
        explanation = "The bank shows more money than Rectifi expects: a credit is missing, or a debit was recorded twice."
    else:
        explanation = "The bank shows less money than Rectifi expects: a debit is missing, or a credit was recorded twice."
    return {
        "account_id": account_id,
        "opening_date": opening["as_of"],
        "opening": opening["balance"],
        "credits": movement["credits"],
        "debits": movement["debits"],
        "count": movement["count"],
        "expected_closing": expected,
        "closing_date": closing["as_of"],
        "closing": closing["balance"],
        "gap": gap,
        "balanced": gap == 0,
        "explanation": explanation,
    }


# ─────────────────────────── statement imports: history and undo ───────────────────────────

def get_import_batch(batch_id: int) -> dict | None:
    """One import: filename, counts, whether it was undone, and how many of its rows still exist."""
    with connect() as conn:
        return conn.execute(
            """SELECT b.id, b.account_id, a.name AS account, b.filename, b.created_at, b.added, b.merged, b.duplicates,
                      b.needs_review, b.undone_at,
                      (SELECT COUNT(*) FROM transactions t WHERE t.import_batch_id = b.id) AS rows_left
               FROM import_batches b LEFT JOIN accounts a ON a.id = b.account_id WHERE b.id = %s""",
            (batch_id,),
        ).fetchone()


def undo_import(batch_id: int) -> dict:
    """Remove exactly the transactions this import ADDED. Returns {'removed', 'snapshots_restored', 'already_undone'}.

    - Rows the import only merged into (your own entries) or found already recorded are not tagged with this batch,
      so they stay as they are.
    - The balance snapshots this import wrote are put back: deleted if the import created them, restored to the old
      value if it replaced one. A snapshot that changed afterwards (you typed a newer balance, or a later import
      replaced it) is left alone.
    - Safe to call twice: the second call does nothing.
    """
    with connect() as conn:
        batch = conn.execute("SELECT * FROM import_batches WHERE id = %s FOR UPDATE", (batch_id,)).fetchone()
        if batch is None:
            raise RectifiError(f"No import #{batch_id}.")
        if batch["undone_at"] is not None:
            return {"removed": 0, "snapshots_restored": 0, "already_undone": True}
        removed = conn.execute("DELETE FROM transactions WHERE import_batch_id = %s", (batch_id,)).rowcount
        restored = 0
        if batch["account_id"] is not None:
            for change in batch["snapshot_changes"] or []:
                current = conn.execute(
                    "SELECT balance, source FROM balance_snapshots WHERE account_id = %s AND as_of = %s",
                    (batch["account_id"], change["as_of"]),
                ).fetchone()
                if current is None or current["source"] != "statement" or current["balance"] != Decimal(change["new_balance"]):
                    continue
                if change["prev_balance"] is None:
                    conn.execute("DELETE FROM balance_snapshots WHERE account_id = %s AND as_of = %s",
                                 (batch["account_id"], change["as_of"]))
                else:
                    conn.execute("UPDATE balance_snapshots SET balance = %s, source = %s WHERE account_id = %s AND as_of = %s",
                                 (Decimal(change["prev_balance"]), change["prev_source"], batch["account_id"], change["as_of"]))
                restored += 1
        conn.execute("UPDATE import_batches SET undone_at = now() WHERE id = %s", (batch_id,))
    return {"removed": removed, "snapshots_restored": restored, "already_undone": False}


# ─────────────────────────── weekly review ───────────────────────────

COMMITMENT_MAX = 280
STALE_BALANCE_DAYS = 14
NO_ACCOUNT_REASON = (
    "No bank account added yet — Rectifi can't prove nothing is missing until you add one and record its balance."
)


def week_bounds(day: str | date | None = None) -> tuple[date, date]:
    """Monday and Sunday of the week that contains `day` (default: today)."""
    d = resolve_date(day)
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def _account_reasons(account: dict, as_of: date) -> list[str]:
    """Why an account needs a look this week. An empty list means it is fine."""
    reasons = []
    if account.get("last_snapshot") is None:
        reasons.append("No bank balance recorded yet, so Rectifi can't prove nothing is missing. Record the balance from your bank.")
        return reasons
    try:
        r = reconcile(account["id"])
    except RectifiError:
        reasons.append("No two bank balances yet, so Rectifi can't prove nothing is missing.")
    else:
        if not r["balanced"]:
            word = "more" if r["gap"] > 0 else "less"
            reasons.append(f"Gap of {fmt_inr(abs(r['gap']))}: the bank shows {word} money than Rectifi expects.")
    last = account.get("last_snapshot")
    if last is not None and (as_of - last).days > STALE_BALANCE_DAYS:
        reasons.append(f"The latest bank balance is from {last.isoformat()}, {(as_of - last).days} days ago. Record a newer one.")
    return reasons


def weekly_review(day: str | date | None = None) -> dict:
    """Everything the Monday-to-Sunday review page shows, computed here so no screen does its own sums.

    The current week isn't over, so spending is compared with the SAME number of days last week
    (Monday to today vs last Monday to last same weekday). A finished week is compared with the whole week before.
    """
    start, end = week_bounds(day)
    now = today()
    through = min(end, now)                       # last day that has really happened
    days_elapsed = (through - start).days + 1
    prev_start = start - timedelta(days=7)
    prev_end = prev_start + timedelta(days=days_elapsed - 1)

    summary = spending_summary(start, end)
    prev_spending = spending_summary(prev_start, prev_end)["spending"]
    change = summary["spending"] - prev_spending
    if prev_spending > 0:
        change_pct = (change / prev_spending * 100).quantize(Decimal("0.1"))
        trend = "up" if change > 0 else "down" if change < 0 else "flat"
    else:
        change_pct = None
        trend = "no_prior" if summary["spending"] > 0 else "flat"

    with connect() as conn:
        biggest = conn.execute(
            TXN_SELECT + """ WHERE t.txn_date BETWEEN %s AND %s AND t.direction = 'debit' AND c.kind = 'expense'
                             ORDER BY t.amount DESC, t.id LIMIT 3""",
            (start, end),
        ).fetchall()
        uncategorised = conn.execute(
            """SELECT COUNT(*) AS n FROM transactions t JOIN categories c ON c.id = t.category_id
               WHERE t.txn_date BETWEEN %s AND %s AND c.name IN (%s, %s)""",
            (start, end, FALLBACK_EXPENSE, FALLBACK_INCOME),
        ).fetchone()["n"]
        unlinked = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE txn_date BETWEEN %s AND %s AND account_id IS NULL",
            (start, end),
        ).fetchone()["n"]
        commitments = {
            row["week_start"]: row["commitment"]
            for row in conn.execute(
                "SELECT week_start, commitment FROM weekly_reviews WHERE week_start IN (%s, %s)", (start, start - timedelta(days=7))
            ).fetchall()
        }

    budgets = budget_status(through)
    needing = [
        {"category": b["category"], "limit": b["limit"], "spent": b["spent"], "percent": b["percent"], "state": b["state"]}
        for b in budgets["categories"] if b["state"] in ("near", "over")
    ]
    accounts = []
    all_accounts = list_accounts()
    if not all_accounts:
        # No account means no balance to check against. "Nothing is missing" can't be proven, so don't say it.
        accounts.append({"id": None, "name": "No bank account", "reasons": [NO_ACCOUNT_REASON]})
    for account in all_accounts:
        reasons = _account_reasons(account, through)
        if reasons:
            accounts.append({"id": account["id"], "name": account["name"], "reasons": reasons})
    all_clear = bool(all_accounts) and not accounts and not needing and not uncategorised and not unlinked

    return {
        "week_start": start,
        "week_end": end,
        "through": through,
        "days_elapsed": days_elapsed,
        "is_current": end >= now,
        "count": summary["count"],
        "income": summary["income"],
        "spending": summary["spending"],
        "saved": summary["saved"],
        "savings_rate": summary["savings_rate"],
        "prev_start": prev_start,
        "prev_end": prev_end,
        "prev_spending": prev_spending,
        "spending_change": change,
        "spending_change_pct": change_pct,
        "trend": trend,
        "top_categories": summary["by_category"][:3],
        "biggest_debits": biggest,
        "uncategorised_count": uncategorised,
        "unlinked_count": unlinked,
        "account_count": len(all_accounts),
        "all_clear": all_clear,
        "budget_month": budgets["month"],
        "budgets_needing_attention": needing,
        "accounts_needing_attention": accounts,
        "last_commitment": commitments.get(start - timedelta(days=7)),
        "commitment": commitments.get(start),
    }


def save_commitment(text: str, day: str | date | None = None) -> dict:
    """Save (or replace) this week's one-line promise. Returns {'week_start', 'commitment'}."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        raise RectifiError("Write one short sentence for your commitment.")
    if len(text) > COMMITMENT_MAX:
        raise RectifiError(f"Keep it to {COMMITMENT_MAX} characters or fewer, one promise is easier to keep.")
    start, _ = week_bounds(day)
    with connect() as conn:
        conn.execute(
            """INSERT INTO weekly_reviews (week_start, commitment) VALUES (%s, %s)
               ON CONFLICT (user_id, week_start) DO UPDATE SET commitment = EXCLUDED.commitment""",
            (start, vault.seal(text)),
        )
    return {"week_start": start, "commitment": text}


def week_anchor(value: str | date | None) -> date:
    """'this_week' (or empty), 'last_week', or any date inside the week you want -> a date in that week."""
    if value is None or (isinstance(value, str) and value.strip().lower().replace(" ", "_") in ("", "this_week", "current", "now")):
        return today()
    if isinstance(value, str) and value.strip().lower().replace(" ", "_") in ("last_week", "previous_week"):
        return today() - timedelta(days=7)
    return resolve_date(value)


def weekly_review_text(review: dict) -> dict:
    """The weekly review as plain strings (rupees as '₹1,234.00', percentages as '12.5%').

    The agent and the MCP server both send this to a model, so every number is already formatted here
    and the model only has to copy it.
    """
    savings_rate = review["savings_rate"]
    return {
        "week": f"{review['week_start']} to {review['week_end']}",
        "counted_through": str(review["through"]),
        "week_is_finished": not review["is_current"],
        "transactions": review["count"],
        "income": fmt_inr(review["income"]),
        "spending": fmt_inr(review["spending"]),
        "saved": fmt_inr(review["saved"]),
        "savings_rate": f"{savings_rate}%" if savings_rate is not None else "no income recorded",
        "compared_with": f"{review['prev_start']} to {review['prev_end']}",
        "previous_spending": fmt_inr(review["prev_spending"]),
        "spending_change": (fmt_inr(review["spending_change"], sign=True) if review["prev_spending"] > 0
                            else "nothing to compare, no spending in the comparison period"),
        "spending_change_percent": f"{review['spending_change_pct']}%" if review["spending_change_pct"] is not None else "no spending in the comparison period",
        "trend": review["trend"],
        "top_categories": [
            {"category": c["category"], "spent": fmt_inr(c["amount"]), "share": f"{c['share']}%"} for c in review["top_categories"]
        ],
        "biggest_debits": [
            f"#{t['id']} {t['txn_date']} {fmt_inr(t['amount'])} {t['payee'] or t['description'] or '—'} [{t['category']}]"
            for t in review["biggest_debits"]
        ],
        "uncategorised_transactions": review["uncategorised_count"],
        "entries_not_linked_to_an_account": review["unlinked_count"],
        "all_clear": review["all_clear"],
        "budget_month": review["budget_month"].strftime("%Y-%m"),
        "budgets_needing_attention": [
            {"category": b["category"], "limit": fmt_inr(b["limit"]), "spent": fmt_inr(b["spent"]),
             "used": f"{b['percent']}%", "state": b["state"]}
            for b in review["budgets_needing_attention"]
        ],
        "accounts_needing_attention": [{"account": a["name"], "reasons": a["reasons"]} for a in review["accounts_needing_attention"]],
        "last_weeks_commitment": review["last_commitment"],
        "this_weeks_commitment": review["commitment"],
    }


# ─────────────────────────── phone SMS capture ───────────────────────────
# A phone forwards bank SMS to the laptop (rectifi/sms_gateway.py). Everything below is fixed rules and SQL:
# no model reads an SMS. A device proves itself with a token; only the token's SHA-256 hash is stored.

TOKEN_PREFIX = "rf_"
DEVICE_NAME_MAX = 60
SMS_TEXT_MAX = 4000


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_device(name: str) -> dict:
    """Pair a phone. Returns {'id', 'name', 'created_at', 'token'}. The plaintext token exists only in this return value:
    the database keeps its hash, so it can never be shown again."""
    name = re.sub(r"\s+", " ", name or "").strip()
    if not name:
        raise RectifiError("Give the phone a name, like 'My Android'.")
    if len(name) > DEVICE_NAME_MAX:
        raise RectifiError(f"Keep the name to {DEVICE_NAME_MAX} characters or fewer.")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO phone_devices (name, token_hash) VALUES (%s, %s) RETURNING id, created_at",
            (vault.seal(name), _hash_token(token)),
        ).fetchone()
    return {**row, "name": name, "token": token}


def list_devices() -> list[dict]:
    """Paired phones, active ones first. Never includes the token or its hash."""
    with connect() as conn:
        return conn.execute(
            """SELECT id, name, created_at, last_seen_at, revoked_at FROM phone_devices
               ORDER BY (revoked_at IS NOT NULL), id"""
        ).fetchall()


def revoke_device(device_id: int) -> bool:
    """Stop accepting SMS from this phone. True if it was active."""
    with connect() as conn:
        return conn.execute(
            "UPDATE phone_devices SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id", (device_id,)
        ).fetchone() is not None


def authenticate_device(token: str | None) -> dict | None:
    """The device this token belongs to, or None (unknown or revoked). A good token also updates last_seen_at.

    The hash of the presented token is compared with every stored hash using hmac.compare_digest, with no early
    exit, so the time taken does not hint at which part matched.

    The lookup runs on a system connection (the token is how we find the person; nobody is signed in yet). A good answer
    carries `ctx`: that person's UserContext with NO data key (strict False) but with the public key and ingest salt, so
    the gateway can seal and dedupe while they are signed out. The caller binds it (userctx.bound) for every write, so a
    token only ever writes as its own owner. `ctx` is never logged."""
    if not token or len(token) > 200:
        return None
    digest = _hash_token(token)
    with connect_system("tools.authenticate_device: find the person a phone token belongs to") as conn:
        match = None
        for row in conn.execute("SELECT id, user_id, name, token_hash, revoked_at FROM phone_devices").fetchall():
            if hmac.compare_digest(row["token_hash"], digest):
                match = row
        if match is None or match["revoked_at"] is not None:
            return None
        person = conn.execute("SELECT public_key, ingest_salt, confirmed_at IS NOT NULL AS confirmed FROM users WHERE id = %s",
                              (match["user_id"],)).fetchone() or {}
    if not person.get("confirmed"):             # an owner claim that is not confirmed yet: no keys, so nothing is written until its
        person = {}                             # old rows have been re-keyed (users.confirm_signup); the phone retries
    ctx = userctx.UserContext(uid=match["user_id"], key=None, public_key=bytes(person["public_key"]) if person.get("public_key") else None,
                              ingest_salt=bytes(person["ingest_salt"]) if person.get("ingest_salt") else None, strict=False)
    with userctx.bound(ctx), connect() as conn:
        conn.execute("UPDATE phone_devices SET last_seen_at = now() WHERE id = %s", (match["id"],))
    name = match["name"] if not vault.is_envelope(match["name"]) else None        # a sealed name stays sealed: the gateway never needs it
    return {"id": match["id"], "name": name, "uid": match["user_id"], "ctx": ctx}


def _sms_digest(sender: str | None, text: str, sent_at: datetime | None, day: date | None = None) -> str:
    """Identifies one SMS. The phone's own timestamp (milliseconds) separates two identical texts;
    without one, the same text on the same day counts as the same SMS (`day` is only for making an old digest again)."""
    stamp = str(int(sent_at.timestamp() * 1000)) if sent_at else f"day:{day or today()}"
    return hashlib.sha256(f"{sender or ''}|{stamp}|{text}".encode("utf-8")).hexdigest()


def _sms_account(parsed) -> int | None:
    """The account an SMS belongs to: the one whose last 4 digits it names; with no digits in the SMS, the only
    account if there is just one. An SMS that names an account Rectifi doesn't know is not attached by guess,
    unless the only account has no last 4 digits saved (then nothing contradicts it)."""
    accounts = list_accounts()
    if parsed.account_last4:
        for a in accounts:
            if a["last4"] and a["last4"][-4:] == parsed.account_last4:
                return a["id"]
        return only_account_id() if len(accounts) == 1 and not accounts[0]["last4"] else None
    return only_account_id()


NOT_A_TRANSACTION_SMS = "That doesn't look like a transaction SMS — no amount, or no debited/credited wording."


def preview_pasted_sms(text: str) -> dict | None:
    """What the Add page's Paste SMS box would save, or None if the text isn't a transaction SMS. Nothing is written.

    The account is matched the same way the phone gateway does it (_sms_account)."""
    parsed = parse_sms(text or "")
    if parsed is None:
        return None
    account_id = _sms_account(parsed)
    account = next((a for a in list_accounts() if a["id"] == account_id), None)
    return {
        "amount": parsed.amount, "direction": parsed.direction, "txn_date": parsed.txn_date, "payee": parsed.payee,
        "upi_ref": parsed.upi_ref, "account_last4": parsed.account_last4,
        "account": {"id": account["id"], "name": account["name"], "last4": account["last4"]} if account else None,
        "warnings": list(parsed.warnings),
    }


def save_pasted_sms(text: str) -> dict:
    """Save the SMS the person pasted. Returns what add_transaction returns. Same rules as preview_pasted_sms."""
    parsed = parse_sms(text or "")
    if parsed is None:
        raise RectifiError(NOT_A_TRANSACTION_SMS)
    return add_transaction(parsed.amount, parsed.direction, parsed.txn_date, payee=parsed.payee,
                           account=_sms_account(parsed), source="sms", upi_ref=parsed.upi_ref, raw_text=text)


def _to_inbox(device_id, sender, text, sent_at, digest, reason) -> dict:
    with connect() as conn:
        row = conn.execute(
            """INSERT INTO sms_inbox (device_id, sender, raw_text, sent_at, reason, text_hash)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, text_hash) DO NOTHING RETURNING id""",
            (device_id, vault.seal(sender), vault.seal(text), sent_at, vault.seal(reason), digest),
        ).fetchone()
    return {"status": "duplicate"} if row is None else {"status": "needs_review", "inbox_id": row["id"], "reason": reason}


def ingest_sms(device_id: int | None, sender: str | None, text: str, sent_at: datetime | None = None) -> dict:
    """One SMS forwarded by a phone -> {'status': 'added' | 'duplicate' | 'ignored' | 'needs_review', ...}.

    added         a clear debit/credit SMS became a transaction (source 'sms', account matched by last 4 digits)
    duplicate     the same SMS (or the same UPI reference) is already here
    ignored       OTP, offer, chat or anything that is not a transaction. Nothing is stored.
    needs_review  it looks like a bank SMS but the fixed rules are not sure. Kept in the inbox for a person.
    """
    text = (text or "").strip()[:SMS_TEXT_MAX]
    sender = (sender or "").strip()[:64] or None
    verdict = classify_sms(text, sender)
    if verdict.kind == "ignored":
        return {"status": "ignored", "reason": verdict.reason}
    if device_id is not None:
        with connect() as conn:                 # the device must be this person's (row level security hides anyone else's)
            if conn.execute("SELECT 1 FROM phone_devices WHERE id = %s", (device_id,)).fetchone() is None:
                raise RectifiError("That phone is not paired to this account.")
    plain_digest = _sms_digest(sender, text, sent_at)
    digest = vault.blind_ingest(plain_digest)
    if verdict.kind == "review":
        return _to_inbox(device_id, sender, text, sent_at, digest, verdict.reason)
    parsed = verdict.parsed
    when = parsed.txn_date or (min(sent_at.date(), today()) if sent_at else today())
    try:
        account_id = _sms_account(parsed)
        with connect() as conn:
            fresh = conn.execute(
                "INSERT INTO sms_seen (text_hash, digest_sealed) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING text_hash",
                (digest, vault.seal(plain_digest))
            ).fetchone()
            if fresh is None:
                return {"status": "duplicate"}
            result = add_transaction(parsed.amount, parsed.direction, when, payee=parsed.payee, account=account_id,
                                     source="sms", upi_ref=parsed.upi_ref, raw_text=text, _conn=conn)
    except RectifiError as exc:             # for example a date in the future: a person should look at it
        return _to_inbox(device_id, sender, text, sent_at, digest, str(exc))
    return {"status": result["status"], "transaction": result["transaction"]}


def list_inbox(status: str = "pending", limit: int = 200) -> list[dict]:
    """SMS waiting for a person ('pending' = Needs review), newest first."""
    with connect() as conn:
        return conn.execute(
            """SELECT i.id, i.sender, i.raw_text, i.sent_at, i.received_at, i.status, i.reason, d.name AS device
               FROM sms_inbox i LEFT JOIN phone_devices d ON d.id = i.device_id
               WHERE i.status = %s ORDER BY i.received_at DESC, i.id DESC LIMIT %s""",
            (status, max(1, min(int(limit), 1000))),
        ).fetchall()


def dismiss_inbox_item(item_id: int) -> bool:
    with connect() as conn:
        return conn.execute(
            "UPDATE sms_inbox SET status = 'dismissed' WHERE id = %s AND status = 'pending' RETURNING id", (item_id,)
        ).fetchone() is not None


def mark_inbox_added(item_id: int) -> bool:
    """Call after the person saved the entry on the Add page."""
    with connect() as conn:
        return conn.execute(
            "UPDATE sms_inbox SET status = 'added' WHERE id = %s AND status = 'pending' RETURNING id", (item_id,)
        ).fetchone() is not None


def inbox_prefill(item_id: int) -> dict:
    """What the Add form should start with for an inbox item. Fixed rules only; a person checks and saves.
    Returns amount, direction, txn_date, payee, upi_ref, account_id (each may be None), plus raw_text and inbox_id."""
    with connect() as conn:
        item = conn.execute("SELECT id, raw_text FROM sms_inbox WHERE id = %s", (item_id,)).fetchone()
    if item is None:
        raise RectifiError(f"No inbox item #{item_id}.")
    if item["raw_text"] is None:
        raise RectifiError("Sign in again to read this message.")
    guess = best_effort_sms(item["raw_text"])
    account_id = None
    if guess["account_last4"]:
        account_id = next((a["id"] for a in list_accounts() if a["last4"] and a["last4"][-4:] == guess["account_last4"]), None)
    elif (only := only_account_id()) is not None:
        account_id = only
    if guess["txn_date"] and guess["txn_date"] > today():
        guess["txn_date"] = None
    return {**guess, "account_id": account_id, "raw_text": item["raw_text"], "inbox_id": item["id"]}


# ═══ WS-AI: begin ═══   (classify, charts, conversations, traces, rag: the owner-only AI part; the rest of this file is WS-CORE)
# ───────────────────────── next phase: one block per workstream ─────────────────────────
# Edit only inside your own block. Nothing in this file talks to an LLM (CLAUDE.md rule 5).

# ═══ WS-CLASSIFY: begin ═══
# Classification bookkeeping. The fixed layers live in categorize.py; the optional model pass is rectifi/agent/classify.py,
# which only calls these functions to read pending rows and to write its answers. Nothing here talks to a model.

RELABELLABLE_SOURCES = ("fallback", "llm", "retrieval", "heuristic")     # "apply to similar" never touches user/learned/rule rows
UNFINISHED_JOB = ("queued", "running", "no_model")
SHOW_FILTERS = ("all", "misc", "model", "suggested")


def category_names_for(direction: str) -> list[str]:
    """Names a payment of this direction may be filed under (a debit never gets an income category)."""
    kinds = categorize.KIND_FOR[direction]
    with connect() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM categories WHERE kind = ANY(%s) ORDER BY name", (list(kinds),)).fetchall()]


def pending_classification(batch_id: int | None = None, limit: int = 5000) -> list[dict]:
    """Rows the fixed layers could not place and the model has not looked at yet: id, direction, amount, payee, description."""
    where, params = "classify_state = 'pending' AND category_source = 'fallback'", []
    if batch_id is not None:
        where += " AND import_batch_id = %s"
        params.append(batch_id)
    with connect() as conn:
        return conn.execute(
            f"SELECT id, direction, amount, payee, description FROM transactions WHERE {where} ORDER BY id LIMIT %s",
            [*params, limit],
        ).fetchall()


def apply_classifications(items: list[dict]) -> dict:
    """Write the model's answers. Each item: {id, category, confidence (0-100), reason, apply: bool}.
    apply=True stores the category (source 'llm'); apply=False stores it only as a suggestion. Either way the row is 'done'.

    The UPDATE touches no money, date, account, reference or fingerprint, and only a row that is still pending AND still
    the catch-all: a correction you made while the job ran (source 'user') wins. Returns {'applied', 'suggested', 'skipped'}."""
    applied = suggested = skipped = 0
    with connect() as conn:
        for item in items:
            row = conn.execute("SELECT direction FROM transactions WHERE id = %s", (item["id"],)).fetchone()
            cat = conn.execute("SELECT id, kind FROM categories WHERE name = %s", (item["category"],)).fetchone()
            if row is None or cat is None or cat["kind"] not in categorize.KIND_FOR[row["direction"]]:
                skipped += 1
                continue
            confidence = max(0, min(100, int(item["confidence"])))
            if item.get("apply"):
                n = conn.execute(
                    """UPDATE transactions SET category_id = %s, category_source = 'llm', category_confidence = %s,
                              category_reason = %s, suggested_category_id = NULL, suggested_confidence = NULL, classify_state = 'done'
                       WHERE id = %s AND classify_state = 'pending' AND category_source = 'fallback'""",
                    (cat["id"], confidence, vault.seal((item.get("reason") or "the local model")[:120]), item["id"]),
                ).rowcount
                applied += n
            else:
                n = conn.execute(
                    """UPDATE transactions SET suggested_category_id = %s, suggested_confidence = %s, classify_state = 'done'
                       WHERE id = %s AND classify_state = 'pending' AND category_source = 'fallback'""",
                    (cat["id"], confidence, item["id"]),
                ).rowcount
                suggested += n
            skipped += 0 if n else 1
    return {"applied": applied, "suggested": suggested, "skipped": skipped}


def finish_classification(ids: list[int]) -> int:
    """The model looked at these rows and was not sure enough: leave them in the catch-all and stop waiting."""
    if not ids:
        return 0
    with connect() as conn:
        return conn.execute(
            "UPDATE transactions SET classify_state = 'done' WHERE id = ANY(%s) AND classify_state = 'pending'", (ids,)
        ).rowcount


def apply_category_to_similar(txn_id: int) -> int:
    """Give every look-alike payment (same merchant key, same direction) the category of this one. Only rows the machine
    guessed (catch-all, model, look-alike, word pattern) change; rows you set, rules you taught and built-in rules never do.
    Returns how many changed."""
    with connect() as conn:
        base = conn.execute(
            "SELECT direction, payee, description, category_id FROM transactions WHERE id = %s", (txn_id,)
        ).fetchone()
        if base is None:
            raise RectifiError(f"No transaction #{txn_id}.")
        key = merchant_key(base["payee"], base["description"])
        if len(key) < LEARN_MIN_KEY:
            return 0
        _check_kind(conn, base["category_id"], base["direction"])
        rows = conn.execute(
            """SELECT id, payee, description FROM transactions
               WHERE id <> %s AND direction = %s AND category_id <> %s AND category_source = ANY(%s)""",
            (txn_id, base["direction"], base["category_id"], list(RELABELLABLE_SOURCES)),
        ).fetchall()
        ids = [r["id"] for r in rows if merchant_key(r["payee"], r["description"]) == key]
        if not ids:
            return 0
        return conn.execute(
            """UPDATE transactions SET category_id = %s, category_source = 'learned', category_confidence = 98,
                      category_reason = %s, suggested_category_id = NULL, suggested_confidence = NULL,
                      classify_state = CASE WHEN classify_state = 'pending' THEN 'done' ELSE classify_state END
               WHERE id = ANY(%s) AND category_source = ANY(%s)""",
            (base["category_id"], vault.seal(f"you taught Rectifi '{key}'"[:120]), ids, list(RELABELLABLE_SOURCES)),
        ).rowcount


def queue_miscellaneous_resort() -> int:
    """"Sort my Miscellaneous entries again": mark every catch-all row that was never decided by you as pending.
    Rows with no recorded source (older than this feature) are stamped 'fallback' so the model pass may touch them.
    Returns how many rows are now waiting."""
    with connect() as conn:
        conn.execute(
            """UPDATE transactions t SET category_source = 'fallback'
               FROM categories c WHERE c.id = t.category_id AND c.name = ANY(%s) AND t.category_source IS NULL""",
            (list(categorize.FALLBACK_NAMES),),
        )
        return conn.execute(
            """UPDATE transactions SET classify_state = 'pending'
               WHERE category_source = 'fallback' AND classify_state IS DISTINCT FROM 'pending'
                 AND category_id IN (SELECT id FROM categories WHERE name = ANY(%s))""",
            (list(categorize.FALLBACK_NAMES),),
        ).rowcount


def create_classify_job(batch_id: int | None, status: str = "queued", error: str | None = None) -> int | None:
    """One job row for the rows waiting in a batch (or everywhere when batch_id is None). None when nothing waits."""
    waiting = len(pending_classification(batch_id))
    if waiting == 0:
        return None
    with connect() as conn:
        return conn.execute(
            "INSERT INTO classify_jobs (batch_id, status, total, error) VALUES (%s, %s, %s, %s) RETURNING id",
            (batch_id, status, waiting, error),
        ).fetchone()["id"]


def get_classify_job(job_id: int) -> dict | None:
    """Progress of a job. A job whose import was undone becomes 'cancelled' here, so Undo needs no hook of its own."""
    with connect() as conn:
        job = conn.execute("SELECT * FROM classify_jobs WHERE id = %s", (job_id,)).fetchone()
        if job and job["batch_id"] is not None and job["status"] in UNFINISHED_JOB:
            undone = conn.execute("SELECT undone_at FROM import_batches WHERE id = %s", (job["batch_id"],)).fetchone()
            if undone and undone["undone_at"] is not None:
                job = conn.execute(
                    "UPDATE classify_jobs SET status = 'cancelled', finished_at = now() WHERE id = %s RETURNING *", (job_id,)
                ).fetchone()
        return job


def update_classify_job(job_id: int, **fields) -> None:
    allowed = {"status", "total", "done", "by_source", "model", "error"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"Unknown classify job fields: {sorted(bad)}")
    if not fields:
        return
    import json as _json

    sets, params = [], []
    for name, value in fields.items():
        if name == "by_source":
            sets.append("by_source = %s::jsonb")
            params.append(_json.dumps(value))
        else:
            sets.append(f"{name} = %s")
            params.append(value)
    if fields.get("status") in ("done", "failed", "cancelled", "no_model"):
        sets.append("finished_at = now()")
    with connect() as conn:
        conn.execute(f"UPDATE classify_jobs SET {', '.join(sets)} WHERE id = %s AND status <> 'cancelled'", [*params, job_id])


def classify_job_for_batch(batch_id: int) -> int | None:
    with connect() as conn:
        row = conn.execute("SELECT id FROM classify_jobs WHERE batch_id = %s ORDER BY id DESC LIMIT 1", (batch_id,)).fetchone()
    return row["id"] if row else None


def unfinished_classify_jobs() -> list[dict]:
    """Jobs a restart interrupted ('queued' or 'running'). 'no_model' jobs wait for the person to ask again."""
    with connect() as conn:
        return conn.execute("SELECT id, batch_id FROM classify_jobs WHERE status IN ('queued', 'running') ORDER BY id").fetchall()


def batch_rows(batch_id: int, show: str = "all", limit: int = 1000) -> list[dict]:
    """The rows one import added, with how each got its category. show: all | misc (still the catch-all) |
    model (decided by the model) | suggested (the model has an idea but was not sure)."""
    if show not in SHOW_FILTERS:
        raise RectifiError(f"show must be one of: {', '.join(SHOW_FILTERS)}.")
    extra = {
        "all": "", "misc": " AND c.name = ANY(%s)", "model": " AND t.category_source = 'llm'",
        "suggested": " AND t.suggested_category_id IS NOT NULL AND t.category_source = 'fallback'",
    }[show]
    params: list = [batch_id]
    if show == "misc":
        params.append(list(categorize.FALLBACK_NAMES))
    params.append(max(1, min(int(limit), 5000)))
    with connect() as conn:
        return conn.execute(
            TXN_SELECT + " WHERE t.import_batch_id = %s" + extra + " ORDER BY t.txn_date, t.id LIMIT %s", params
        ).fetchall()


def batch_source_counts(batch_id: int) -> dict:
    """{'total': n, 'by_source': {'rule': 241, ...}, 'miscellaneous': n} for the summary line above an import's rows."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(category_source, 'rule') AS source, COUNT(*) AS n FROM transactions WHERE import_batch_id = %s GROUP BY 1",
            (batch_id,),
        ).fetchall()
        misc = conn.execute(
            """SELECT COUNT(*) AS n FROM transactions t JOIN categories c ON c.id = t.category_id
               WHERE t.import_batch_id = %s AND c.name = ANY(%s)""",
            (batch_id, list(categorize.FALLBACK_NAMES)),
        ).fetchone()["n"]
    by_source = {r["source"]: r["n"] for r in rows}
    return {"total": sum(by_source.values()), "by_source": by_source, "miscellaneous": misc}
# ═══ WS-CLASSIFY: end ═══

# ═══ WS-CHARTS: begin ═══
# chart_period, chart_frame (integer paise), chart_reference_totals (Decimal), saved charts: save/list/get/pin/delete.
# The frame goes only into the chart sandbox; the model sees a schema and fake rows (rectifi/charts/prompts.py).

CHART_FRAME_COLUMNS = ["date", "month", "weekday", "amount_paise", "amount", "direction", "category", "category_kind", "payee", "account"]
CHART_BUDGET_COLUMNS = ["month", "category", "limit_paise"]
CHART_FRAME_MAX_ROWS = 20000
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def chart_period(text: str | None) -> tuple[date, date, str]:
    """A chart period as words -> (start, end, label). Understands 'YYYY-MM', 'this_month', 'last_month', 'last-30-days',
    'last-3-months' (the default), 'all' and 'YYYY-MM-DD..YYYY-MM-DD'. The end is never after today."""
    raw = (text or "last-3-months").strip().lower().replace("_", "-").replace(" ", "-")
    now = today()
    if raw in ("all", "everything", "all-time"):
        with connect() as conn:
            first = conn.execute("SELECT MIN(txn_date) AS d FROM transactions").fetchone()["d"]
        return (first or now), now, "all time"
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", raw)
    if match:
        start, end = date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
        if start > end:
            raise RectifiError("The start of the period is after its end.")
        return start, min(end, now), f"{start.isoformat()} to {min(end, now).isoformat()}"
    match = re.fullmatch(r"last-(\d{1,3})-(day|days|month|months)", raw)
    if match:
        n = max(1, int(match.group(1)))
        if match.group(2).startswith("day"):
            return now - timedelta(days=n - 1), now, f"last {n} days"
        first = now.replace(day=1)
        for _ in range(n - 1):
            first = (first - timedelta(days=1)).replace(day=1)
        return first, now, f"last {n} months"
    first, last = month_bounds(raw.replace("-month", "_month"))     # 'YYYY-MM', 'this-month', 'last-month' (RectifiError if unreadable)
    return first, min(last, now), first.strftime("%b %Y")


def chart_frame(start: date, end: date) -> dict:
    """Everything the chart sandbox gets, as plain JSON-able lists. Money is integer paise (`amount` is a float for drawing only
    and is never added, compared or stored). Payees are masked and cut to 40 characters. No LLM ever sees these rows."""
    from .privacy import mask_sensitive

    first = start.replace(day=1)
    with connect() as conn:
        rows = conn.execute(
            """SELECT t.txn_date, t.amount, t.direction, c.name AS category, c.kind AS category_kind, t.payee, a.name AS account
               FROM transactions t
               LEFT JOIN categories c ON c.id = t.category_id
               LEFT JOIN accounts a ON a.id = t.account_id
               WHERE t.txn_date BETWEEN %s AND %s
               ORDER BY t.txn_date, t.id LIMIT %s""",
            (start, end, CHART_FRAME_MAX_ROWS + 1),
        ).fetchall()
        budget_rows = conn.execute(
            """SELECT b.month, c.name AS category, b.limit_amount FROM budgets b JOIN categories c ON c.id = b.category_id
               WHERE b.month BETWEEN %s AND %s ORDER BY b.month, c.name""",
            (first, end),
        ).fetchall()
        category_rows = conn.execute("SELECT name, kind FROM categories ORDER BY kind, name").fetchall()
    truncated = len(rows) > CHART_FRAME_MAX_ROWS
    out = []
    for r in rows[:CHART_FRAME_MAX_ROWS]:
        paise = int((Decimal(r["amount"]) * 100).to_integral_value(ROUND_HALF_UP))
        payee = mask_sensitive(r["payee"] or "")[:40]
        d = r["txn_date"]
        out.append([d.isoformat(), d.strftime("%Y-%m"), _WEEKDAYS[d.weekday()], paise, float(Decimal(paise) / 100), r["direction"],
                    r["category"] or "Uncategorised", r["category_kind"] or "expense", payee, r["account"] or ""])
    return {
        "start": start.isoformat(), "end": end.isoformat(), "truncated": truncated,
        "transaction_columns": CHART_FRAME_COLUMNS, "transactions": out,
        "budget_columns": CHART_BUDGET_COLUMNS,
        "budgets": [[b["month"].strftime("%Y-%m"), b["category"], int((Decimal(b["limit_amount"]) * 100).to_integral_value(ROUND_HALF_UP))]
                    for b in budget_rows],
        "categories": [[c["name"], c["kind"]] for c in category_rows],
    }


def chart_reference_totals(start: date, end: date) -> dict:
    """The figures a chart's text may quote, as Decimal, worked out by SQL: spending (debits in expense categories) and income
    (credits in income categories) overall, by category, by month, by category x month and by day; budget limits; the
    number of entries. checks.py compares every rupee figure in a chart's text against these and against what is plotted."""
    def block(direction: str, kind: str) -> dict:
        where = "t.txn_date BETWEEN %s AND %s AND t.direction = %s AND c.kind = %s"
        args = (start, end, direction, kind)
        with connect() as conn:
            overall = conn.execute(f"SELECT COALESCE(SUM(t.amount), 0) AS v, COUNT(*) AS n FROM transactions t JOIN categories c ON c.id = t.category_id WHERE {where}", args).fetchone()
            by_cat = conn.execute(f"SELECT c.name AS k, SUM(t.amount) AS v FROM transactions t JOIN categories c ON c.id = t.category_id WHERE {where} GROUP BY c.name", args).fetchall()
            by_month = conn.execute(f"SELECT to_char(t.txn_date, 'YYYY-MM') AS k, SUM(t.amount) AS v FROM transactions t JOIN categories c ON c.id = t.category_id WHERE {where} GROUP BY 1", args).fetchall()
            by_cat_month = conn.execute(f"SELECT c.name AS k, to_char(t.txn_date, 'YYYY-MM') AS m, SUM(t.amount) AS v FROM transactions t JOIN categories c ON c.id = t.category_id WHERE {where} GROUP BY 1, 2", args).fetchall()
            by_day = conn.execute(f"SELECT t.txn_date AS k, SUM(t.amount) AS v FROM transactions t JOIN categories c ON c.id = t.category_id WHERE {where} GROUP BY 1", args).fetchall()
        return {"overall": Decimal(overall["v"]), "count": overall["n"],
                "by_category": {r["k"]: Decimal(r["v"]) for r in by_cat},
                "by_month": {r["k"]: Decimal(r["v"]) for r in by_month},
                "by_category_month": {(r["k"], r["m"]): Decimal(r["v"]) for r in by_cat_month},
                "by_day": {r["k"]: Decimal(r["v"]) for r in by_day}}

    spending, income = block("debit", "expense"), block("credit", "income")
    with connect() as conn:
        limits = conn.execute(
            """SELECT c.name AS category, to_char(b.month, 'YYYY-MM') AS m, b.limit_amount AS v FROM budgets b JOIN categories c ON c.id = b.category_id
               WHERE b.month BETWEEN %s AND %s""", (start.replace(day=1), end)).fetchall()
        entries = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE txn_date BETWEEN %s AND %s", (start, end)).fetchone()["n"]
    by_month_limit: dict = {}
    for r in limits:
        by_month_limit[r["m"]] = by_month_limit.get(r["m"], D0) + Decimal(r["v"])
    return {"start": start, "end": end, "entries": entries, "spending": spending, "income": income,
            "net": income["overall"] - spending["overall"],
            "budget_limits": {"by_category_month": {(r["category"], r["m"]): Decimal(r["v"]) for r in limits}, "by_month": by_month_limit}}


_CHART_LIST_COLUMNS = ("id, title, prompt, kind, period_start, period_end, pinned, model, attempts, trace, checks, thread_id, "
                       "created_at, refreshed_at")


def save_chart(*, title: str, prompt: str, code: str, image_light: bytes, image_dark: bytes, period_start: date | None, period_end: date | None,
               model: str | None, attempts: int, trace: list, checks: dict, thread_id: str | None = None, pinned: bool = False) -> int:
    from psycopg.types.json import Jsonb

    with connect() as conn:
        row = conn.execute(
            """INSERT INTO charts (title, prompt, code, kind, image_light, image_dark, period_start, period_end, pinned, model, attempts,
                                   trace, checks, thread_id)
               VALUES (%s, %s, %s, 'png', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            ((title or "Chart")[:200], prompt[:2000], code, image_light, image_dark, period_start, period_end, pinned, model, max(1, int(attempts)),
             Jsonb(trace), Jsonb(checks), thread_id),
        ).fetchone()
    return row["id"]


def list_charts(pinned_only: bool = False, limit: int = 100) -> list[dict]:
    where = "WHERE pinned" if pinned_only else ""
    with connect() as conn:
        return conn.execute(f"SELECT {_CHART_LIST_COLUMNS} FROM charts {where} ORDER BY COALESCE(refreshed_at, created_at) DESC, id DESC LIMIT %s",
                            (max(1, min(int(limit), 500)),)).fetchall()


def get_chart(chart_id: int, *, with_code: bool = True, with_images: bool = True) -> dict:
    cols = _CHART_LIST_COLUMNS + (", code" if with_code else "") + (", image_light, image_dark" if with_images else "")
    with connect() as conn:
        row = conn.execute(f"SELECT {cols} FROM charts WHERE id = %s", (chart_id,)).fetchone()
    if not row:
        raise RectifiError("There is no saved chart with that id.")
    return row


def set_chart_pinned(chart_id: int, pinned: bool) -> dict:
    with connect() as conn:
        row = conn.execute("UPDATE charts SET pinned = %s WHERE id = %s RETURNING id", (bool(pinned), chart_id)).fetchone()
    if not row:
        raise RectifiError("There is no saved chart with that id.")
    return get_chart(chart_id, with_code=False, with_images=False)


def update_chart_run(chart_id: int, *, image_light: bytes, image_dark: bytes, period_start: date | None, period_end: date | None,
                     trace: list, checks: dict) -> dict:
    """Store the result of re-running a saved chart's code on the latest data (no model involved)."""
    from psycopg.types.json import Jsonb

    with connect() as conn:
        row = conn.execute(
            """UPDATE charts SET image_light = %s, image_dark = %s, period_start = %s, period_end = %s, trace = %s, checks = %s,
                                 refreshed_at = now() WHERE id = %s RETURNING id""",
            (image_light, image_dark, period_start, period_end, Jsonb(trace), Jsonb(checks), chart_id)).fetchone()
    if not row:
        raise RectifiError("There is no saved chart with that id.")
    return get_chart(chart_id, with_code=False, with_images=False)


def delete_chart(chart_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM charts WHERE id = %s", (chart_id,)).rowcount > 0
# ═══ WS-CHARTS: end ═══

# ═══ WS-CHAT: begin ═══
# Conversations (one row per chat thread), local trace queries, and the learned category rules shown on the Memory page.
# The chat messages themselves live in LangGraph's checkpoint tables (agent/checkpoint.py); this block only keeps the list.
import uuid as _uuid

from .privacy import mask_sensitive as _mask

CONV_TITLE_DEFAULT = "New conversation"
CONV_TITLE_MAX = 80
CONV_SEARCH_MAX = 20_000
_CONV_COLS = "thread_id, title, preview, created_at, updated_at"


def _clean_title(title: str | None) -> str:
    text = re.sub(r"\s+", " ", title or "").strip()
    if not text:
        raise RectifiError("A conversation needs a name.")
    return text[:CONV_TITLE_MAX]


def conversation_create(thread_id: str | None = None, title: str | None = None) -> dict:
    """Make the list row for a thread. Calling it again for the same thread changes nothing."""
    tid = thread_id or str(_uuid.uuid4())
    name = _clean_title(title) if title else CONV_TITLE_DEFAULT
    with connect() as conn:
        conn.execute("INSERT INTO conversations (thread_id, title) VALUES (%s, %s) ON CONFLICT (thread_id) DO NOTHING", (tid, name))
        return conn.execute(f"SELECT {_CONV_COLS} FROM conversations WHERE thread_id = %s", (tid,)).fetchone()


def conversation_get(thread_id: str) -> dict | None:
    with connect() as conn:
        return conn.execute(f"SELECT {_CONV_COLS} FROM conversations WHERE thread_id = %s", (thread_id,)).fetchone()


def conversation_list(q: str | None = None, limit: int = 100) -> list[dict]:
    """Newest first. `q` matches the title or the (masked) text of the conversation, ignoring case."""
    limit = max(1, min(int(limit), 500))
    with connect() as conn:
        if q and q.strip():
            like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            return conn.execute(
                f"SELECT {_CONV_COLS} FROM conversations WHERE title ILIKE %s OR search_text ILIKE %s ORDER BY updated_at DESC LIMIT %s",
                (like, like, limit)).fetchall()
        return conn.execute(f"SELECT {_CONV_COLS} FROM conversations ORDER BY updated_at DESC LIMIT %s", (limit,)).fetchall()


def conversation_rename(thread_id: str, title: str) -> dict:
    name = _clean_title(title)
    with connect() as conn:
        row = conn.execute(f"UPDATE conversations SET title = %s WHERE thread_id = %s RETURNING {_CONV_COLS}", (name, thread_id)).fetchone()
    if row is None:
        raise RectifiError("There is no conversation with that id.")
    return row


def conversation_delete(thread_id: str) -> bool:
    """Remove the list row and this thread's trace spans. The caller removes the checkpoints (that needs LangGraph)."""
    with connect() as conn:
        conn.execute("DELETE FROM trace_spans WHERE thread_id = %s", (thread_id,))
        return conn.execute("DELETE FROM conversations WHERE thread_id = %s", (thread_id,)).rowcount > 0


def conversation_touch(thread_id: str, *, user_text: str | None = None, assistant_text: str | None = None) -> dict:
    """After a turn: make sure the row exists, name it from the first question, set the preview, append the masked text
    to the search column (the newest 20 KB are kept) and bump updated_at."""
    mu = _mask(re.sub(r"\s+", " ", user_text or "").strip())
    ma = _mask(re.sub(r"\s+", " ", assistant_text or "").strip())
    with connect() as conn:
        conn.execute("INSERT INTO conversations (thread_id) VALUES (%s) ON CONFLICT (thread_id) DO NOTHING", (thread_id,))
        row = conn.execute("SELECT title, search_text FROM conversations WHERE thread_id = %s FOR UPDATE", (thread_id,)).fetchone()
        title = row["title"]
        if mu and title == CONV_TITLE_DEFAULT:
            title = mu[:48].rstrip() + ("…" if len(mu) > 48 else "")
        search = (row["search_text"] + " " + mu + " " + ma).strip()[-CONV_SEARCH_MAX:]
        preview = (ma or mu)[:120] or None
        return conn.execute(
            f"UPDATE conversations SET title = %s, search_text = %s, preview = COALESCE(%s, preview), updated_at = now() "
            f"WHERE thread_id = %s RETURNING {_CONV_COLS}", (title, search, preview, thread_id)).fetchone()


# ── traces (written by agent/tracing.py, read by the Traces page) ──

_ROUTE_SPANS = {"knowledge": "knowledge", "chart": "chart", "smalltalk": "chat", "remember": "memory", "agent": "agent"}


def trace_runs(limit: int = 50, status: str | None = None, entry: str | None = None) -> list[dict]:
    """The newest runs (a run is a root span). Latency is wall time from the first span start to the last end."""
    limit = max(1, min(int(limit), 200))
    with connect() as conn:
        roots = conn.execute(
            """SELECT r.run_id AS root_run_id, r.name AS entry, r.thread_id, r.started_at,
                      (SELECT count(*) FROM trace_spans s WHERE s.root_run_id = r.run_id) AS span_count,
                      (SELECT bool_or(s.status = 'error') FROM trace_spans s WHERE s.root_run_id = r.run_id) AS has_error,
                      (SELECT max(s.ended_at) FROM trace_spans s WHERE s.root_run_id = r.run_id) AS ended_at,
                      (SELECT string_agg(DISTINCT s.model, ', ') FROM trace_spans s WHERE s.root_run_id = r.run_id AND s.model IS NOT NULL) AS model,
                      (SELECT COALESCE(sum(s.input_tokens), 0) + COALESCE(sum(s.output_tokens), 0) FROM trace_spans s WHERE s.root_run_id = r.run_id) AS tokens,
                      (SELECT array_agg(DISTINCT s.name) FROM trace_spans s WHERE s.root_run_id = r.run_id AND s.name = ANY(%s)) AS route_nodes,
                      r.status AS root_status, r.input_preview
               FROM trace_spans r WHERE r.run_id = r.root_run_id ORDER BY r.started_at DESC LIMIT %s""",
            (list(_ROUTE_SPANS), limit * 4 if (status or entry) else limit)).fetchall()
    runs = []
    for r in roots:
        names = set(r["route_nodes"] or [])
        route = next((label for key, label in _ROUTE_SPANS.items() if key in names and key != "agent"), "agent" if "agent" in names else None)
        failed = bool(r["has_error"]) or r["root_status"] == "error"
        label = "chat" if r["entry"] in ("chat", "ask") else r["entry"]
        run = {"root_run_id": str(r["root_run_id"]), "entry": label, "route": route, "thread_id": r["thread_id"],
               "started_at": r["started_at"], "latency_ms": _ms(r["started_at"], r["ended_at"]), "status": "error" if failed else
               ("running" if r["root_status"] == "running" else "ok"), "span_count": r["span_count"], "model": r["model"],
               "tokens": int(r["tokens"] or 0), "preview": r["input_preview"]}
        if (status and run["status"] != status) or (entry and run["entry"] != entry):
            continue
        runs.append(run)
    return runs[:limit]


def _ms(start, end) -> int | None:
    return int((end - start).total_seconds() * 1000) if start and end else None


def trace_tree(root_run_id: str) -> dict | None:
    """One run with its spans in tree order (each span knows its depth)."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM trace_spans WHERE root_run_id = %s ORDER BY started_at, id", (root_run_id,)).fetchall()
    if not rows:
        return None
    children: dict = {}
    for r in rows:
        children.setdefault(r["parent_run_id"], []).append(r)
    known = {r["run_id"] for r in rows}
    ordered: list[dict] = []

    def walk(parent, depth):
        for r in children.get(parent, []):
            ordered.append({"run_id": str(r["run_id"]), "parent_run_id": str(r["parent_run_id"]) if r["parent_run_id"] else None,
                            "depth": depth, "name": r["name"], "run_type": r["run_type"], "model": r["model"], "status": r["status"],
                            "started_at": r["started_at"], "ms": _ms(r["started_at"], r["ended_at"]),
                            "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                            "input_preview": r["input_preview"], "output_preview": r["output_preview"], "error": r["error"]})
            walk(r["run_id"], depth + 1)

    walk(None, 0)
    for orphan in [r for r in rows if r["parent_run_id"] is not None and r["parent_run_id"] not in known]:   # parent span never ended
        ordered.append({"run_id": str(orphan["run_id"]), "parent_run_id": str(orphan["parent_run_id"]), "depth": 1, "name": orphan["name"],
                        "run_type": orphan["run_type"], "model": orphan["model"], "status": orphan["status"], "started_at": orphan["started_at"],
                        "ms": _ms(orphan["started_at"], orphan["ended_at"]), "input_tokens": orphan["input_tokens"],
                        "output_tokens": orphan["output_tokens"], "input_preview": orphan["input_preview"],
                        "output_preview": orphan["output_preview"], "error": orphan["error"]})
    root = next((r for r in rows if r["run_id"] == r["root_run_id"]), rows[0])
    summary = next((x for x in trace_runs(200) if x["root_run_id"] == str(root["root_run_id"])), None)
    return {"run": summary or {"root_run_id": str(root["root_run_id"]), "entry": root["name"], "status": root["status"]}, "spans": ordered}


def trace_clear() -> int:
    with connect() as conn:
        return conn.execute("DELETE FROM trace_spans").rowcount


# ── category rules the person taught (shown on the Memory page) ──

def memory_learned_rules() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """SELECT r.id, r.pattern, c.name AS category, r.created_at FROM merchant_rules r JOIN categories c ON c.id = r.category_id
               WHERE r.source = 'learned' ORDER BY r.created_at DESC, r.id DESC""").fetchall()


def memory_remove_learned_rule(rule_id: int) -> bool:
    """Forget one rule you taught. Rules that ship with Rectifi cannot be removed here."""
    with connect() as conn:
        return conn.execute("DELETE FROM merchant_rules WHERE id = %s AND source = 'learned'", (rule_id,)).rowcount > 0
# ═══ WS-CHAT: end ═══

# ═══ WS-RAG: begin ═══
# Knowledge documents and the rows the knowledge loaders read. Plain SQL and plain text: no model, no embeddings here.
# The vector maths and the chunk embeddings live in rectifi/rag/ (it may use model libraries; this file may not).

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"      # the built-in guides, shipped with Rectifi
RAG_CORPORA = ("guide", "note", "upload", "txn")
RAG_UPLOAD_EXTENSIONS = (".md", ".txt")
RAG_UPLOAD_MAX = 300 * 1024            # characters-ish: a note or a guide, not a book
RAG_TITLE_MAX = 120

_STATEMENT_DATE = re.compile(r"\b(?:\d{1,2}[-/ ](?:\d{1,2}|[A-Za-z]{3,9})[-/ ]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
_STATEMENT_AMOUNT = re.compile(r"\d[\d,]*\.\d{2}\b")
_STATEMENT_HEADER = re.compile(
    r"(?i)(opening balance|closing balance|withdrawal amt|deposit amt|value d(?:t|ate)|chq\.?\s*/?\s*ref|statement of account|"
    r"running balance|txn date|transaction date)")


def rag_looks_like_statement(text: str) -> bool:
    """True when a text looks like a bank statement: several lines that each have a date and an amount, or statement column
    headings. Statements belong in Add > Import (which proves them against the balance), never in the knowledge library."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    rowlike = sum(1 for line in lines if _STATEMENT_DATE.search(line) and _STATEMENT_AMOUNT.search(line))
    headers = {m.group(0).lower().replace(" ", "") for m in _STATEMENT_HEADER.finditer(text or "")}
    return rowlike >= 5 or (len(headers) >= 2 and rowlike >= 2) or len(headers) >= 3


def _title_of(text: str, fallback: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:RAG_TITLE_MAX]
    return fallback[:RAG_TITLE_MAX]


def rag_check_upload(filename: str, data: bytes) -> tuple[str, str, str]:
    """Check a file someone added to the library. Returns (source, title, text) or raises RectifiError with a plain reason.
    Only .md and .txt are accepted. A bank statement (or anything that looks like one) is refused."""
    name = Path(filename or "").name.strip()
    if not name:
        raise RectifiError("Choose a file to add.")
    if not name.lower().endswith(RAG_UPLOAD_EXTENSIONS):
        raise RectifiError("The library takes notes as .md or .txt files. Bank statements go in Add, then Import, where Rectifi checks them against your balance.")
    if len(data) > RAG_UPLOAD_MAX:
        raise RectifiError(f"That file is too long for the library (the limit is {RAG_UPLOAD_MAX // 1024} KB of text).")
    if b"\x00" in data:
        raise RectifiError("That doesn't look like a text file.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RectifiError("That file isn't UTF-8 text. Save it as plain UTF-8 and add it again.") from None
    text = text.strip()
    if not text:
        raise RectifiError("That file is empty.")
    if rag_looks_like_statement(text):
        raise RectifiError("That looks like a bank statement. Add statements in Add, then Import, so Rectifi can check them against your balance. The library is for your own notes.")
    return name, _title_of(text, Path(name).stem), text


def rag_guide_files() -> list[dict]:
    """The built-in guides on disk, sorted by file name: [{source, title, content, sha256}]. source is 'guides/<file>.md'."""
    out = []
    for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        out.append({"source": f"guides/{path.name}", "title": _title_of(text, path.stem), "content": text,
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return out


def rag_upsert_document(corpus: str, source: str, title: str, content: str) -> dict:
    """Add or replace one library document. Returns {id, changed}: changed is False when the same text was already stored."""
    if corpus not in ("guide", "note", "upload"):
        raise RectifiError(f"Unknown library section {corpus!r}.")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with connect() as conn:
        row = conn.execute("SELECT id, sha256 FROM rag_documents WHERE corpus = %s AND source = %s", (corpus, source)).fetchone()
        if row and row["sha256"] == digest:
            conn.execute("UPDATE rag_documents SET title = %s WHERE id = %s", (title[:RAG_TITLE_MAX], row["id"]))
            return {"id": row["id"], "changed": False}
        if row:
            conn.execute("UPDATE rag_documents SET title = %s, sha256 = %s, content = %s WHERE id = %s",
                         (title[:RAG_TITLE_MAX], digest, content, row["id"]))
            return {"id": row["id"], "changed": True}
        new = conn.execute(
            "INSERT INTO rag_documents (corpus, title, source, sha256, content) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (corpus, title[:RAG_TITLE_MAX], source, digest, content)).fetchone()
        return {"id": new["id"], "changed": True}


def rag_list_documents(corpus: str | None = None) -> list[dict]:
    """Library documents with how many chunks each has (any embedding model). Newest first within a section."""
    with connect() as conn:
        return conn.execute(
            """SELECT d.id, d.corpus, d.title, d.source, d.created_at, d.sha256,
                      (SELECT COUNT(*) FROM rag_chunks c WHERE c.document_id = d.id) AS chunks
               FROM rag_documents d WHERE (%s::text IS NULL OR d.corpus = %s)
               ORDER BY d.corpus, d.created_at DESC, d.id DESC""", (corpus, corpus)).fetchall()


def rag_get_document(doc_id: int) -> dict | None:
    with connect() as conn:
        return conn.execute("SELECT id, corpus, title, source, content, sha256, created_at FROM rag_documents WHERE id = %s", (doc_id,)).fetchone()


def rag_delete_document(doc_id: int, *, corpus: str | None = None) -> bool:
    """Remove a document and (by ON DELETE CASCADE) its chunks. Pass corpus to be sure a guide is never deleted by an upload route."""
    with connect() as conn:
        return conn.execute("DELETE FROM rag_documents WHERE id = %s AND (%s::text IS NULL OR corpus = %s)",
                            (doc_id, corpus, corpus)).rowcount > 0


def rag_delete_missing(corpus: str, keep_sources: list[str]) -> int:
    """Delete documents of one section whose source is no longer in keep_sources (a note that was removed, a guide renamed)."""
    with connect() as conn:
        return conn.execute("DELETE FROM rag_documents WHERE corpus = %s AND NOT (source = ANY(%s))", (corpus, list(keep_sources))).rowcount


def rag_note_rows() -> list[dict]:
    """The person's own words: each weekly commitment and each transaction note, as [{source, title, content}].
    This is the user's text, so a rupee figure in it counts as typed by the user.

    Multi-user mode (D8): both come from sealed columns (commitment, note, and the payee in the title), and this index is plain
    text, so nothing is copied out. The notes the person uploads to the library are separate and still indexed."""
    out = []
    if config.multiuser():
        return out
    with connect() as conn:
        for r in conn.execute("SELECT week_start, commitment FROM weekly_reviews ORDER BY week_start").fetchall():
            out.append({"source": f"commitment:{r['week_start']}", "title": f"Weekly commitment, week of {r['week_start']}",
                        "content": f"Weekly commitment for the week starting {r['week_start']}: {r['commitment']}"})
        for r in conn.execute(
                "SELECT id, txn_date, payee, description, note FROM transactions WHERE note IS NOT NULL AND btrim(note) <> '' ORDER BY id").fetchall():
            who = r["payee"] or r["description"] or "a payment"
            out.append({"source": f"txn-note:{r['id']}", "title": f"Note on {who}, {r['txn_date']}",
                        "content": f"Note on {who} ({r['txn_date']}): {r['note'].strip()}"})
    return out


def rag_txn_rows(after_id: int = 0, limit: int | None = None) -> list[dict]:
    """Transactions in id order for the knowledge loader: id, txn_date, amount (Decimal), direction, payee, description, note,
    category. The loader writes the sentence and masks it; this function only reads."""
    sql = """SELECT t.id, t.txn_date, t.amount, t.direction, t.payee, t.description, t.note, c.name AS category
             FROM transactions t LEFT JOIN categories c ON c.id = t.category_id WHERE t.id > %s ORDER BY t.id"""
    params: list = [after_id]
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def rag_status(embed_model: str) -> dict:
    """What the library has indexed with this embedding model: chunks per section, documents, the newest chunk's time, and
    how many chunks were made with some other model (they are ignored until a re-index)."""
    with connect() as conn:
        chunks = {r["corpus"]: r["n"] for r in conn.execute(
            "SELECT corpus, COUNT(*) AS n FROM rag_chunks WHERE embed_model = %s GROUP BY corpus", (embed_model,)).fetchall()}
        other = conn.execute("SELECT COUNT(*) AS n FROM rag_chunks WHERE embed_model <> %s", (embed_model,)).fetchone()["n"]
        newest = conn.execute("SELECT max(created_at) AS t FROM rag_chunks WHERE embed_model = %s", (embed_model,)).fetchone()["t"]
        docs = {r["corpus"]: r["n"] for r in conn.execute("SELECT corpus, COUNT(*) AS n FROM rag_documents GROUP BY corpus").fetchall()}
        txns = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
    return {"embed_model": embed_model, "chunks": {c: chunks.get(c, 0) for c in RAG_CORPORA}, "chunks_total": sum(chunks.values()),
            "documents": {c: docs.get(c, 0) for c in ("guide", "note", "upload")}, "transactions": 0 if config.multiuser() else txns,
            "other_model_chunks": other, "updated_at": newest}
# ═══ WS-RAG: end ═══
# ═══ WS-AI: end ═══
