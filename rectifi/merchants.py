"""Merchant names for the leak alerts: which payments are Swiggy, Blinkit, Amazon... and which group they belong to.

Fixed rules only (CLAUDE.md rule 14). No model, no langchain. A payment is matched on its payee plus its narration:
'UPI/DR/412.../SWIGGY/YESB/swiggy@yesbank/Pay' and 'swiggy@icici' are both Swiggy.

How a pattern matches (so 'AMAZING TAILORS' is not Amazon and 'SWIGGLE TOYS' is not Swiggy):
  * the text is lower-cased and every run of other characters becomes one space ('BLINKIT.PAYU@HDFCBANK' -> 'blinkit payu hdfcbank')
  * a pattern of four letters or fewer must start a word ('ajio' matches 'ajio retail', not 'bajio')
  * a longer pattern may sit anywhere in a word ('amazon' matches 'amazonpay')
  * the list is ordered, most specific first: 'instamart' is tried before 'swiggy', so 'Swiggy Instamart' is Quick commerce
  * patterns you taught (table merchant_patterns) are tried before the built-in list

The bare word 'eternal' (Zomato's parent company) is NOT a pattern on purpose: it would match far too much.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import merchant_allowlist, vault
from .categorize import merchant_key
from .db import connect
from .tools import RectifiError

QUICK = "Quick commerce"
FOOD = "Food delivery"
SHOPPING = "Shopping"
OTHER = "Other"
GROUPS = (QUICK, FOOD, SHOPPING, OTHER)

SHORT_PATTERN = 4          # a pattern this short or shorter must start a word
MIN_PATTERN = 3            # shorter patterns are refused (they would match half the ledger)
MAX_PATTERN = 60
MAX_MERCHANT = 60


@dataclass(frozen=True)
class Merchant:
    name: str                  # 'Swiggy Instamart'
    group: str                 # one of GROUPS
    pattern: str = ""          # the pattern that matched
    user: bool = False         # True when the person taught this pattern


# (pattern, merchant, group). Order matters: most specific first.
REGISTRY: tuple[tuple[str, str, str], ...] = (
    # Quick commerce
    ("swiggy instamart", "Swiggy Instamart", QUICK),
    ("instamart", "Swiggy Instamart", QUICK),
    ("blinkit", "Blinkit", QUICK),
    ("grofers", "Blinkit", QUICK),
    ("zepto", "Zepto", QUICK),
    ("kiranakart", "Zepto", QUICK),
    ("bigbasket", "BigBasket", QUICK),
    ("bbnow", "BigBasket", QUICK),
    ("flipkart minutes", "Flipkart Minutes", QUICK),
    ("jiomart", "JioMart", QUICK),
    # Food delivery (after Instamart, because Swiggy Instamart contains 'swiggy')
    ("swiggy", "Swiggy", FOOD),
    ("bundl tech", "Swiggy", FOOD),
    ("zomato", "Zomato", FOOD),
    ("eatsure", "EatSure", FOOD),
    ("dominos", "Domino's", FOOD),
    # Shopping (after Flipkart Minutes, because it contains 'flipkart')
    ("amazon", "Amazon", SHOPPING),
    ("amzn", "Amazon", SHOPPING),
    ("flipkart", "Flipkart", SHOPPING),
    ("myntra", "Myntra", SHOPPING),
    ("ajio", "Ajio", SHOPPING),
    ("meesho", "Meesho", SHOPPING),
    ("nykaa", "Nykaa", SHOPPING),
)

# Every brand on the allow-list (rectifi/merchant_allowlist.py, plan D3), so a payment that carries only the plain
# `transactions.merchant_key` (it arrived while its owner was signed out and its payee is still sealed) still has a merchant
# and a group. key -> (merchant name, group). The names are the ones above; 'instamart' is 'Swiggy Instamart'.
BRANDS: dict[str, tuple[str, str]] = {
    "instamart": ("Swiggy Instamart", QUICK), "swiggy": ("Swiggy", FOOD), "zomato": ("Zomato", FOOD),
    "blinkit": ("Blinkit", QUICK), "zepto": ("Zepto", QUICK), "bigbasket": ("BigBasket", QUICK),
    "jiomart": ("JioMart", QUICK), "dominos": ("Domino's", FOOD), "eatsure": ("EatSure", FOOD),
    "amazon": ("Amazon", SHOPPING), "flipkart": ("Flipkart", SHOPPING), "myntra": ("Myntra", SHOPPING),
    "ajio": ("Ajio", SHOPPING), "meesho": ("Meesho", SHOPPING), "nykaa": ("Nykaa", SHOPPING),
    "uber": ("Uber", OTHER), "ola": ("Ola", OTHER), "rapido": ("Rapido", OTHER), "irctc": ("IRCTC", OTHER),
    "bookmyshow": ("BookMyShow", OTHER), "pvr": ("PVR", OTHER), "netflix": ("Netflix", OTHER),
    "spotify": ("Spotify", OTHER), "hotstar": ("Hotstar", OTHER),
}
PLAIN_NAMES: frozenset[str] = frozenset(name for name, _ in BRANDS.values())


def _allow_list_patterns() -> tuple[tuple[str, str, str], ...]:
    """Registry rows for the allow-listed brands the list above does not name yet. They match the way the allow-list does
    (a name of four letters or fewer must be a whole word, so 'ola' is not 'olam'): a short pattern is padded with spaces."""
    have = {name for _, name, _ in REGISTRY}
    out = []
    for brand in merchant_allowlist.ALLOWED:
        name, group = BRANDS[brand.key]
        if name in have:
            continue
        for word in (brand.key, brand.name, *brand.aliases):
            text = re.sub(r"[^a-z0-9]+", " ", word.lower()).strip()
            out.append((f" {text} " if len(text.replace(" ", "")) <= merchant_allowlist.SHORT else text, name, group))
    return tuple(out)


REGISTRY = REGISTRY + _allow_list_patterns()


def is_plain(name: str | None) -> bool:
    """True for a brand on the allow-list: its name may sit in plain text (a cap target, an alert's dedupe key, a push).
    Any other merchant name (one you taught, Flipkart Minutes) is yours and stays sealed."""
    return name in PLAIN_NAMES


def by_key(key: str | None) -> Merchant | None:
    """The merchant for a plain `merchant_key` ('swiggy'), or None."""
    found = BRANDS.get(key or "")
    return Merchant(found[0], found[1], key or "") if found else None


_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize(*parts: str | None) -> str:
    """Lower-case text with every run of other characters squeezed to one space, padded so word starts are easy to find."""
    text = " ".join(p for p in parts if p)
    return " " + _NON_WORD.sub(" ", text.lower()).strip() + " "


def clean_pattern(pattern: str | None) -> str:
    """A pattern as it is stored: lower-case words separated by single spaces. Raises RectifiError when it is too short."""
    text = _NON_WORD.sub(" ", (pattern or "").lower()).strip()
    if len(text.replace(" ", "")) < MIN_PATTERN:
        raise RectifiError(f"A merchant pattern needs at least {MIN_PATTERN} letters, or it would match too many payments.")
    if len(text) > MAX_PATTERN:
        raise RectifiError(f"A merchant pattern can be at most {MAX_PATTERN} characters.")
    return text


def _hit(haystack: str, pattern: str) -> bool:
    """Does `pattern` occur in the normalised text? A short pattern (<= 4 letters) must start a word."""
    if len(pattern) <= SHORT_PATTERN:
        return " " + pattern in haystack
    return pattern in haystack


def user_patterns(conn=None) -> list[tuple[str, str, str]]:
    """(pattern, merchant, group) rows the person taught, newest first, so a newer teaching overrides an older one."""
    sql = "SELECT pattern, merchant, grp FROM merchant_patterns ORDER BY id DESC"
    if conn is not None:
        rows = conn.execute(sql).fetchall()
    else:
        with connect() as own:
            rows = own.execute(sql).fetchall()
    # a taught pattern is sealed: signed out (no key) it reads as None and is simply not used until the next sign-in
    return [(r["pattern"], r["merchant"], r["grp"]) for r in rows if r["pattern"] and r["merchant"]]


def match(payee: str | None, description: str | None = None, *, user: list[tuple[str, str, str]] | None = None) -> Merchant | None:
    """The merchant of one payment, or None. Pass `user=user_patterns()` once when matching many rows."""
    haystack = normalize(payee, description)
    if haystack.strip() == "":
        return None
    for pattern, name, group in (user_patterns() if user is None else user):
        if _hit(haystack, pattern):
            return Merchant(name, group, pattern, True)
    for pattern, name, group in REGISTRY:
        if _hit(haystack, pattern):
            return Merchant(name, group, pattern, False)
    return None


def payee_label(payee: str | None, description: str | None = None) -> str:
    """A short, safe label for a payment that is in no group: the merchant key ('some shop'), never a handle or number."""
    key = merchant_key(payee, description)
    words = [w for w in key.split() if w.isalpha()][:3]
    return " ".join(words).title() if words else "a shop"


def groups() -> tuple[str, ...]:
    return GROUPS


def known_merchants() -> dict[str, str]:
    """{merchant name: group} for the built-in list and everything the person taught (a name taught twice keeps its latest group)."""
    found = {name: group for _, name, group in REGISTRY}
    for _, name, group in reversed(user_patterns()):
        found[name] = group
    return found


def list_patterns() -> list[dict]:
    with connect() as conn:
        return conn.execute("SELECT id, pattern, merchant, grp AS \"group\", created_at FROM merchant_patterns ORDER BY id").fetchall()


def add_pattern(pattern: str, merchant: str, group: str) -> dict:
    """Teach Rectifi a merchant name. Raises RectifiError for a pattern under 3 letters, an empty merchant or an unknown group."""
    text = clean_pattern(pattern)
    name = (merchant or "").strip()
    if not name or len(name) > MAX_MERCHANT:
        raise RectifiError(f"Give the merchant a name of 1 to {MAX_MERCHANT} characters.")
    if group not in GROUPS:
        raise RectifiError(f"Group must be one of: {', '.join(GROUPS)}.")
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO merchant_patterns (pattern, merchant, grp, pattern_blind) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id, pattern_blind) DO UPDATE SET merchant = EXCLUDED.merchant, grp = EXCLUDED.grp "
            "RETURNING id, pattern, merchant, grp AS \"group\", created_at",
            (vault.seal(text), vault.seal(name), group, vault.blind(text)),
        ).fetchone()
    return row


def delete_pattern(pattern_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM merchant_patterns WHERE id = %s", (pattern_id,)).rowcount > 0
