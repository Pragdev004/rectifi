"""How Rectifi picks a category for a payment. Pure: standard library only (numpy later), NO langchain, NO rectifi.agent.

F1 (the foundation) wrote only the names and shapes below. WS-CLASSIFY writes the logic inside its own block at the end
of this file. Everything else imports the constants from here, so the catch-all category has one name in one place.

Layers, in order (the first sure one wins): your own correction (`user`), a rule you taught (`learned`), a built-in
merchant rule (`rule`), a word-boundary heuristic (`heuristic`), a look-alike past payment (`retrieval`), then, after the
import and only for what is left, the model (`llm`). Nothing sure: the catch-all (`fallback`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import textvec, userctx

FALLBACK_EXPENSE = "Miscellaneous"      # was "Other" before this phase; the database row was renamed in place, same id
FALLBACK_INCOME = "Other income"
FALLBACK_NAMES = (FALLBACK_EXPENSE, FALLBACK_INCOME)
LEGACY_FALLBACK_EXPENSE = "Other"       # only db._migrate_categories mentions this

# Where a category came from. Matches the CHECK on transactions.category_source.
SOURCES = ("user", "learned", "rule", "heuristic", "retrieval", "llm", "fallback")


def fallback_name(direction: str) -> str:
    """The catch-all category for money going out (debit) or coming in (credit)."""
    return FALLBACK_EXPENSE if direction == "debit" else FALLBACK_INCOME


@dataclass(frozen=True)
class Decision:
    """What the classifier decided for one payment. `confidence` is a whole percent (0-100), never a Decimal.

    category        name of the category to store now (never None: the catch-all if nothing was sure)
    source          one of SOURCES
    confidence      0-100
    reason          one short plain sentence for the Ledger: "rule 'swiggy'", "like #812 SWIGGY BLR"
    suggested       a better guess to offer the person without applying it (a name), or None
    suggested_confidence  0-100 or None
    """

    category: str
    source: str
    confidence: int
    reason: str
    suggested: str | None = None
    suggested_confidence: int | None = None


_VPA = re.compile(r"([\w.\-]{2,})@[a-z][a-z0-9.\-]*", re.I)
_IFSC = re.compile(r"\b[a-z]{4}0[a-z0-9]{6}\b", re.I)
_DATE = re.compile(r"\b\d{1,2}[-/ ](?:\d{1,2}|[a-z]{3})[-/ ]\d{2,4}\b", re.I)
_NOISE_WORDS = {"upi", "dr", "cr", "p2m", "p2a", "p2p", "imps", "neft", "rtgs", "mb", "ib", "pos", "ach", "nach", "ref", "txn",
                "payment", "pay", "paid", "to", "from", "by", "via", "transfer", "ecom", "pur", "purchase"}
_SEGMENT_SPLIT = re.compile(r"[/|]")


def _clean_key(text: str) -> str:
    text = _IFSC.sub(" ", _DATE.sub(" ", text.lower()))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [w for w in text.split() if not w.isdigit() and w not in _NOISE_WORDS]
    return " ".join(words)


def _from_narration(narration: str) -> str:
    """The merchant part of a bank narration: the VPA handle if there is one, else the first segment that looks like a name."""
    vpa = _VPA.search(narration)
    if vpa:
        key = _clean_key(vpa.group(1))
        if key:
            return key
    parts = [p for p in _SEGMENT_SPLIT.split(narration) if p.strip()]
    if len(parts) < 3 and "-" in narration:
        parts = [p for p in re.split(r"\s-\s|-", narration) if p.strip()]
    if len(parts) >= 2:
        for part in parts:
            key = _clean_key(part)
            if re.search(r"[a-z]{3,}", key):
                return key
    return _clean_key(narration)


def merchant_key(payee: str | None, narration: str | None = None) -> str:
    """A stable short key for "the same merchant": lowercase, UPI handle domain, dates, IFSC codes, long numbers and
    words like UPI/DR/CR dropped, spaces squeezed. 'SWIGGY*ORDER 8812@icici' and 'swiggy order' give the same key.
    With no payee the key comes from the narration ('UPI/DR/4291.../SWIGGY/YESB/swiggy@ybl/Pay' -> 'swiggy')."""
    if payee and payee.strip():
        return _clean_key(payee.split("@", 1)[0] if "@" in payee else payee)
    return _from_narration(narration or "")


# ═══ WS-CLASSIFY: begin ═══
# Layers in order. The first sure one wins (CLAUDE.md rule 5: nothing here talks to a model).
#   1 learned rules  2 built-in rules  3 heuristics  4 retrieval over trusted past labels  6 fallback.
# (Layer 5, the model, runs after the import in rectifi/agent/classify.py and never in here.)

CONF_LEARNED = 98
CONF_RULE = 95
SHORT_RULE = 4            # a rule this short or shorter must start a word ("ola" matches olacabs, not "hola" or "gola")

RETRIEVAL_TOP_COS = 0.82  # the closest trusted payment must be at least this alike ...
RETRIEVAL_VOTE = 0.7      # ... and this share of the weighted vote must agree
SUGGEST_TOP_COS = 0.6     # a weaker echo is offered as a suggestion, never applied
SUGGEST_VOTE = 0.5
RETRIEVAL_K = 5
INDEX_MAX_ROWS = 5000

KIND_FOR = {"debit": ("expense", "transfer"), "credit": ("income", "transfer")}


# ── layer 3: heuristics ────────────────────────────────────────────────────────────────────────────
# (regex on the lowercase "payee narration" text, direction or None for both, category, confidence)

def _h(pattern: str, direction: str | None, category: str, confidence: int):
    return (re.compile(pattern), direction, category, confidence)


HEURISTICS = [
    _h(r"\b(atm|cash\s*wdl|cash\s*withdrawal|nwd)\b", "debit", "Cash Withdrawal", 90),
    _h(r"\b(salary|sal\s*cr|stipend)\b", "credit", "Salary & Allowance", 88),
    _h(r"\b(int\.?\s*pd|interest|cashback|dividend)\b", "credit", "Interest & Cashback", 85),
    _h(r"\b(refund|reversal|reversed)\b", "credit", "Refunds", 85),
    _h(r"\b(chrg|charges?|annual\s*fee|sms\s*alert|min(?:imum)?\s*bal\w*)\b", "debit", "Fees & Charges", 85),
    _h(r"\b(emi|loan)\b|\bnach\b.*\b(bajaj|hdb\w*|fin\w*)\b", None, "EMI & Loans", 85),
    _h(r"\b(sip|mutual\s*fund|groww|zerodha|nps|ppf)\b", None, "Investments", 82),
    _h(r"\b(rent|nobroker)\b", "debit", "Rent", 85),
    _h(r"\b(hpcl|bpcl|iocl|indian\s*oil|nayara|petrol|fuel)\b", "debit", "Fuel", 90),
    _h(r"\b(pharma|pharmacy|chemist|medical|hospital|clinic|diagnostics?|dental)\b", "debit", "Health", 85),
    _h(r"\b(restaurant|biryani|pizza|burger|bakery|canteen|dhaba|tiffin|juice)\b", "debit", "Food & Dining", 80),
    _h(r"\b(supermarket|kirana|grocery|groceries|vegetables)\b", "debit", "Groceries", 80),
    _h(r"\b(cinema|movies?|theatre|multiplex)\b", "debit", "Entertainment", 80),
    _h(r"\b(flights?|airlines?|airways|resort|homestay)\b", "debit", "Travel", 80),
    _h(r"\b(cab|taxi)\b", "debit", "Transport", 80),
    _h(r"\b(recharge|prepaid|postpaid|dth|broadband|electricity)\b", "debit", "Bills & Utilities", 80),
    _h(r"\b(school|tuition|university|exam\s*fee)\b", "debit", "Education", 80),
    _h(r"\b(gym|fitness|salon|spa|parlou?r)\b", "debit", "Personal Care", 80),
    _h(r"\b(donation|charity)\b", "debit", "Gifts & Donations", 80),
    _h(r"\b(self|own\s*a/?c)\b", None, "Transfers", 80),
]


def _own_account_pattern(last4: str):
    """The statement shows an account number as XXXX1234, ****1234 or 'a/c 1234'. A bare 1234 proves nothing."""
    return re.compile(rf"(?:[x*]{{2,}}|a/?c\W*(?:no\W*)?\w*?|acct?\W*\w*?|account\W*\w*?){last4}\b")


@dataclass
class Context:
    """Everything decide() reads from the database, loaded once. An import builds one and reuses it for every row."""

    categories: dict[str, str]                       # name -> kind
    category_ids: dict[str, int]                     # name -> id
    rules: list[tuple[str, str, str]]                # (pattern, category name, 'learned'|'default'): learned first, then longest first
    own_last4: list[str]
    index: "NgramIndex"
    kinds_by_id: dict[int, str] = field(default_factory=dict)


def load_context(conn, *, with_index: bool = True) -> Context:
    cats = conn.execute("SELECT id, name, kind FROM categories").fetchall()
    # A learned pattern is sealed, so the database cannot sort by its length: the order is made here. A pattern that reads as
    # None is sealed and its key is not here (signed out): it is skipped until the next sign-in.
    rules = sorted(
        (r for r in conn.execute(
            """SELECT r.pattern, c.name AS category, r.source FROM merchant_rules r JOIN categories c ON c.id = r.category_id"""
        ).fetchall() if r["pattern"]),
        key=lambda r: (r["source"] != "learned", -len(r["pattern"]), r["pattern"]),
    )
    last4 = [r["last4"][-4:] for r in conn.execute("SELECT last4 FROM accounts WHERE last4 IS NOT NULL AND length(last4) >= 4").fetchall()]
    index = get_index(conn) if with_index else NgramIndex([])
    return Context(
        categories={c["name"]: c["kind"] for c in cats}, category_ids={c["name"]: c["id"] for c in cats},
        rules=[(r["pattern"], r["category"], r["source"]) for r in rules], own_last4=last4, index=index,
        kinds_by_id={c["id"]: c["kind"] for c in cats},
    )


def _fits(ctx: Context, name: str, direction: str) -> bool:
    return ctx.categories.get(name) in KIND_FOR[direction]


def _rule_matches(pattern: str, source: str, haystack: str, key: str) -> bool:
    if source == "learned":
        bounded = re.compile(rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])")
        return bool(bounded.search(key) or bounded.search(haystack))
    if pattern not in haystack:
        return False
    if len(pattern) <= SHORT_RULE:
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(pattern)}", haystack))
    return True


def _by_rule(ctx: Context, haystack: str, key: str, direction: str) -> Decision | None:
    for pattern, category, source in ctx.rules:
        if not _fits(ctx, category, direction) or not _rule_matches(pattern, source, haystack, key):
            continue
        if source == "learned":
            return Decision(category, "learned", CONF_LEARNED, f"you taught Rectifi '{pattern}'")
        return Decision(category, "rule", CONF_RULE, f"rule '{pattern}'")
    return None


def _by_heuristic(ctx: Context, haystack: str, direction: str) -> Decision | None:
    for regex, way, category, confidence in HEURISTICS:
        if (way and way != direction) or not _fits(ctx, category, direction):
            continue
        found = regex.search(haystack)
        if found:
            return Decision(category, "heuristic", confidence, f"the words '{found.group(0).strip()}' mean {category}")
    if "Transfers" in ctx.categories:
        for last4 in ctx.own_last4:
            if _own_account_pattern(last4).search(haystack):
                return Decision("Transfers", "heuristic", 80, f"mentions your own account ending {last4}")
    return None


# ── layer 4: retrieval ─────────────────────────────────────────────────────────────────────────────

class NgramIndex:
    """Trusted, labelled payments as character n-gram vectors. `rows` are dicts with id, key, category, kind, label."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.matrix = textvec.vectorize_many([r["key"] for r in rows])

    def __len__(self) -> int:
        return len(self.rows)

    def neighbours(self, key: str, direction: str, k: int = RETRIEVAL_K) -> list[tuple[dict, float]]:
        """The k closest trusted payments whose category fits this direction, best first, as (row, cosine)."""
        if not self.rows or not key:
            return []
        wanted = KIND_FOR[direction]
        found = textvec.top_matches(self.matrix, textvec.vectorize(key), k=min(len(self.rows), k * 4))
        return [(self.rows[i], cos) for i, cos in found if self.rows[i]["kind"] in wanted][:k]


_index_cache: dict[tuple, "NgramIndex"] = {}      # keyed by (person, stamp): one person's index is never handed to another


def get_index(conn) -> "NgramIndex":
    """The index over the newest INDEX_MAX_ROWS trusted payments. Trusted means YOU or a fixed rule gave the label
    (user, learned, rule) or it is an older entry from before sources were recorded. Never the model, a guess or the
    catch-all, so a mistake cannot teach itself. Cached until the labels change."""
    rows = conn.execute(
        """SELECT t.id, t.payee, t.description, c.name AS category, c.kind
           FROM transactions t JOIN categories c ON c.id = t.category_id
           WHERE (t.category_source IN ('user', 'learned', 'rule')
                  OR (t.category_source IS NULL AND c.name <> ALL(%s)))
             AND (t.payee IS NOT NULL OR t.description IS NOT NULL)
           ORDER BY t.id DESC LIMIT %s""",
        (list(FALLBACK_NAMES), INDEX_MAX_ROWS),
    ).fetchall()
    items = []
    for r in rows:
        key = merchant_key(r["payee"], r["description"])
        if key:
            items.append({"id": r["id"], "key": key, "category": r["category"], "kind": r["kind"],
                          "label": (r["payee"] or r["description"] or "")[:40]})
    stamp = (userctx.current().uid, repr([(i["id"], i["key"], i["category"]) for i in items]))
    cached = _index_cache.get(stamp)
    if cached is None:
        for old in [k for k in _index_cache if k[0] == stamp[0]]:       # one entry per person
            del _index_cache[old]
        cached = _index_cache[stamp] = NgramIndex(items)
    return cached


def _by_retrieval(ctx: Context, key: str, direction: str) -> tuple[Decision | None, Decision | None]:
    """(accepted decision, weaker suggestion). At most one is not None."""
    near = ctx.index.neighbours(key, direction)
    if not near:
        return None, None
    votes: dict[str, float] = {}
    for row, cos in near:
        if cos >= SUGGEST_TOP_COS:
            votes[row["category"]] = votes.get(row["category"], 0.0) + cos
    if not votes:
        return None, None
    total = sum(votes.values())
    winner = max(votes, key=lambda c: votes[c])
    share = votes[winner] / total
    top_row, top_cos = near[0]
    nearest_of_winner = next(row for row, _ in near if row["category"] == winner)
    confidence = max(0, min(89, round(100 * top_cos * share)))
    reason = f"like #{nearest_of_winner['id']} {nearest_of_winner['label']}"
    if top_cos >= RETRIEVAL_TOP_COS and share >= RETRIEVAL_VOTE and _fits(ctx, winner, direction):
        return Decision(winner, "retrieval", confidence, reason), None
    if top_cos >= SUGGEST_TOP_COS and share >= SUGGEST_VOTE and _fits(ctx, winner, direction):
        return None, Decision(winner, "fallback", confidence, reason)
    return None, None


# ── the entry point ────────────────────────────────────────────────────────────────────────────────

def decide_with(ctx: Context, payee: str | None, narration: str | None, direction: str) -> Decision:
    """decide() with a ready Context (an import reuses one for every row)."""
    if direction not in KIND_FOR:
        raise ValueError(f"direction must be 'debit' or 'credit', not {direction!r}")
    haystack = f"{payee or ''} {narration or ''}".lower()
    key = merchant_key(payee, narration)
    fallback = fallback_name(direction)
    if haystack.strip():
        for layer in (lambda: _by_rule(ctx, haystack, key, direction), lambda: _by_heuristic(ctx, haystack, direction)):
            found = layer()
            if found:
                return found
        accepted, suggestion = _by_retrieval(ctx, key, direction)
        if accepted:
            return accepted
        if suggestion:
            return Decision(fallback, "fallback", 0, "no rule matched", suggestion.category, suggestion.confidence)
    return Decision(fallback, "fallback", 0, "nothing matched")


def decide(conn, payee: str | None, narration: str | None, direction: str, ctx: Context | None = None) -> Decision:
    """Pick a category for one payment from the fixed layers. Returns a Decision with a category NAME that always fits the
    direction (the catch-all if nothing was sure). Instant: no model, no network."""
    return decide_with(ctx or load_context(conn), payee, narration, direction)

# ═══ WS-CLASSIFY: end ═══
