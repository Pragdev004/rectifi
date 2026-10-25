"""Insights: what Rectifi notices in your own numbers and what to do about it. 100% deterministic.

Fixed rules, SQL and Decimal. No model reads your data to produce an insight, and nothing in this file talks to one
(a test checks that). Every ₹ figure in a title or detail is a whole-rupee Decimal that is also in `Insight.numbers`,
so anyone who repeats an insight (the Insights page, the agent, an optional "Explain this") can be checked against it.

The rules, in the order the page ranks them within one severity:
  data quality   unlinked entries, no bank account, "Miscellaneous" pile, accounts not checked against the bank, a balance gap
  budget pace    month to date x days in month / day of month, per budget (not before day 5)
  spike          this month vs your usual month (average of the last 3 months that have data)
  recurring      the same payee at a similar amount every month: monthly and yearly cost, new ones, price rises
  large payment  a recent payment above max(3 x your median payment, 2,000)
  savings rate   the last 3 complete months: a drop of 10 points or more, or a rise
  unbudgeted     big spending categories without a budget, with a suggested limit (you press a button to apply it)
  small leaks    many payments under 200 in one category that add up
Data quality always comes first, because a wrong or missing record undermines every other number.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from . import tools
from .categorize import FALLBACK_EXPENSE
from .db import connect
from .money import fmt_inr

# ───────────────────────── tunables (every threshold in one place) ─────────────────────────

MIN_PACE_DAY = 5                      # no month projection before this day: too noisy
MIN_PACE_TXNS = 3                     # a category with fewer payments than this is lumpy (rent), not a pace
PACE_OVER = Decimal("1.05")           # projected above limit x 1.05 -> act
PACE_NEAR = Decimal("0.90")           # projected from limit x 0.90 up to limit x 1.05 -> watch
SPIKE_PCT = Decimal("30")             # at least 30% above the usual month ...
SPIKE_MIN = Decimal("500")            # ... and at least 500 rupees above it
SPIKE_MIN_MONTHS = 2                  # needs this many earlier months with data
SIMILAR = Decimal("0.10")             # "similar amount" = within 10% of the median
RECURRING_WINDOW_DAYS = 150
MONTHLY_GAP = (25, 38)                # days between payments that still counts as monthly
ACTIVE_WITHIN_DAYS = 45               # a recurring payment must have been paid this recently, or it was cancelled
NEW_RECURRING_DAYS = 75               # a payee first seen this recently, when older history exists, is "new"
LARGE_MULTIPLE = Decimal("3")
LARGE_FLOOR = Decimal("2000")
LARGE_LOOKBACK_DAYS = 90
LARGE_RECENT_DAYS = 14
LARGE_MIN_PAYMENTS = 5                # a median of fewer payments means nothing
SAVINGS_CHANGE = Decimal("10")        # points
SMALL_PAYMENT = Decimal("200")
SMALL_MIN_COUNT = 8
SMALL_MIN_TOTAL = Decimal("1000")
SMALL_WINDOW_DAYS = 30
UNBUDGETED_MIN_AVERAGE = Decimal("500")
STALE_ACCOUNT_DAYS = 30
OTHER_WATCH_COUNT = 3
QUALITY_LOOKBACK_DAYS = 90
MAX_PER_RULE = 3

SEVERITIES = ("act", "watch", "info")
_RANK = {"act": 0, "watch": 1, "info": 2}
DATA_QUALITY_KINDS = ("no_account", "unlinked", "other_pile", "stale_account", "balance_gap")

# Keys in `numbers` that are not rupees (the "Explain this" prompt needs to know which are which).
_NON_MONEY_SUFFIXES = ("_count", "_pct", "_days", "_months", "_month", "_points", "day")


def is_money_key(key: str) -> bool:
    return not key.endswith(_NON_MONEY_SUFFIXES)


@dataclass
class Insight:
    kind: str
    severity: str                                   # info | watch | act
    title: str
    detail: str
    numbers: dict[str, Decimal]                     # every figure in title/detail; money keys are whole rupees
    action_label: str | None = None
    action_page: str | None = None                  # a page key of the app: ledger, budgets, accounts, add, review, insights
    key: str = ""                                   # stable id (for buttons)
    apply: dict | None = None                       # a one-click change the page may offer: {"tool": "set_budget", ...}

    def __post_init__(self) -> None:
        assert self.severity in SEVERITIES, self.severity


# ───────────────────────── small helpers ─────────────────────────

def _whole(value) -> Decimal:
    """A money value as whole rupees (the figure shown and stored in `numbers`)."""
    return Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def _rs(whole: Decimal) -> str:
    return fmt_inr(whole, paise=False)


def _shift_month(first: date, back: int) -> date:
    """The first day of the month `back` months before `first` (a first-of-month date)."""
    index = first.year * 12 + first.month - 1 - back
    return date(index // 12, index % 12 + 1, 1)


def _days_in(first: date) -> int:
    return calendar.monthrange(first.year, first.month)[1]


_STOP_WORDS = {"the", "mr", "mrs", "ms", "shri", "sri", "upi", "pos", "neft", "imps", "rtgs", "ach", "nach", "payment", "pay",
               "to", "from", "ref", "txn", "dr", "cr", "paytm", "ybl"}


def payee_key(payee: str | None, description: str | None = None) -> tuple[str, str] | None:
    """(key, display name) that groups payments to the same place: 'swiggy@icici' and 'Swiggy' are both 'swiggy'."""
    text = (payee or description or "").lower()
    text = text.split("@", 1)[0]
    words = [w for w in re.findall(r"[a-z]+", text) if len(w) >= 3 and w not in _STOP_WORDS and not set(w) <= {"x"}]
    if not words:
        return None
    return words[0], words[0].title()


@dataclass
class Context:
    today: date
    m0: date                                         # first day of this month
    rows: list[dict]                                 # every transaction since six months back
    spend: list[dict] = field(default_factory=list)  # the expense debits among them
    first_data: date | None = None                   # date of the oldest transaction in the whole database

    def month_of(self, d: date) -> date:
        return d.replace(day=1)


def _load(today: date) -> Context:
    m0 = today.replace(day=1)
    since = _shift_month(m0, 5)
    with connect() as conn:
        rows = conn.execute(
            """SELECT t.id, t.txn_date, t.amount, t.direction, t.payee, t.description, t.account_id,
                      c.name AS category, c.kind AS kind
               FROM transactions t JOIN categories c ON c.id = t.category_id
               WHERE t.txn_date BETWEEN %s AND %s ORDER BY t.txn_date, t.id""",
            (since, today),
        ).fetchall()
        first = conn.execute("SELECT MIN(txn_date) AS d FROM transactions").fetchone()["d"]
    ctx = Context(today=today, m0=m0, rows=rows, first_data=first)
    ctx.spend = [r for r in rows if r["direction"] == "debit" and r["kind"] == "expense"]
    return ctx


def data_span(today: date | None = None) -> dict:
    """How much data there is: {'count', 'first', 'last', 'days'}. The page uses it to say "need about a month"."""
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n, MIN(txn_date) AS first, MAX(txn_date) AS last FROM transactions").fetchone()
    days = (row["last"] - row["first"]).days + 1 if row["n"] else 0
    return {"count": row["n"], "first": row["first"], "last": row["last"], "days": days}


# ───────────────────────── data quality ─────────────────────────

def _data_quality(ctx: Context) -> list[Insight]:
    out: list[Insight] = []
    recent_from = ctx.today - timedelta(days=QUALITY_LOOKBACK_DAYS)
    recent = [r for r in ctx.rows if r["txn_date"] >= recent_from]
    accounts = tools.list_accounts()
    if not accounts and ctx.rows:
        out.append(Insight(
            "no_account", "act", "No bank account added yet",
            "Rectifi can't prove that nothing is missing until an account exists and its balance is recorded. "
            "Every other insight rests on complete records, so start here.",
            {}, "Add an account", "accounts", key="no_account"))
    unlinked = [r for r in recent if r["account_id"] is None]
    if unlinked and accounts:
        n = Decimal(len(unlinked))
        out.append(Insight(
            "unlinked", "act", f"{len(unlinked)} {'entry is' if len(unlinked) == 1 else 'entries are'} not linked to an account",
            f"In the last {QUALITY_LOOKBACK_DAYS} days, {len(unlinked)} {'entry has' if len(unlinked) == 1 else 'entries have'} "
            "no account, so no balance check can cover them. Link them in the Ledger, or import the statement: "
            "an import attaches the account to a matching hand entry.",
            {"unlinked_count": n, "lookback_days": Decimal(QUALITY_LOOKBACK_DAYS)}, "Open the Ledger", "ledger", key="unlinked"))
    other = [r for r in recent if r["category"] == FALLBACK_EXPENSE and r["direction"] == "debit"]
    if other:
        total = _whole(sum((r["amount"] for r in other), Decimal(0)))
        out.append(Insight(
            "other_pile", "watch" if len(other) >= OTHER_WATCH_COUNT else "info",
            f"{len(other)} {'payment sits' if len(other) == 1 else 'payments sit'} in “{FALLBACK_EXPENSE}” ({_rs(total)})",
            f"Money in “{FALLBACK_EXPENSE}” can't be budgeted or compared. In the last {QUALITY_LOOKBACK_DAYS} days {len(other)} "
            f"{'payment' if len(other) == 1 else 'payments'} adding up to {_rs(total)} have no real category. "
            "Give them one in the Ledger: Rectifi learns the payee for next time.",
            {"other_count": Decimal(len(other)), "other_total": total, "lookback_days": Decimal(QUALITY_LOOKBACK_DAYS)},
            "Fix categories", "ledger", key="other_pile"))
    for account in accounts:
        if not account["transactions"]:
            continue
        last = account["last_snapshot"]
        name = account["name"]
        if last is None:
            out.append(Insight(
                "stale_account", "watch", f"{name} has never been checked against the bank",
                f"{name} has {account['transactions']} entries but no bank balance on record, so nothing proves they are complete. "
                "Import a statement that has a balance column, or record a balance on the Accounts page.",
                {"entries_count": Decimal(account["transactions"])}, "Open Accounts", "accounts", key=f"stale:{account['id']}"))
            continue
        age = (ctx.today - last).days
        if age > STALE_ACCOUNT_DAYS:
            out.append(Insight(
                "stale_account", "watch", f"{name} was last checked against the bank {age} days ago",
                f"The latest bank balance for {name} is from {last:%d %b %Y}. Record a newer one, or import a recent statement.",
                {"age_days": Decimal(age)}, "Open Accounts", "accounts", key=f"stale:{account['id']}"))
        try:
            check = tools.reconcile(account["id"])
        except tools.RectifiError:
            continue
        if not check["balanced"]:
            gap = _whole(abs(check["gap"]))
            way = "more" if check["gap"] > 0 else "less"
            out.append(Insight(
                "balance_gap", "act", f"{name} is out by {_rs(gap)}",
                f"The bank shows {_rs(gap)} {way} than Rectifi expects, so a payment is missing or recorded twice. "
                "Look at the balance check on the Accounts page.",
                {"gap": gap}, "Open Accounts", "accounts", key=f"gap:{account['id']}"))
    return out


# ───────────────────────── budget pace ─────────────────────────

def _budget_pace(ctx: Context) -> list[Insight]:
    status = tools.budget_status(ctx.m0)
    if not status["categories"]:
        return []
    day, days = ctx.today.day, _days_in(ctx.m0)
    if day < MIN_PACE_DAY:
        return [Insight(
            "budget_pace", "info", f"Budget projections start on day {MIN_PACE_DAY}",
            f"It is day {day} of {days}. Projecting a whole month from fewer than {MIN_PACE_DAY} days is mostly noise, "
            "so Rectifi waits. Until then the Budgets page shows what you have spent so far.",
            {"day": Decimal(day), "days_in_month": Decimal(days), "first_day": Decimal(MIN_PACE_DAY)},
            "Open Budgets", "budgets", key="budget_pace:wait")]
    counts: dict[str, int] = {}
    for r in ctx.rows:
        if r["direction"] == "debit" and r["txn_date"] >= ctx.m0:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
    out = []
    for b in status["categories"]:
        name, spent, limit = b["category"], b["spent"], b["limit"]
        projected = spent * days / day
        spent_w, limit_w, proj_w = _whole(spent), _whole(limit), _whole(projected)
        numbers = {"spent": spent_w, "limit": limit_w, "day": Decimal(day), "days_in_month": Decimal(days)}
        lumpy = counts.get(name, 0) < MIN_PACE_TXNS
        if spent > limit:
            over = spent_w - limit_w
            pace = "" if lumpy else f" Same pace for the rest of the month: about {_rs(proj_w)}."
            numbers.update({"over_by": over, **({} if lumpy else {"projected": proj_w})})
            out.append(Insight(
                "budget_pace", "act", f"{name} is already {_rs(over)} over its {_rs(limit_w)} budget",
                f"{_rs(spent_w)} spent so far against a {_rs(limit_w)} budget, on day {day} of {days}.{pace}",
                numbers, "Open Budgets", "budgets", key=f"budget_pace:{name}"))
        elif not lumpy and projected > limit * PACE_OVER:
            over = proj_w - limit_w
            numbers.update({"projected": proj_w, "over_by": over})
            out.append(Insight(
                "budget_pace", "act", f"At this pace {name} ends the month ~{_rs(over)} over",
                f"You have spent {_rs(spent_w)} of your {_rs(limit_w)} {name} budget in {day} of {days} days. "
                f"Keep that pace and the month ends near {_rs(proj_w)}.",
                numbers, "Open Budgets", "budgets", key=f"budget_pace:{name}"))
        elif not lumpy and projected >= limit * PACE_NEAR:
            numbers.update({"projected": proj_w})
            out.append(Insight(
                "budget_pace", "watch", f"{name} is on pace to use almost all of its {_rs(limit_w)} budget",
                f"{_rs(spent_w)} spent in {day} of {days} days. Keep that pace and the month ends near {_rs(proj_w)}, "
                f"right at the {_rs(limit_w)} limit.",
                numbers, "Open Budgets", "budgets", key=f"budget_pace:{name}"))
    return out


# ───────────────────────── category spike ─────────────────────────

def _month_sums(ctx: Context) -> dict[tuple[date, str], Decimal]:
    sums: dict[tuple[date, str], Decimal] = {}
    for r in ctx.spend:
        k = (ctx.month_of(r["txn_date"]), r["category"])
        sums[k] = sums.get(k, Decimal(0)) + r["amount"]
    return sums


def _prior_months(ctx: Context, sums: dict) -> list[date]:
    """The (up to 3) complete months before this one that have any spending."""
    months = [_shift_month(ctx.m0, i) for i in (1, 2, 3)]
    return [m for m in months if any(month == m for (month, _) in sums)]


def _spikes(ctx: Context) -> list[Insight]:
    sums = _month_sums(ctx)
    prior = _prior_months(ctx, sums)
    if len(prior) < SPIKE_MIN_MONTHS:
        return []
    found = []
    for (month, category), current in sums.items():
        if month != ctx.m0:
            continue
        usual = sum((sums.get((m, category), Decimal(0)) for m in prior), Decimal(0)) / len(prior)
        if usual <= 0:
            continue
        extra = current - usual
        if extra >= SPIKE_MIN and extra * 100 >= usual * SPIKE_PCT:
            found.append((extra, category, current, usual))
    out = []
    for _, category, current, usual in sorted(found, reverse=True)[:MAX_PER_RULE]:
        cur_w, usual_w = _whole(current), _whole(usual)
        extra_w = cur_w - usual_w
        pct = (extra_w / usual_w * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        out.append(Insight(
            "category_spike", "watch", f"{category} is already {pct}% above your usual month",
            f"{_rs(cur_w)} on {category} so far this month, against about {_rs(usual_w)} in a usual month "
            f"(the average of your last {len(prior)} months with data). That is {_rs(extra_w)} more, with the month not over.",
            {"current": cur_w, "usual": usual_w, "extra": extra_w, "above_pct": pct, "prior_months": Decimal(len(prior))},
            "See it in the Ledger", "ledger", key=f"spike:{category}"))
    return out


# ───────────────────────── recurring payments ─────────────────────────

def _similar(values: list[Decimal]) -> bool:
    m = median(values)
    return all(abs(v - m) <= m * SIMILAR for v in values)


def _longest_similar_suffix(values: list[Decimal]) -> int:
    for k in range(len(values), 0, -1):
        if _similar(values[-k:]):
            return k
    return 0


def _is_monthly(dates: list[date], window_start: date, today: date) -> bool:
    """Roughly once a month: in at least 3 of the last 4 calendar months, or every gap is about a month. Never
    more than twice in one month (that is a habit, like chai, not a subscription), and the last payment must be
    recent (one that stopped months ago was cancelled, so it is not a cost any more)."""
    if dates[-1] < today - timedelta(days=ACTIVE_WITHIN_DAYS):
        return False
    per_month: dict[date, int] = {}
    for d in dates:
        per_month[d.replace(day=1)] = per_month.get(d.replace(day=1), 0) + 1
    if any(n > 2 for n in per_month.values()):
        return False
    if len({m for m in per_month if m >= window_start}) >= 3:
        return True
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    return len(dates) >= 3 and all(MONTHLY_GAP[0] <= g <= MONTHLY_GAP[1] for g in gaps)


@dataclass
class Recurring:
    key: str
    name: str
    amount: Decimal                  # the latest monthly amount
    count: int
    first_seen: date
    is_new: bool = False
    old_amount: Decimal | None = None
    price_up: bool = False


def find_recurring(ctx: Context) -> list[Recurring]:
    window_start = _shift_month(ctx.m0, 3)
    since = ctx.today - timedelta(days=RECURRING_WINDOW_DAYS)
    groups: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for r in ctx.spend:
        if r["txn_date"] < since:
            continue
        k = payee_key(r["payee"], r["description"])
        if k is None:
            continue
        groups.setdefault(k[0], []).append(r)
        names[k[0]] = k[1]
    found = []
    for key, rows in groups.items():
        if len(rows) < 3:
            continue
        amounts = [r["amount"] for r in rows]
        dates = [r["txn_date"] for r in rows]
        k = _longest_similar_suffix(amounts)
        run, earlier = amounts[-k:], amounts[:-k]
        j = _longest_similar_suffix(earlier) if earlier else 0
        before = earlier[-j:] if j else []
        step = bool(before) and median(before) > 0 and not (median(before) * (1 - SIMILAR) <= median(run) <= median(before) * (1 + SIMILAR))
        relevant, old = None, None
        if k >= 3:
            relevant = dates[-k:]
            old = median(before) if step else None
        elif step and (k >= 2 and j >= 1 or k == 1 and j >= 2) and k + j >= 3 and median(run) <= 3 * median(before):
            relevant = dates[-(k + j):]
            old = median(before)
        if relevant is None or not _is_monthly(relevant, window_start, ctx.today):
            continue
        current = median(run)
        price_up = old is not None and current > old * (1 + SIMILAR) and relevant[-k] >= ctx.today - timedelta(days=NEW_RECURRING_DAYS)
        first = relevant[0]
        is_new = (first >= ctx.today - timedelta(days=NEW_RECURRING_DAYS)
                  and ctx.first_data is not None and ctx.first_data <= first - timedelta(days=30))
        found.append(Recurring(key, names[key], current, len(relevant), first, is_new, old if price_up else None, price_up))
    return sorted(found, key=lambda x: (-x.amount, x.name))


def _recurring(ctx: Context, items: list[Recurring]) -> list[Insight]:
    if not items:
        return []
    monthly = _whole(sum((i.amount for i in items), Decimal(0)))
    yearly = monthly * 12
    numbers: dict[str, Decimal] = {"monthly_total": monthly, "yearly_total": yearly, "recurring_count": Decimal(len(items))}
    parts = []
    for i in items:
        amount = _whole(i.amount)
        numbers[f"{i.key}_monthly"] = amount
        parts.append(f"{i.name} {_rs(amount)}")
    out = [Insight(
        "recurring", "info",
        f"{len(items)} recurring {'payment costs' if len(items) == 1 else 'payments cost'} {_rs(monthly)} a month",
        f"{', '.join(parts)}. That is {_rs(monthly)} a month and {_rs(yearly)} a year. These repeat at a similar amount about "
        "every month; cancel any you no longer use. A regular shop with similar bills can show up here too.",
        numbers, "See them in the Ledger", "ledger", key="recurring")]
    for i in items:
        amount = _whole(i.amount)
        if i.price_up and i.old_amount is not None:
            old, extra = _whole(i.old_amount), amount - _whole(i.old_amount)
            out.append(Insight(
                "recurring_price", "watch", f"{i.name} went up from {_rs(old)} to {_rs(amount)}",
                f"That is {_rs(extra)} more a month, {_rs(extra * 12)} more a year. Check if the new price is right.",
                {"old": old, "new": amount, "extra_monthly": extra, "extra_yearly": extra * 12},
                "See it in the Ledger", "ledger", key=f"price:{i.key}"))
        if i.is_new:
            out.append(Insight(
                "recurring_new", "watch", f"{i.name} looks like a new monthly payment",
                f"{i.name} has charged about {_rs(amount)} on {i.count} different months since {i.first_seen:%d %b}, and it is not in "
                f"your older records. Over a year that is {_rs(amount * 12)}. Make sure you meant to start it.",
                {"monthly": amount, "yearly": amount * 12, "charges_count": Decimal(i.count)},
                "See it in the Ledger", "ledger", key=f"new:{i.key}"))
    return out


# ───────────────────────── large payments ─────────────────────────

def _large(ctx: Context, recurring_keys: set[str]) -> list[Insight]:
    since = ctx.today - timedelta(days=LARGE_LOOKBACK_DAYS)
    debits = [r for r in ctx.spend if r["txn_date"] >= since]
    if len(debits) < LARGE_MIN_PAYMENTS:
        return []
    typical = median([r["amount"] for r in debits])
    limit = max(LARGE_MULTIPLE * typical, LARGE_FLOOR)
    recent_from = ctx.today - timedelta(days=LARGE_RECENT_DAYS)
    big = []
    for r in debits:
        k = payee_key(r["payee"], r["description"])
        if r["txn_date"] >= recent_from and r["amount"] > limit and not (k and k[0] in recurring_keys):
            big.append(r)
    out = []
    for r in sorted(big, key=lambda r: -r["amount"])[:MAX_PER_RULE]:
        amount, limit_w, typical_w = _whole(r["amount"]), _whole(limit), _whole(typical)
        who = (r["payee"] or r["description"] or "an unnamed payee").split("@", 1)[0][:40]
        out.append(Insight(
            "large_payment", "watch", f"{_rs(amount)} to {who} on {r['txn_date']:%d %b} is unusually large",
            f"That is above {_rs(limit_w)}: three times your typical payment ({_rs(typical_w)}) or {_rs(LARGE_FLOOR)}, whichever is more. "
            "If you planned it, ignore this. If you don't recognise it, look at the entry in the Ledger.",
            {"amount": amount, "threshold": limit_w, "typical": typical_w, "floor": _whole(LARGE_FLOOR)},
            "Open the Ledger", "ledger", key=f"large:{r['id']}"))
    return out


# ───────────────────────── savings rate ─────────────────────────

def _savings(ctx: Context) -> list[Insight]:
    months = [_shift_month(ctx.m0, i) for i in (3, 2, 1)]                 # oldest first; all complete
    stats = {}
    for m in months:
        s = tools.spending_summary(m, m.replace(day=_days_in(m)))
        if s["savings_rate"] is not None:
            stats[m] = s
    latest_month = months[-1]
    if latest_month not in stats or len(stats) < 2:
        return []
    earlier = [stats[m]["savings_rate"] for m in months[:-1] if m in stats]
    before = (sum(earlier, Decimal(0)) / len(earlier)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    latest = stats[latest_month]["savings_rate"]
    change = (latest - before).quantize(Decimal("0.1"))
    numbers = {"latest_pct": latest, "before_pct": before, "change_points": abs(change),
               "income": _whole(stats[latest_month]["income"]), "spending": _whole(stats[latest_month]["spending"])}
    label = f"{latest_month:%B}"
    if change <= -SAVINGS_CHANGE:
        return [Insight(
            "savings_rate", "act" if latest < 0 else "watch",
            f"Your savings rate fell from {before}% to {latest}% in {label}",
            f"In {label} you earned {_rs(numbers['income'])} and spent {_rs(numbers['spending'])}. Before that you saved {before}% "
            f"of your income on average. That is a drop of {abs(change)} points.",
            numbers, "See where it went", "review", key="savings_rate")]
    if change >= SAVINGS_CHANGE:
        return [Insight(
            "savings_rate", "info", f"Your savings rate rose from {before}% to {latest}% in {label}. Nice.",
            f"In {label} you earned {_rs(numbers['income'])} and spent {_rs(numbers['spending'])}. Before that you saved "
            f"{before}% on average, so you are up {change} points. Keep what changed.",
            numbers, None, None, key="savings_rate")]
    return []


# ───────────────────────── unbudgeted categories ─────────────────────────

def _unbudgeted(ctx: Context) -> list[Insight]:
    sums = _month_sums(ctx)
    prior = _prior_months(ctx, sums)
    if not prior:
        return []
    budgeted = {b["category"] for b in tools.budget_status(ctx.m0)["categories"]}
    averages = []
    for category in {c for (_, c) in sums}:
        if category in budgeted or category == FALLBACK_EXPENSE:
            continue
        avg = sum((sums.get((m, category), Decimal(0)) for m in prior), Decimal(0)) / len(prior)
        if avg >= UNBUDGETED_MIN_AVERAGE:
            averages.append((avg, category))
    out = []
    for avg, category in sorted(averages, reverse=True)[:MAX_PER_RULE]:
        suggested = max(Decimal(100), (avg / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP) * 100)
        avg_w = _whole(avg)
        out.append(Insight(
            "unbudgeted", "info", f"{category} has no budget but averages {_rs(avg_w)} a month",
            f"Over your last {len(prior)} {'month' if len(prior) == 1 else 'months'} with data, {category} averaged {_rs(avg_w)}. A budget of {_rs(suggested)} would "
            "match that, and Rectifi could warn you before the month runs away. This is only a suggestion: nothing is set until you press the button.",
            {"average": avg_w, "suggested_budget": suggested, "prior_months": Decimal(len(prior))},
            f"Set a {_rs(suggested)} budget", "budgets", key=f"unbudgeted:{category}",
            apply={"tool": "set_budget", "category": category, "limit": suggested, "month": ctx.m0}))
    return out


# ───────────────────────── small leaks ─────────────────────────

def _small_leaks(ctx: Context) -> list[Insight]:
    since = ctx.today - timedelta(days=SMALL_WINDOW_DAYS)
    by_category: dict[str, list[Decimal]] = {}
    for r in ctx.spend:
        if r["txn_date"] > since and r["amount"] < SMALL_PAYMENT:
            by_category.setdefault(r["category"], []).append(r["amount"])
    found = [(sum(v, Decimal(0)), c, len(v)) for c, v in by_category.items()
             if len(v) >= SMALL_MIN_COUNT and sum(v, Decimal(0)) >= SMALL_MIN_TOTAL]
    out = []
    for total, category, n in sorted(found, reverse=True)[:MAX_PER_RULE]:
        total_w = _whole(total)
        out.append(Insight(
            "small_leak", "watch", f"{n} small {category} payments added up to {_rs(total_w)} in {SMALL_WINDOW_DAYS} days",
            f"Each was under {_rs(_whole(SMALL_PAYMENT))}, so none felt like much, but together that is {_rs(total_w)} a month on {category}. "
            "If this is where your money leaks, a small weekly limit can help.",
            {"leak_count": Decimal(n), "total": total_w, "under": _whole(SMALL_PAYMENT), "window_days": Decimal(SMALL_WINDOW_DAYS)},
            "See them in the Ledger", "ledger", key=f"leak:{category}"))
    return out


# ───────────────────────── the public function ─────────────────────────

def insights(today: date | None = None) -> list[Insight]:
    """Every insight right now, best first: data quality, then act, watch, info. Deterministic: same data, same list.

    Returns [] when there are no transactions at all."""
    today = today or tools.today()
    ctx = _load(today)
    if not ctx.rows and ctx.first_data is None:
        return []
    recurring = find_recurring(ctx)
    found: list[Insight] = []
    for produce in (_data_quality, _budget_pace, _spikes):
        found += produce(ctx)
    found += _recurring(ctx, recurring)
    found += _large(ctx, {i.key for i in recurring})
    found += _savings(ctx)
    found += _unbudgeted(ctx)
    found += _small_leaks(ctx)
    order = {id(i): n for n, i in enumerate(found)}
    return sorted(found, key=lambda i: (i.kind not in DATA_QUALITY_KINDS, _RANK[i.severity], order[id(i)]))


def top_insights(limit: int = 3, today: date | None = None) -> list[Insight]:
    """For the Overview strip: things worth a look first (act and watch). Quiet "info" items only fill spare places."""
    found = insights(today)
    loud = [i for i in found if i.severity != "info"]
    return (loud + [i for i in found if i.severity == "info"])[:limit]


def act_insights(today: date | None = None) -> list[Insight]:
    return [i for i in insights(today) if i.severity == "act"]


def insights_text(found: list[Insight]) -> list[dict]:
    """Plain dicts for the agent and the MCP server. Titles and details already carry every figure, formatted."""
    return [{"severity": i.severity, "kind": i.kind, "title": i.title, "detail": i.detail,
             "where_to_act": i.action_page} for i in found]
