"""How Rectifi picks a category for a payment. Pure: standard library only (numpy later), NO langchain, NO rectifi.agent.

F1 (the foundation) wrote only the names and shapes below. WS-CLASSIFY writes the logic inside its own block at the end
of this file. Everything else imports the constants from here, so the catch-all category has one name in one place.

Layers, in order (the first sure one wins): your own correction (`user`), a rule you taught (`learned`), a built-in
merchant rule (`rule`), a word-boundary heuristic (`heuristic`), a look-alike past payment (`retrieval`), then, after the
import and only for what is left, the model (`llm`). Nothing sure: the catch-all (`fallback`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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


def merchant_key(payee: str | None, narration: str | None = None) -> str:
    """A stable short key for "the same merchant": lowercase, UPI handle and trailing digits dropped, spaces squeezed.
    'SWIGGY*ORDER 8812@icici' and 'swiggy order' give the same key. WS-CLASSIFY may make this smarter, never less stable."""
    text = (payee or "").strip() or (narration or "")
    text = text.lower().split("@", 1)[0]
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ═══ WS-CLASSIFY: begin ═══
# The layers, NgramIndex and `decide(conn, payee, narration, direction) -> Decision` go here (nobody else edits this block).

def decide(conn, payee: str | None, narration: str | None, direction: str) -> Decision:
    """Pick a category for one payment. F1 only fixes the signature; WS-CLASSIFY implements it."""
    raise NotImplementedError("categorize.decide is written by WS-CLASSIFY")

# ═══ WS-CLASSIFY: end ═══
