"""Leak alerts and real-time budget alerts. 100% deterministic (CLAUDE.md rule 15).

Fixed rules, SQL and Decimal. No model reads your data to make an alert, and nothing here talks to one (a test checks
that this file loads neither langchain nor `rectifi.agent`). Every rupee figure in an alert's title, detail, push_title
or push_body is a whole-rupee Decimal that is also in `Alert.numbers` (a test enforces it).

Part 1 (this top half) is pure: it reads the ledger into a `Context` and turns it into alerts and numbers.
  caps        a spending cap (a merchant, a group like Food delivery, a category, or everything) per day, week or month,
              plus every monthly budget as an implicit cap. Only the HIGHEST newly crossed step (50, 80, 100 %) fires.
  streaks     the 5th, 7th and 10th order from one merchant in an ISO week
  projection  month to date x days in month / day of month (not before day 5)
  safe to spend, income, presets, the leak report (waterfall) and the Today strip

Part 2 (the bottom half) has the side effects: `sweep` (find new debits, write alerts), `deliver` (Web Push), `tick`
(release held alerts, the evening digest, retries) and the small CRUD the API wraps.

Why a watermark sweep and not a hook inside `add_transaction`: that function runs inside the caller's database
transaction, and the SMS gateway (:8765) and the web app (:8501) are separate processes. A sweep looks at debits newer
than `app_settings['alerts.watermark']` under a Postgres advisory lock, and `alerts.dedupe_key` is UNIQUE, so the same
alert can never be written (or pushed) twice.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from statistics import median

from . import insights, merchants
from .categorize import FALLBACK_EXPENSE
from .db import connect
from .money import fmt_inr, parse_amount
from .tools import RectifiError

log = logging.getLogger("rectifi.alerts")

# ───────────────────────── tunables (every threshold in one place) ─────────────────────────

THRESHOLDS = (50, 80, 100)            # percent of a cap; only the highest newly crossed one fires
STREAK_TIERS = (5, 7, 10)             # orders from one merchant in one ISO week
BULK_MIN = 5                          # a sweep that finds MORE new debits than this is "bulk": one summary, not many alerts
REALTIME_DAYS = 2                     # a payment dated more than this many days ago is not "real time"
MIN_PROJECTION_DAY = 5                # no month projection before this day: too noisy
SMALL_SPEND_MAX = Decimal("500")      # a payment under this is a "small spend" on the Today strip
BIG_ONE_OFF = Decimal("1000")         # the leak report calls a non-recurring payment this big a "big one-off"
PRESET_MONTHS = 3                     # presets use the average of this many complete months
PRESET_TRIM = Decimal("0.8")          # "Trim 20%"
ROUND_TO = Decimal("100")             # a preset cap is rounded UP to a multiple of this
SALARY_CATEGORY = "Salary & Allowance"
INCOME_LOOKBACK_DAYS = 45             # a salary older than this is not "detected" any more
FIXED_CATEGORIES = ("Rent", "EMI & Loans")
RETRY_MAX = 5                         # a push that fails is tried again this many times, then left in the inbox only
RETRY_BACKOFF_MINUTES = (1, 5, 15, 60, 180)
PUSH_TTL_SECONDS = 3600
SWEEP_LOCK_ID = 7_314_201             # pg_advisory_xact_lock key for sweep
TICK_LOCK_ID = 7_314_202

PERIODS = ("day", "week", "month")
SCOPES = ("merchant", "group", "category", "total")
SEVERITY_FOR = {50: "info", 80: "watch", 100: "act"}
KINDS = ("cap", "streak", "bulk", "digest")

PREF_DEFAULTS = {
    "push_on": True,
    "private_notifications": False,
    "quiet_start": "22:00",
    "quiet_end": "07:30",
    "max_per_hour": 3,
    "max_per_day": 8,
    "cooldown_hours": 6,
    "digest_on": False,
    "digest_time": "21:00",
    "monthly_income": None,
    "spend_plan": None,
    "snooze_until": None,
    "muted_kinds": [],
    "muted_scopes": [],
    "snoozed_scopes": {},
}


@dataclass
class Alert:
    kind: str
    severity: str                                   # info | watch | act
    title: str
    detail: str
    numbers: dict[str, Decimal]                     # every figure in the four texts; money keys are whole rupees
    dedupe_key: str
    push_title: str
    push_body: str
    rule_id: int | None = None
    txn_id: int | None = None
    scope_key: str | None = None                    # cooldown and mute are per scope_key
    threshold: int | None = None                    # 50 / 80 / 100 for a cap alert

    def __post_init__(self) -> None:
        assert self.severity in insights.SEVERITIES, self.severity
        assert self.kind in KINDS, self.kind


@dataclass
class Context(insights.Context):
    rules: list[dict] = field(default_factory=list)
    budgets: dict[str, Decimal] = field(default_factory=dict)        # this month's budgets: category -> limit
    prefs: dict = field(default_factory=dict)
    patterns: list[tuple[str, str, str]] = field(default_factory=list)


# ───────────────────────── small helpers ─────────────────────────

def _whole(value) -> Decimal:
    return Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def _rs(value) -> str:
    return fmt_inr(_whole(value), paise=False)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def week_start(d: date) -> date:
    """Monday of the ISO week that holds `d`."""
    return d - timedelta(days=d.weekday())


def period_start(period: str, d: date) -> date:
    return {"day": d, "week": week_start(d), "month": d.replace(day=1)}[period]


def _days_in(first: date) -> int:
    return calendar.monthrange(first.year, first.month)[1]


def _shift(first: date, back: int) -> date:
    return insights._shift_month(first, back)


def _ceil_to(value: Decimal, step: Decimal = ROUND_TO) -> Decimal:
    """Round UP to a multiple of `step`, never below one step."""
    return max(step, (value / step).to_integral_value(rounding=ROUND_CEILING) * step)


# ───────────────────────── preferences ─────────────────────────

@contextmanager
def _use(conn=None):
    """Use the given connection, or borrow one. A sweep passes its own so it never needs a second one while holding the lock."""
    if conn is not None:
        yield conn
    else:
        with connect() as own:
            yield own


def get_prefs(conn=None) -> dict:
    with _use(conn) as c:
        row = c.execute("SELECT value FROM app_settings WHERE key = 'alerts.prefs'").fetchone()
    saved = row["value"] if row and isinstance(row["value"], dict) else {}
    return {**PREF_DEFAULTS, **{k: v for k, v in saved.items() if k in PREF_DEFAULTS}}


def _write_prefs(prefs: dict, conn=None) -> None:
    payload = json.dumps({k: prefs[k] for k in PREF_DEFAULTS})
    sql = ("INSERT INTO app_settings (key, value) VALUES ('alerts.prefs', %s::jsonb) "
           "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()")
    if conn is not None:
        conn.execute(sql, (payload,))
        return
    with connect() as c:
        c.execute(sql, (payload,))


def _hhmm(value, name: str) -> str:
    try:
        parsed = datetime.strptime(str(value), "%H:%M")
    except ValueError as exc:
        raise RectifiError(f"{name} must look like 22:00 (hours and minutes).") from exc
    return parsed.strftime("%H:%M")


def _limit(value, name: str, low: int = 1, high: int = 20) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RectifiError(f"{name} must be a whole number from {low} to {high}.")
    return value


def _optional_amount(value, name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        amount = parse_amount(value)
    except ValueError as exc:
        raise RectifiError(f"{name}: {exc}") from exc
    if amount <= 0:
        raise RectifiError(f"{name} must be more than ₹0.")
    return str(amount)


def save_prefs(changes: dict) -> dict:
    """Validate and save a partial update of the settings the Delivery tab edits. Returns all settings."""
    prefs = get_prefs()
    for key, value in changes.items():
        if key in ("push_on", "private_notifications", "digest_on"):
            if not isinstance(value, bool):
                raise RectifiError(f"{key} must be true or false.")
            prefs[key] = value
        elif key in ("quiet_start", "quiet_end", "digest_time"):
            prefs[key] = _hhmm(value, key)
        elif key in ("max_per_hour", "max_per_day"):
            prefs[key] = _limit(value, key)
        elif key == "cooldown_hours":
            prefs[key] = _limit(value, key, 0, 72)
        elif key in ("monthly_income", "spend_plan"):
            prefs[key] = _optional_amount(value, key)
        elif key == "snooze_until":
            prefs[key] = None if not value else _iso(value)
        elif key == "muted_kinds":
            if not isinstance(value, list) or any(k not in KINDS for k in value):
                raise RectifiError("muted_kinds must be a list of alert kinds.")
            prefs[key] = sorted(set(value))
        else:
            raise RectifiError(f"Unknown setting: {key}")
    _write_prefs(prefs)
    return prefs


def _iso(value) -> str:
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise RectifiError("snooze_until must be a date and time.") from exc


# ───────────────────────── rules ─────────────────────────

def _rule_target(scope: str, target: str) -> str:
    """The stored spelling of a rule's target, or RectifiError when it names nothing Rectifi knows."""
    target = (target or "").strip()
    if scope == "total":
        return "all"
    if scope == "merchant":
        known = merchants.known_merchants()
        for name in known:
            if name.lower() == target.lower():
                return name
        raise RectifiError(f"I don't know a merchant called {target!r}. Teach it first under 'Add a merchant name'.")
    if scope == "group":
        for name in merchants.GROUPS:
            if name.lower() == target.lower():
                return name
        raise RectifiError(f"Group must be one of: {', '.join(merchants.GROUPS)}.")
    if scope == "category":
        with connect() as conn:
            row = conn.execute("SELECT name FROM categories WHERE lower(name) = lower(%s) AND kind = 'expense'", (target,)).fetchone()
        if row is None:
            raise RectifiError(f"{target!r} is not a spending category.")
        return row["name"]
    raise RectifiError(f"Scope must be one of: {', '.join(SCOPES)}.")


def _rule_cap(cap) -> Decimal:
    try:
        value = parse_amount(cap)
    except ValueError as exc:
        raise RectifiError(str(exc)) from exc
    if value <= 0:
        raise RectifiError("A cap must be more than ₹0.")
    return value


def list_rules() -> list[dict]:
    with connect() as conn:
        return conn.execute("SELECT id, scope, target, period, cap, muted, snoozed_until, created_at FROM alert_rules ORDER BY id").fetchall()


def create_rule(scope: str, target: str, period: str, cap) -> dict:
    if scope not in SCOPES:
        raise RectifiError(f"Scope must be one of: {', '.join(SCOPES)}.")
    if period not in PERIODS:
        raise RectifiError(f"Period must be one of: {', '.join(PERIODS)}.")
    target, value = _rule_target(scope, target), _rule_cap(cap)
    with connect() as conn:
        # Saving the same merchant/period again just changes its cap (and un-mutes it).
        return conn.execute(
            "INSERT INTO alert_rules (scope, target, period, cap) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id, scope, target_blind, period) DO UPDATE SET cap = EXCLUDED.cap, muted = false, snoozed_until = NULL "
            "RETURNING id, scope, target, period, cap, muted, snoozed_until, created_at",
            (scope, target, period, value)).fetchone()


def update_rule(rule_id: int, **changes) -> dict:
    allowed = {"cap", "muted", "period", "snoozed_until"}
    unknown = set(changes) - allowed
    if unknown:
        raise RectifiError(f"You can change only: {', '.join(sorted(allowed))}.")
    with connect() as conn:
        row = conn.execute("SELECT * FROM alert_rules WHERE id = %s", (rule_id,)).fetchone()
        if row is None:
            raise RectifiError("No such alert rule.")
        cap = _rule_cap(changes["cap"]) if "cap" in changes else row["cap"]
        period = changes.get("period", row["period"])
        if period not in PERIODS:
            raise RectifiError(f"Period must be one of: {', '.join(PERIODS)}.")
        muted = changes.get("muted", row["muted"])
        if not isinstance(muted, bool):
            raise RectifiError("muted must be true or false.")
        snoozed = changes.get("snoozed_until", row["snoozed_until"])
        try:
            return conn.execute(
                "UPDATE alert_rules SET cap = %s, period = %s, muted = %s, snoozed_until = %s WHERE id = %s "
                "RETURNING id, scope, target, period, cap, muted, snoozed_until, created_at",
                (cap, period, muted, snoozed, rule_id)).fetchone()
        except Exception as exc:                       # the same scope/target/period already exists
            raise RectifiError("You already have a cap for that. Edit that one instead.") from exc


def delete_rule(rule_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM alert_rules WHERE id = %s", (rule_id,)).rowcount > 0


# ───────────────────────── reading the ledger ─────────────────────────

def build_context(today: date | None = None, since: date | None = None, conn=None) -> Context:
    """Everything the pure rules need, read once: six months of transactions (each tagged with its merchant), the caps, this
    month's budgets and the settings."""
    from . import tools

    today = today or tools.today()
    m0 = today.replace(day=1)
    start = min(_shift(m0, 5), since) if since else _shift(m0, 5)
    with _use(conn) as c:
        patterns = merchants.user_patterns(c)
        rows = c.execute(
            """SELECT t.id, t.txn_date, t.amount, t.direction, t.payee, t.description, t.account_id, t.import_batch_id,
                      c.name AS category, c.kind AS kind
               FROM transactions t JOIN categories c ON c.id = t.category_id
               WHERE t.txn_date BETWEEN %s AND %s ORDER BY t.txn_date, t.id""", (start, today)).fetchall()
        first = c.execute("SELECT MIN(txn_date) AS d FROM transactions").fetchone()["d"]
        rules = c.execute("SELECT id, scope, target, period, cap, muted, snoozed_until FROM alert_rules ORDER BY id").fetchall()
        budgets = {r["category"]: r["limit_amount"] for r in c.execute(
            "SELECT c.name AS category, b.limit_amount FROM budgets b JOIN categories c ON c.id = b.category_id WHERE b.month = %s", (m0,)).fetchall()}
        prefs = get_prefs(c)
    for r in rows:
        found = merchants.match(r["payee"], r["description"], user=patterns)
        r["merchant"] = found.name if found else None
        r["grp"] = found.group if found else None
    ctx = Context(today=today, m0=m0, rows=rows, first_data=first, rules=rules, budgets=budgets, prefs=prefs, patterns=patterns)
    ctx.spend = [r for r in rows if r["direction"] == "debit" and r["kind"] == "expense"]
    return ctx


def _in_scope(row: dict, scope: str, target: str) -> bool:
    if scope == "total":
        return True
    if scope == "merchant":
        return row["merchant"] == target
    if scope == "group":
        return row["grp"] == target
    return row["category"] == target


def _label(scope: str, target: str) -> str:
    return "All spending" if scope == "total" else target


# ───────────────────────── caps ─────────────────────────

@dataclass
class Cap:
    scope: str
    target: str
    period: str
    cap: Decimal
    rule_id: int | None = None                     # None for an implicit budget cap


def active_caps(ctx: Context) -> list[Cap]:
    """Every cap that may fire now: your rules (not muted, not snoozed) and this month's budgets as category/month caps."""
    now = _clock()
    caps: list[Cap] = []
    explicit = {(r["scope"], r["target"], r["period"]) for r in ctx.rules}
    for r in ctx.rules:
        if r["muted"] or (r["snoozed_until"] and r["snoozed_until"] > now):
            continue
        caps.append(Cap(r["scope"], r["target"], r["period"], r["cap"], r["id"]))
    for category, limit in sorted(ctx.budgets.items()):
        if ("category", category, "month") not in explicit and not scope_quiet(ctx.prefs, f"category:{category}:month"):
            caps.append(Cap("category", category, "month", limit))
    return caps


def spent_in(ctx: Context, cap: Cap) -> Decimal:
    """What the cap's scope has spent in the cap's current period (the day, ISO week or month that holds today)."""
    start = period_start(cap.period, ctx.today)
    end = {"day": start, "week": start + timedelta(days=6), "month": start.replace(day=_days_in(start))}[cap.period]
    return sum((r["amount"] for r in ctx.spend if start <= r["txn_date"] <= end and _in_scope(r, cap.scope, cap.target)), Decimal(0))


def _cap_alert(ctx: Context, cap: Cap, spent: Decimal, threshold: int, txn_id: int | None) -> Alert:
    label = _label(cap.scope, cap.target)
    adj = {"day": "daily", "week": "weekly", "month": "monthly"}[cap.period]
    when = {"day": "today", "week": "this week", "month": "this month"}[cap.period]
    on = {"merchant": f"at {label}", "group": f"on {label}", "category": f"on {label}", "total": "in total"}[cap.scope]
    left = max(cap.cap - spent, Decimal(0))
    over = max(spent - cap.cap, Decimal(0))
    if threshold == 100:
        tail = f"That is {_rs(over)} over." if over > 0 else "Nothing is left."
    else:
        tail = f"{_rs(left)} is left."
    numbers: dict[str, Decimal] = {"spent": _whole(spent), "cap": _whole(cap.cap), "left": _whole(left), "threshold_pct": Decimal(threshold)}
    if over > 0 and threshold == 100:
        numbers["over"] = _whole(over)
    start = period_start(cap.period, ctx.today)
    return Alert(
        "cap", SEVERITY_FOR[threshold],
        f"{label}: {threshold}% of your {adj} cap",
        f"You have spent {_rs(spent)} of {_rs(cap.cap)} {on} {when}. {tail}",
        numbers,
        f"cap:{cap.scope}:{cap.target}:{cap.period}:{start.isoformat()}:{threshold}",
        f"{label}: {threshold}% of {adj} cap",
        f"{_rs(spent)} of {_rs(cap.cap)} spent {when}.",
        rule_id=cap.rule_id, txn_id=txn_id, scope_key=f"{cap.scope}:{cap.target}:{cap.period}", threshold=threshold)


def cap_alerts(ctx: Context, new: list[dict] | None = None) -> list[Alert]:
    """The highest cap step each cap has reached. With `new` (the sweep's fresh debits) only caps that one of them touches are
    looked at, so an old, untouched cap does not speak up because of an unrelated payment. 49 -> 50 fires 50; 79 -> 81 fires 80;
    40 -> 85 fires only 80. The dedupe key holds the step, so a step fires once per period."""
    found: list[Alert] = []
    for cap in active_caps(ctx):
        start = period_start(cap.period, ctx.today)
        touching = None
        if new is not None:
            touching = [r for r in new if r["txn_date"] >= start and _in_scope(r, cap.scope, cap.target)]
            if not touching:
                continue
        spent = spent_in(ctx, cap)
        reached = [t for t in THRESHOLDS if spent * 100 >= cap.cap * t]
        if not reached:
            continue
        last = touching[-1]["id"] if touching else None
        found.append(_cap_alert(ctx, cap, spent, max(reached), last))
    return found


# ───────────────────────── streaks ─────────────────────────

def scope_quiet(prefs: dict, scope_key: str) -> bool:
    """A scope you muted, or snoozed and the time has not come yet. Used for alerts that have no rule row (budgets, streaks)."""
    return scope_key in (prefs.get("muted_scopes") or []) or _still_snoozed((prefs.get("snoozed_scopes") or {}).get(scope_key))


def _still_snoozed(until: str | None) -> bool:
    if not until:
        return False
    moment = datetime.fromisoformat(until)
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment > _clock()


def streak_alerts(ctx: Context, new: list[dict] | None = None) -> list[Alert]:
    """'7th Swiggy order this week, ₹2,140 total.' The 5th, 7th and 10th order from one merchant in an ISO week. Only the highest
    tier reached fires."""
    start = week_start(ctx.today)
    names = {r["merchant"] for r in (new if new is not None else ctx.spend) if r["merchant"] and r["txn_date"] >= start}
    found = []
    for name in sorted(names):
        scope_key = f"streak:{name}"
        if scope_quiet(ctx.prefs, scope_key):
            continue
        rows = [r for r in ctx.spend if r["merchant"] == name and start <= r["txn_date"] <= start + timedelta(days=6)]
        tiers = [t for t in STREAK_TIERS if len(rows) >= t]
        if not tiers:
            continue
        n, total = len(rows), sum((r["amount"] for r in rows), Decimal(0))
        found.append(Alert(
            "streak", "act" if max(tiers) >= STREAK_TIERS[-1] else "watch",
            f"{_ordinal(n)} {name} order this week, {_rs(total)} in all",
            f"That is {n} {name} orders since Monday, {_rs(total)} in total. Small orders add up fast.",
            {"order_count": Decimal(n), "week_total": _whole(total)},
            f"streak:{name}:{start.isoformat()}:{max(tiers)}",
            f"{_ordinal(n)} {name} order this week", f"{_rs(total)} so far this week.",
            txn_id=rows[-1]["id"], scope_key=scope_key))
    return found


# ───────────────────────── income, projection, safe to spend ─────────────────────────

def income(ctx: Context, *, ignore_manual: bool = False) -> dict:
    """{'amount', 'source', 'month'}. Precedence: what you typed > salary credits of the latest month within 45 days > nothing.
    `ignore_manual` shows what would be detected from salary alone (the Delivery tab prints it next to the box you type in)."""
    manual = None if ignore_manual else ctx.prefs.get("monthly_income")
    if manual:
        return {"amount": parse_amount(manual), "source": "manual", "month": None}
    since = ctx.today - timedelta(days=INCOME_LOOKBACK_DAYS)
    credits = [r for r in ctx.rows if r["direction"] == "credit" and r["category"] == SALARY_CATEGORY and r["txn_date"] >= since]
    if not credits:
        return {"amount": None, "source": None, "month": None}
    latest = max(r["txn_date"].replace(day=1) for r in credits)
    total = sum((r["amount"] for r in credits if r["txn_date"].replace(day=1) == latest), Decimal(0))
    return {"amount": total, "source": "salary", "month": latest}


def projection(ctx: Context) -> dict | None:
    """Month to date x days in month / day. None before day 5. 'pct_of_income' is a one-decimal Decimal when the income is known."""
    day = ctx.today.day
    if day < MIN_PROJECTION_DAY:
        return None
    spent = sum((r["amount"] for r in ctx.spend if r["txn_date"] >= ctx.m0), Decimal(0))
    days = _days_in(ctx.m0)
    projected = (spent * days / day).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    known = income(ctx)["amount"]
    pct = (projected * 100 / known).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if known else None
    return {"spent": spent, "day": day, "days_in_month": days, "projected": projected, "income": known, "pct_of_income": pct}


def _usual_day(rows: list[dict]) -> int:
    return int(median(r["txn_date"].day for r in rows[-3:]))


def committed(ctx: Context) -> list[dict]:
    """Recurring payments that insights.find_recurring sees, whose usual day is today or later and that are not paid yet this
    month: [{'name', 'amount', 'day'}]. This is only what Rectifi can detect; it says so on the page."""
    items = []
    for rec in insights.find_recurring(ctx):
        rows = [r for r in ctx.spend if (k := insights.payee_key(r["payee"], r["description"])) and k[0] == rec.key]
        if not rows or any(r["txn_date"] >= ctx.m0 for r in rows):
            continue
        day = _usual_day(rows)
        if day >= ctx.today.day:
            items.append({"name": rec.name, "amount": rec.amount, "day": day})
    return items


def safe_to_spend(ctx: Context) -> dict | None:
    """What you can spend per day for the rest of the month, with every input so the page can show the sum.

    spendable   your spend plan, else the sum of this month's budgets, else your monthly income, else None (then this is None)
    daily       floor((spendable - spent before today - committed) / days left including today), never below 0
    left_today  daily - spent today (can be negative: you are over)"""
    if ctx.prefs.get("spend_plan"):
        spendable, source = parse_amount(ctx.prefs["spend_plan"]), "plan"
    elif ctx.budgets:
        spendable, source = sum(ctx.budgets.values(), Decimal(0)), "budgets"
    elif (known := income(ctx)["amount"]):
        spendable, source = known, "income"
    else:
        return None
    before = sum((r["amount"] for r in ctx.spend if ctx.m0 <= r["txn_date"] < ctx.today), Decimal(0))
    today_spent = sum((r["amount"] for r in ctx.spend if r["txn_date"] == ctx.today), Decimal(0))
    items = committed(ctx)
    commit = sum((i["amount"] for i in items), Decimal(0))
    days_left = _days_in(ctx.m0) - ctx.today.day + 1
    daily = max(Decimal(0), ((spendable - before - commit) / days_left).to_integral_value(rounding=ROUND_FLOOR))
    return {"spendable": spendable, "source": source, "spent_before_today": before, "committed": commit, "committed_items": items,
            "days_left": days_left, "daily": daily, "spent_today": today_spent, "left_today": daily - today_spent,
            "over_today": today_spent > daily}


# ───────────────────────── presets ─────────────────────────

def presets(ctx: Context) -> list[dict]:
    """For each merchant, group and category you spent on in the last 3 complete months: the average per day, week and month,
    a 'keep' cap (the average rounded up to ₹100) and a 'trim' cap (80% of it, rounded up to ₹100). The buttons on the Caps tab."""
    window_end = ctx.m0 - timedelta(days=1)
    window_start = _shift(ctx.m0, PRESET_MONTHS)
    if ctx.first_data is None:
        return []
    begin = max(window_start, ctx.first_data)
    if begin > window_end:
        return []
    days = (window_end - begin).days + 1
    months = max(1, (window_end.year * 12 + window_end.month) - (begin.year * 12 + begin.month) + 1)
    totals: dict[tuple[str, str], Decimal] = {}
    for r in ctx.spend:
        if not begin <= r["txn_date"] <= window_end:
            continue
        keys = [("category", r["category"])]
        if r["merchant"]:
            keys += [("merchant", r["merchant"]), ("group", r["grp"])]
        for key in keys:
            totals[key] = totals.get(key, Decimal(0)) + r["amount"]
    out = []
    for (scope, target), total in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])):
        avg = {"day": total / days, "week": total * 7 / days, "month": total / months}
        out.append({
            "scope": scope, "target": target, "months": months,
            "average": {p: _whole(v) for p, v in avg.items()},
            "keep": {p: _ceil_to(v) for p, v in avg.items()},
            "trim": {p: _ceil_to(v * PRESET_TRIM) for p, v in avg.items()},
        })
    return out


# ───────────────────────── the leak report (waterfall) ─────────────────────────

SEGMENT_LABELS = {"fixed": "Fixed bills", "big": "Big one-offs", "quick": "Quick commerce", "food": "Food delivery",
                  "shopping": "Shopping", "other": "Other small spends"}
_GROUP_SEGMENT = {merchants.QUICK: "quick", merchants.FOOD: "food", merchants.SHOPPING: "shopping"}


def leak_report(ctx: Context, month: date | None = None) -> dict:
    """Where one month's money went: Income, then each outflow, then what is Left.

    Every spend of the month lands in exactly one segment, in this order: Rent / EMI categories or a detected recurring bill
    ('fixed'), a Quick commerce / Food delivery / Shopping merchant, a payment of ₹1,000 or more ('big'), everything else
    ('other'). Start and end of each bar are worked out here, so the page only draws. income - sum(outflows) = left, exactly."""
    first = (month or ctx.m0).replace(day=1)
    last = first.replace(day=_days_in(first))
    rows = [r for r in ctx.spend if first <= r["txn_date"] <= last]
    recurring_keys = {r.key for r in insights.find_recurring(ctx)}
    amounts = {key: Decimal(0) for key in SEGMENT_LABELS}
    counts = {key: 0 for key in SEGMENT_LABELS}
    for r in rows:
        k = insights.payee_key(r["payee"], r["description"])
        if r["category"] in FIXED_CATEGORIES:
            seg = "fixed"
        elif r["grp"] in _GROUP_SEGMENT:
            seg = _GROUP_SEGMENT[r["grp"]]
        elif k and k[0] in recurring_keys:
            seg = "fixed"
        elif r["amount"] >= BIG_ONE_OFF:
            seg = "big"
        else:
            seg = "other"
        amounts[seg] += r["amount"]
        counts[seg] += 1
    spent = sum(amounts.values(), Decimal(0))
    manual = ctx.prefs.get("monthly_income")
    credits = sum((r["amount"] for r in ctx.rows if r["direction"] == "credit" and r["kind"] == "income" and first <= r["txn_date"] <= last), Decimal(0))
    if manual:
        total_in, source = parse_amount(manual), "manual"
    elif credits > 0:
        total_in, source = credits, "credits"
    else:
        total_in, source = None, None
    segments = []
    top = total_in if total_in is not None else Decimal(0)
    if total_in is not None:
        segments.append({"key": "income", "label": "Income", "kind": "income", "amount": total_in, "start": Decimal(0), "end": total_in, "count": 0})
    level = top
    for key in ("fixed", "big", "quick", "food", "shopping", "other"):
        end = level - amounts[key]
        segments.append({"key": key, "label": SEGMENT_LABELS[key], "kind": "outflow", "amount": amounts[key], "start": level, "end": end, "count": counts[key]})
        level = end
    left = (total_in - spent) if total_in is not None else None
    if left is not None:
        segments.append({"key": "left", "label": "Left" if left >= 0 else "Overspent", "kind": "left", "amount": left, "start": Decimal(0), "end": left, "count": 0})
    table: dict[str, dict] = {}
    for r in rows:
        if r["merchant"]:
            e = table.setdefault(r["merchant"], {"merchant": r["merchant"], "group": r["grp"], "orders": 0, "total": Decimal(0)})
            e["orders"] += 1
            e["total"] += r["amount"]
    merchants_table = sorted(table.values(), key=lambda e: (-e["total"], e["merchant"]))
    for e in merchants_table:
        e["pct_of_income"] = (e["total"] * 100 / total_in).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if total_in else None
    return {"month": first, "income": total_in, "income_source": source, "spent": spent, "left": left, "segments": segments,
            "merchants": merchants_table, "projection": projection(ctx) if first == ctx.m0 else None}


# ───────────────────────── the Today strip ─────────────────────────

def today_view(ctx: Context) -> dict:
    small = [{"txn_id": r["id"], "label": r["merchant"] or merchants.payee_label(r["payee"], r["description"]), "group": r["grp"], "amount": r["amount"]}
             for r in ctx.spend if r["txn_date"] == ctx.today and r["amount"] < SMALL_SPEND_MAX]
    with connect() as conn:
        unread = conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE read_at IS NULL").fetchone()["n"]
    return {"safe": safe_to_spend(ctx), "small_spends": small, "unread": unread, "today": ctx.today}


# ───────────────────────── clock (one place to fake in tests) ─────────────────────────

def _clock() -> datetime:
    """Now, as an aware datetime in the laptop's local zone. Tests replace this one function."""
    return datetime.now().astimezone()


def _now_naive() -> datetime:
    return _clock().replace(tzinfo=None)


# ═════════════════════════ Part 2: side effects ═════════════════════════
# Everything below writes: the sweep (new debits -> alerts), delivery (Web Push), the 60-second tick, and the inbox.

import re  # noqa: E402
import threading  # noqa: E402

from . import tools, webpush  # noqa: E402

WATERMARK_KEY = "alerts.watermark"
BASELINE_AGE_MINUTES = 10
_RANK = {"act": 0, "watch": 1, "info": 2}
_DIGITS = re.compile(r"(?<![₹\d,.])\d{4,}")
_HANDLE = re.compile(r"@\S+")
PRIVATE_TITLE, PRIVATE_BODY = "Rectifi", "New spending alert"


def _numbers_json(numbers: dict) -> str:
    return json.dumps({k: str(v) for k, v in numbers.items()})


def _numbers_back(raw) -> dict[str, Decimal]:
    return {k: Decimal(str(v)) for k, v in (raw or {}).items()}


def _safe_text(text: str) -> str:
    """Last defence for text that leaves the laptop: no @handle and no run of 4+ digits (a UPI ref, a card or phone number).
    A rupee amount is kept: a digit run right after the rupee sign is allowed."""
    return " ".join(_DIGITS.sub("…", _HANDLE.sub("", text)).split())


# ───────────────────────── the watermark ─────────────────────────

def _read_watermark(conn) -> int | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (WATERMARK_KEY,)).fetchone()
    return int(row["value"]["txn_id"]) if row else None


def _write_watermark(conn, txn_id: int) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES (%s, %s::jsonb) ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                 (WATERMARK_KEY, json.dumps({"txn_id": int(txn_id)})))


def _baseline(conn) -> int:
    """The first sweep ever: payments that were already here (dated before the real-time window, or stored a while ago) are history, not news."""
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS top FROM transactions WHERE txn_date < %s OR created_at < %s",
                       (tools.today() - timedelta(days=REALTIME_DAYS), _clock() - timedelta(minutes=BASELINE_AGE_MINUTES))).fetchone()
    return row["top"]


def ensure_watermark(conn=None) -> int:
    with _use(conn) as c:
        mark = _read_watermark(c)
        if mark is None:
            mark = _baseline(c)
            _write_watermark(c, mark)
        return mark


# ───────────────────────── push gates ─────────────────────────

def in_quiet_hours(now: datetime, prefs: dict) -> bool:
    start, end = time.fromisoformat(prefs["quiet_start"]), time.fromisoformat(prefs["quiet_end"])
    t = now.time().replace(second=0, microsecond=0)
    if start == end:
        return False
    return start <= t < end if start < end else (t >= start or t < end)


def _pushed_since(conn, cutoff: datetime) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM alerts "
                        "WHERE push_state IN ('pending', 'sending', 'sent') AND COALESCE(pushed_at, created_at) >= %s", (cutoff,)).fetchone()["n"]


def _gate(conn, a: Alert, prefs: dict, now: datetime) -> str:
    """The state a fresh alert gets, by these gates in this order: push off, snooze, muted kind (skipped); quiet hours (held, sent
    later as one push); a cooldown for the same scope (a 100% alert skips it); the hourly and daily limit (skipped).
    A skipped alert is still in the inbox and the digest."""
    if not prefs["push_on"] or _still_snoozed(prefs["snooze_until"]) or a.kind in prefs["muted_kinds"]:
        return "skipped"
    if in_quiet_hours(now.replace(tzinfo=None), prefs):
        return "held"
    if a.scope_key and a.threshold != 100 and prefs["cooldown_hours"]:
        recent = conn.execute("SELECT 1 FROM alerts WHERE scope_key = %s AND push_state IN ('pending', 'sending', 'sent') "
                              "AND COALESCE(pushed_at, created_at) >= %s LIMIT 1", (a.scope_key, now - timedelta(hours=prefs["cooldown_hours"]))).fetchone()
        if recent:
            return "skipped"
    if _pushed_since(conn, now - timedelta(hours=1)) >= prefs["max_per_hour"] or _pushed_since(conn, now - timedelta(hours=24)) >= prefs["max_per_day"]:
        return "skipped"
    return "pending"


def _insert(conn, a: Alert, state: str, now: datetime) -> int | None:
    row = conn.execute(
        """INSERT INTO alerts (dedupe_key, kind, severity, title, detail, push_title, push_body, numbers, rule_id, txn_id, scope_key, created_at, push_state)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
           ON CONFLICT (user_id, dedupe_key) DO NOTHING RETURNING id""",
        (a.dedupe_key, a.kind, a.severity, a.title, a.detail, a.push_title, a.push_body, _numbers_json(a.numbers),
         a.rule_id, a.txn_id, a.scope_key, now, state)).fetchone()
    return row["id"] if row else None


# ───────────────────────── the sweep ─────────────────────────

def _bulk_alert(debits: list[dict]) -> Alert:
    batches = {r["import_batch_id"] for r in debits}
    ids = [r["id"] for r in debits]
    total = sum((r["amount"] for r in debits), Decimal(0))
    word = "payment" if len(debits) == 1 else "payments"
    if len(batches) == 1 and None not in batches:
        key, title = f"import:{batches.pop()}", f"{len(debits)} {word} imported"
    else:
        key, title = f"bulk:{min(ids)}-{max(ids)}", f"{len(debits)} {word} added at once"
    return Alert(
        "bulk", "info", title,
        f"{len(debits)} {word} came in together, {_rs(total)} in all. Rectifi did not send an alert for each one. "
        "Open Leaks to see where the money went.",
        {"payment_count": Decimal(len(debits)), "batch_total": _whole(total)}, key,
        title, f"{_rs(total)} in total.", txn_id=max(ids))


def sweep(origin: str = "tick") -> dict:
    """Look at debits newer than the watermark and write the alerts they cause. Returns {'new': alerts written, 'mode': ...}.

    mode 'realtime': normal alerts (caps, streaks), pushed through the gates.
    mode 'bulk': more than 5 debits, or any came from an import, or any is dated more than 2 days back. ONE summary alert is
                 pushed; the cap and streak alerts are still written (so the same step never fires again) but not pushed.
    mode 'idle' / 'busy': nothing new / another process is sweeping right now.
    One Postgres advisory lock makes the gateway process and the web app process take turns; alerts.dedupe_key is UNIQUE, so even
    a repeat can never write twice."""
    with connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (SWEEP_LOCK_ID,)).fetchone()["ok"]:
            return {"new": 0, "mode": "busy"}
        mark = ensure_watermark(conn)
        fresh = conn.execute("SELECT t.id, t.direction, t.txn_date, t.import_batch_id, t.amount, c.kind FROM transactions t "
                             "JOIN categories c ON c.id = t.category_id WHERE t.id > %s ORDER BY t.id", (mark,)).fetchall()
        if not fresh:
            return {"new": 0, "mode": "idle"}
        top = fresh[-1]["id"]
        debits = [r for r in fresh if r["direction"] == "debit" and r["kind"] == "expense"]
        if not debits:
            _write_watermark(conn, top)
            return {"new": 0, "mode": "idle"}
        ctx = build_context(conn=conn)
        new_ids = {r["id"] for r in debits}
        new = [r for r in ctx.spend if r["id"] in new_ids]
        bulk = (len(debits) > BULK_MIN or any(r["import_batch_id"] for r in debits)
                or any(r["txn_date"] < ctx.today - timedelta(days=REALTIME_DAYS) for r in debits))
        found = cap_alerts(ctx, new) + streak_alerts(ctx, new)
        found.sort(key=lambda a: _RANK[a.severity])
        now = _clock()
        written = 0
        if bulk:
            for a in found:
                written += _insert(conn, a, "skipped", now) is not None
            summary = _bulk_alert(debits)
            written += _insert(conn, summary, _gate(conn, summary, ctx.prefs, now), now) is not None
        else:
            for a in found:
                written += _insert(conn, a, _gate(conn, a, ctx.prefs, now), now) is not None
        _write_watermark(conn, top)
    log.info("alerts sweep (%s): %s alert(s), mode %s", origin, written, "bulk" if bulk else "realtime")
    return {"new": written, "mode": "bulk" if bulk else "realtime"}


# ───────────────────────── delivery ─────────────────────────

def _push_all(title: str, body: str, url: str, tag: str | None, urgency: str = "normal", client=None) -> dict:
    """Send one notification to every saved phone. Returns {'devices', 'ok', 'gone', 'retry'}; removes phones that are gone."""
    with connect() as conn:
        subs = conn.execute("SELECT id, endpoint, p256dh, auth FROM push_subscriptions ORDER BY id").fetchall()
    result = {"devices": len(subs), "ok": 0, "gone": 0, "retry": 0}
    if not subs:
        return result
    payload = webpush.build_payload(_safe_text(title), _safe_text(body), url, _safe_text(tag) if tag else None)
    outcome: dict[int, str] = {}
    for sub in subs:
        verdict = webpush.send(sub, payload, ttl=PUSH_TTL_SECONDS, urgency=urgency, client=client)
        outcome[sub["id"]] = "ok" if verdict == "ok" else ("gone" if verdict == "gone" else "retry")
    for verdict in outcome.values():
        result[verdict] += 1
    with connect() as conn:
        for sub_id, verdict in outcome.items():
            if verdict == "gone":
                conn.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub_id,))
            elif verdict == "ok":
                conn.execute("UPDATE push_subscriptions SET last_ok_at = %s, fail_count = 0 WHERE id = %s", (_clock(), sub_id))
            else:
                conn.execute("UPDATE push_subscriptions SET fail_count = fail_count + 1 WHERE id = %s", (sub_id,))
    return result


def _texts(title: str, body: str, prefs: dict) -> tuple[str, str]:
    return (PRIVATE_TITLE, PRIVATE_BODY) if prefs["private_notifications"] else (title, body)


def deliver(client=None) -> dict:
    """Push every 'pending' alert. Rows are claimed with FOR UPDATE SKIP LOCKED, so two processes never send the same one."""
    prefs = get_prefs()
    with connect() as conn:
        claimed = conn.execute(
            """UPDATE alerts SET push_state = 'sending', push_attempts = push_attempts + 1
               WHERE id IN (SELECT id FROM alerts WHERE push_state = 'pending' ORDER BY id FOR UPDATE SKIP LOCKED)
               RETURNING id, severity, push_title, push_body, scope_key, push_attempts""").fetchall()
    sent = failed = skipped = 0
    for row in sorted(claimed, key=lambda r: r["id"]):
        title, body = _texts(row["push_title"], row["push_body"], prefs)
        try:
            result = _push_all(title, body, f"/alerts#{row['id']}", row["scope_key"] or f"alert-{row['id']}",
                               "high" if row["severity"] == "act" else "normal", client)
        except Exception as exc:                               # noqa: BLE001  (the row must not be left 'sending')
            log.error("push failed (%s)", type(exc).__name__)
            result = {"devices": 1, "ok": 0, "gone": 0, "retry": 1}
        now = _clock()
        with connect() as conn:
            if result["ok"]:
                conn.execute("UPDATE alerts SET push_state = 'sent', pushed_at = %s WHERE id = %s", (now, row["id"]))
                sent += 1
            elif result["retry"] and row["push_attempts"] < RETRY_MAX:
                wait = RETRY_BACKOFF_MINUTES[min(row["push_attempts"] - 1, len(RETRY_BACKOFF_MINUTES) - 1)]
                conn.execute("UPDATE alerts SET push_state = 'failed', next_try_at = %s WHERE id = %s", (now + timedelta(minutes=wait), row["id"]))
                failed += 1
            elif result["retry"]:
                conn.execute("UPDATE alerts SET push_state = 'failed', next_try_at = NULL WHERE id = %s", (row["id"],))
                failed += 1
            else:                                              # no phone to tell (none saved, or all gone)
                conn.execute("UPDATE alerts SET push_state = 'skipped' WHERE id = %s", (row["id"],))
                skipped += 1
    return {"sent": sent, "failed": failed, "skipped": skipped}


def sweep_and_deliver(origin: str = "tick") -> dict:
    """What the real-time paths call (an SMS arrived, you added a payment, an import finished). Never raises: a problem here must
    not turn a stored payment into an error for the phone. Only the type of an error is logged."""
    try:
        result = sweep(origin)
        if result["mode"] in ("realtime", "bulk"):
            deliver()
        return result
    except Exception as exc:                                    # noqa: BLE001
        log.error("alerts sweep failed (%s)", type(exc).__name__)
        return {"new": 0, "mode": "error"}


def kick(origin: str) -> threading.Thread | None:
    """Run sweep_and_deliver in a short background thread (for the web app's own routes, which should not wait for the network).
    RECTIFI_NO_ALERT_BACKGROUND=1 turns this and the 60-second ticker off (the test suite sets it)."""
    if os.getenv("RECTIFI_NO_ALERT_BACKGROUND"):
        return None
    thread = threading.Thread(target=sweep_and_deliver, args=(origin,), name=f"alerts-{origin}", daemon=True)
    thread.start()
    return thread


# ───────────────────────── the tick ─────────────────────────

def _release_held(prefs: dict, now: datetime, client=None) -> int:
    """After quiet hours: ONE push for everything held ('While you were asleep: 3 alerts'). Returns how many alerts it covered."""
    if _still_snoozed(prefs["snooze_until"]) or in_quiet_hours(now.replace(tzinfo=None), prefs):
        return 0
    with connect() as conn:
        if not prefs["push_on"]:
            conn.execute("UPDATE alerts SET push_state = 'skipped' WHERE push_state = 'held'")
            return 0
        held = conn.execute("SELECT id, severity, push_title, push_body, scope_key FROM alerts WHERE push_state = 'held' ORDER BY id "
                            "FOR UPDATE SKIP LOCKED").fetchall()
        if not held:
            return 0
        conn.execute("UPDATE alerts SET push_state = 'sending', push_attempts = push_attempts + 1 WHERE id = ANY(%s)", ([h["id"] for h in held],))
    if len(held) == 1:
        one = held[0]
        title, body = _texts(one["push_title"], one["push_body"], prefs)
        url, tag = f"/alerts#{one['id']}", one["scope_key"]
    else:
        top = sorted(held, key=lambda h: _RANK[h["severity"]])[0]
        title, body = _texts(f"While you were asleep: {len(held)} alerts", top["push_title"], prefs)
        if prefs["private_notifications"]:
            body = f"{len(held)} new spending alerts"
        url, tag = "/alerts", "held"
    try:
        result = _push_all(title, body, url, tag, client=client)
    except Exception as exc:                                   # noqa: BLE001
        log.error("push failed (%s)", type(exc).__name__)
        result = {"devices": 1, "ok": 0, "gone": 0, "retry": 1}
    ids = [h["id"] for h in held]
    with connect() as conn:
        if result["ok"]:
            conn.execute("UPDATE alerts SET push_state = 'sent', pushed_at = %s WHERE id = ANY(%s)", (now, ids))
        elif result["retry"]:
            conn.execute("UPDATE alerts SET push_state = 'failed', next_try_at = %s WHERE id = ANY(%s)", (now + timedelta(minutes=RETRY_BACKOFF_MINUTES[0]), ids))
        else:
            conn.execute("UPDATE alerts SET push_state = 'skipped' WHERE id = ANY(%s)", (ids,))
    return len(held)


def _digest(prefs: dict, now: datetime) -> int:
    """Once a day, after digest_time: today's spending in one alert. Returns 1 when a new digest was written."""
    if not prefs["digest_on"] or now.time() < time.fromisoformat(prefs["digest_time"]):
        return 0
    ctx = build_context()
    spent = [r for r in ctx.spend if r["txn_date"] == ctx.today]
    if not spent:
        return 0
    total = sum((r["amount"] for r in spent), Decimal(0))
    safe = safe_to_spend(ctx)
    numbers: dict[str, Decimal] = {"payment_count": Decimal(len(spent)), "day_total": _whole(total)}
    word = "payment" if len(spent) == 1 else "payments"
    tail = ""
    if safe:
        if safe["left_today"] >= 0:
            numbers["safe_left"] = _whole(safe["left_today"])
            tail = f" {_rs(safe['left_today'])} was left for today."
        else:
            numbers["safe_over"] = _whole(-safe["left_today"])
            tail = f" That is {_rs(-safe['left_today'])} more than the safe amount for today."
    a = Alert("digest", "info", f"Today you spent {_rs(total)}", f"{len(spent)} {word} today, {_rs(total)} in total.{tail}", numbers,
              f"digest:{ctx.today.isoformat()}", "Today's spending", f"{_rs(total)} across {len(spent)} {word}.")
    with connect() as conn:
        written = _insert(conn, a, _gate(conn, a, prefs, now), now) is not None
        if written:
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('alerts.digest_last', %s::jsonb) ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                         (json.dumps({"date": ctx.today.isoformat()}),))
    return int(written)


def tick(client=None) -> dict:
    """Run every minute by the web app: sweep, release held alerts, the digest, retries. Safe to call from two processes."""
    now = _clock()
    prefs = get_prefs()
    out = {"sweep": sweep("tick"), "released": 0, "digest": 0, "retried": 0}
    out["digest"] = _digest(prefs, now)
    with connect() as conn:
        got = conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (TICK_LOCK_ID,)).fetchone()["ok"]
        if not got:
            return out
        out["retried"] = conn.execute("UPDATE alerts SET push_state = 'pending' WHERE push_state = 'failed' AND push_attempts < %s "
                                      "AND next_try_at IS NOT NULL AND next_try_at <= %s", (RETRY_MAX, now)).rowcount
    out["released"] = _release_held(prefs, now, client)
    deliver(client)
    return out


# ───────────────────────── the inbox ─────────────────────────

ALERT_COLUMNS = ("id, kind, severity, title, detail, numbers, rule_id, txn_id, scope_key, created_at, read_at, push_state")


def _alert_row(row: dict) -> dict:
    row["numbers"] = _numbers_back(row["numbers"])
    return row


def list_alerts(status: str = "unread", limit: int = 100) -> list[dict]:
    if status not in ("unread", "all"):
        raise RectifiError("status must be 'unread' or 'all'.")
    where = "WHERE read_at IS NULL" if status == "unread" else ""
    with connect() as conn:
        rows = conn.execute(f"SELECT {ALERT_COLUMNS} FROM alerts {where} ORDER BY created_at DESC, id DESC LIMIT %s", (max(1, min(limit, 500)),)).fetchall()
    return [_alert_row(r) for r in rows]


def get_alert(alert_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(f"SELECT {ALERT_COLUMNS} FROM alerts WHERE id = %s", (alert_id,)).fetchone()
    if row is None:
        raise RectifiError("No such alert.")
    return _alert_row(row)


def unread_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE read_at IS NULL").fetchone()["n"]


def mark_read(alert_id: int) -> dict:
    with connect() as conn:
        conn.execute("UPDATE alerts SET read_at = COALESCE(read_at, %s) WHERE id = %s", (_clock(), alert_id))
    return get_alert(alert_id)


def read_all() -> int:
    with connect() as conn:
        return conn.execute("UPDATE alerts SET read_at = %s WHERE read_at IS NULL", (_clock(),)).rowcount


def mute_alert(alert_id: int) -> dict:
    """Stop this kind of alert. An alert from a cap rule mutes that rule. Any other (a budget, a streak) mutes its scope."""
    row = get_alert(alert_id)
    if row["rule_id"] is not None:
        update_rule(row["rule_id"], muted=True)
        return {"muted": "rule", "rule_id": row["rule_id"]}
    prefs = get_prefs()
    if row["scope_key"]:
        prefs["muted_scopes"] = sorted(set(prefs["muted_scopes"]) | {row["scope_key"]})
    else:
        prefs["muted_kinds"] = sorted(set(prefs["muted_kinds"]) | {row["kind"]})
    _write_prefs(prefs)
    return {"muted": "scope" if row["scope_key"] else "kind"}


def snooze_alert(alert_id: int, hours: int) -> dict:
    if hours not in (1, 24, 168):
        raise RectifiError("Snooze for 1 hour, 1 day or 1 week.")
    row = get_alert(alert_id)
    until = _clock() + timedelta(hours=hours)
    if row["rule_id"] is not None:
        update_rule(row["rule_id"], snoozed_until=until)
        return {"snoozed": "rule", "until": until}
    if not row["scope_key"]:
        raise RectifiError("This kind of alert can't be snoozed. You can mute it instead.")
    prefs = get_prefs()
    prefs["snoozed_scopes"] = {**prefs["snoozed_scopes"], row["scope_key"]: until.isoformat()}
    _write_prefs(prefs)
    return {"snoozed": "scope", "until": until}


def as_insight(row: dict) -> insights.Insight:
    """An alert as an Insight, so the optional 'Explain this' (agent/explain.py) can check its reply against the alert's own numbers."""
    return insights.Insight(row["kind"], row["severity"], row["title"], row["detail"], row["numbers"], key=f"alert:{row['id']}")


# ───────────────────────── phones ─────────────────────────

def add_subscription(endpoint: str, p256dh: str, auth: str, label: str | None = None) -> dict:
    if not webpush.allowed_endpoint(endpoint):
        raise RectifiError("That address is not a push service Rectifi can use.")
    for name, value in (("p256dh", p256dh), ("auth", auth)):
        try:
            raw = webpush.b64u_decode(value)
        except Exception as exc:
            raise RectifiError(f"The subscription's {name} key is not valid.") from exc
        if len(raw) != (65 if name == "p256dh" else 16):
            raise RectifiError(f"The subscription's {name} key is not valid.")
    label = (label or "").strip()[:60] or None
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth, label) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id, endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth, label = COALESCE(EXCLUDED.label, push_subscriptions.label), fail_count = 0 "
            "RETURNING id", (endpoint, p256dh, auth, label)).fetchone()
    return get_subscription(row["id"])


def _public_sub(row: dict) -> dict:
    from urllib.parse import urlsplit

    return {"id": row["id"], "label": row["label"], "service": urlsplit(row["endpoint"]).hostname, "created_at": row["created_at"], "last_ok_at": row["last_ok_at"]}


def get_subscription(sub_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT id, endpoint, label, created_at, last_ok_at FROM push_subscriptions WHERE id = %s", (sub_id,)).fetchone()
    if row is None:
        raise RectifiError("No such phone.")
    return _public_sub(row)


def list_subscriptions() -> list[dict]:
    """Only id, label, the push service's host, when added and when it last worked. The endpoint is a secret and never leaves."""
    with connect() as conn:
        return [_public_sub(r) for r in conn.execute("SELECT id, endpoint, label, created_at, last_ok_at FROM push_subscriptions ORDER BY id").fetchall()]


def delete_subscription(sub_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub_id,)).rowcount > 0


def send_test(client=None) -> dict:
    """'Send test': a harmless notification to every saved phone, so you can see it works. Not an alert; nothing is stored."""
    return _push_all("Rectifi test", "Phone alerts are working.", "/alerts", "test", client=client)
