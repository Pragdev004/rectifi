"""The fixed list of well-known shops whose NAME may be stored in plain text (multi-user plan, decision D3).

Why: a spending cap on "Swiggy" has to fire while the person is signed out, and while signed out the server cannot read
the payee. So a payment to one of these brands also gets a plain `transactions.merchant_key` ('swiggy'). Every other
payee stays sealed. The database enforces it: `merchant_key` is a foreign key to the table `allowed_merchants`, which
`init_db` fills from this file, so nothing outside the list can be written there.

This is NOT `categorize.merchant_key()` (that returns a key for any payee) and it is never filled from a person's own
pattern. Standard library only: no database, no langchain.

Rules for the list: well-known shops only. Nothing that reveals health, lending, investing, donations or a person.

How a name matches (so 'AMAZING TAILORS' is not Amazon and 'COLA HOUSE' is not Ola):
  * the text is lower-cased and every run of other characters becomes one space ('ola.cabs@axl' -> 'ola cabs axl')
  * a name of four letters or fewer must be a whole word ('ola', 'uber', 'pvr', 'ajio')
  * a longer name may sit anywhere in a word ('amazon' matches 'amazonpay')
  * brands are tried in list order, most specific first: 'instamart' before 'swiggy'
Known limit: a person whose own name is Ola would be tagged 'ola'. A brand with an unusual handle stays None and just
loses the signed-out alert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Brand:
    key: str                       # stored in transactions.merchant_key
    name: str                      # shown to the person
    aliases: tuple[str, ...] = ()  # other spellings and legal names; the key and the lower-case name always match too


# Order matters: more specific first ('instamart' contains no 'swiggy' as a word, but 'swiggy instamart' does).
ALLOWED: tuple[Brand, ...] = (
    Brand("instamart", "Instamart", ("swiggy instamart", "swiggyinstamart")),
    Brand("swiggy", "Swiggy", ("bundl tech", "bundl technologies")),
    Brand("zomato", "Zomato"),
    Brand("blinkit", "Blinkit", ("grofers",)),
    Brand("zepto", "Zepto", ("kiranakart",)),
    Brand("bigbasket", "BigBasket", ("bbnow", "big basket")),
    Brand("jiomart", "JioMart", ("jio mart",)),
    Brand("dominos", "Domino's", ("domino s", "jubilant foodworks")),
    Brand("eatsure", "EatSure", ("eat sure",)),
    Brand("amazon", "Amazon", ("amzn",)),
    Brand("flipkart", "Flipkart"),
    Brand("myntra", "Myntra"),
    Brand("ajio", "Ajio"),
    Brand("meesho", "Meesho"),
    Brand("nykaa", "Nykaa"),
    Brand("uber", "Uber"),
    Brand("ola", "Ola", ("olacabs", "ola cabs")),
    Brand("rapido", "Rapido"),
    Brand("irctc", "IRCTC"),
    Brand("bookmyshow", "BookMyShow", ("book my show",)),
    Brand("pvr", "PVR", ("pvrcinemas", "pvr cinemas")),
    Brand("netflix", "Netflix"),
    Brand("spotify", "Spotify"),
    Brand("hotstar", "Hotstar", ("disney hotstar",)),
)

KEYS: frozenset[str] = frozenset(b.key for b in ALLOWED)
NAMES: dict[str, str] = {b.key: b.name for b in ALLOWED}

SHORT = 4          # a name this short or shorter must be a whole word

_NON_WORD = re.compile(r"[^a-z0-9]+")


def _normalise(*parts: str | None) -> str:
    text = " ".join(p for p in parts if p)
    return " " + _NON_WORD.sub(" ", text.lower()).strip() + " "


def _hit(haystack: str, name: str) -> bool:
    name = _NON_WORD.sub(" ", name.lower()).strip()
    if len(name.replace(" ", "")) <= SHORT:
        return f" {name} " in haystack
    return name in haystack


def plain_key(payee: str | None, description: str | None = None) -> str | None:
    """The allow-listed key for a payment ('swiggy'), or None. Reads the payee and the narration."""
    haystack = _normalise(payee, description)
    if haystack.strip() == "":
        return None
    for brand in ALLOWED:
        for name in (brand.key, brand.name, *brand.aliases):
            if _hit(haystack, name):
                return brand.key
    return None


def rows() -> list[tuple[str, str]]:
    """(key, name) pairs to seed the table `allowed_merchants`."""
    return [(b.key, b.name) for b in ALLOWED]
