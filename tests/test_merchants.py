"""P1: the merchant list. Fixed rules only; a name must not match inside a longer, different word."""

import pytest

from rectifi import db, merchants
from rectifi.tools import RectifiError


def m(payee=None, description=None):
    found = merchants.match(payee, description)
    return (found.name, found.group) if found else None


def test_narration_with_swiggy():
    assert m(None, "UPI/DR/412345678901/SWIGGY/YESB/swiggy@yesbank/Pay") == ("Swiggy", "Food delivery")


def test_instamart_wins_over_swiggy():
    assert m("Swiggy Instamart") == ("Swiggy Instamart", "Quick commerce")
    assert m("swiggy", "instamart order") == ("Swiggy Instamart", "Quick commerce")


@pytest.mark.parametrize("payee,want", [
    ("BLINKIT.PAYU@HDFCBANK", ("Blinkit", "Quick commerce")),
    ("grofers india", ("Blinkit", "Quick commerce")),
    ("ZEPTO MARKETPLACE PVT LTD", ("Zepto", "Quick commerce")),
    ("kiranakart@ybl", ("Zepto", "Quick commerce")),
    ("amzn mktp", ("Amazon", "Shopping")),
    ("AMAZON PAY INDIA", ("Amazon", "Shopping")),
    ("zomato-order@ptybl", ("Zomato", "Food delivery")),
    ("Bundl Technologies Pvt", ("Swiggy", "Food delivery")),
    ("Flipkart Minutes", ("Flipkart Minutes", "Quick commerce")),
    ("flipkart", ("Flipkart", "Shopping")),
    ("ajio retail", ("Ajio", "Shopping")),
])
def test_known_merchants(payee, want):
    assert m(payee) == want


@pytest.mark.parametrize("payee", ["AMAZING TAILORS", "SWIGGLE TOYS", "BBQ NATION", "Eternal Ltd", "bajio cafe", None, ""])
def test_negatives(payee):
    assert m(payee) is None


def test_user_pattern_overrides_and_is_removable():
    row = merchants.add_pattern("Corner Cafe", "Corner Cafe", "Food delivery")
    assert m("CORNER-CAFE@okaxis") == ("Corner Cafe", "Food delivery")
    merchants.add_pattern("swiggy", "Swiggy Dineout", "Other")      # a taught pattern is tried first
    assert m("swiggy@icici") == ("Swiggy Dineout", "Other")
    assert merchants.delete_pattern(row["id"]) is True
    assert m("corner cafe") is None


def test_short_pattern_rejected_and_bad_group():
    with pytest.raises(RectifiError):
        merchants.add_pattern("ab", "Ab", "Other")
    with pytest.raises(RectifiError):
        merchants.add_pattern("abc def", "X", "Nonsense")
    with pytest.raises(Exception):
        with db.connect() as conn:                              # the database says no too
            conn.execute("INSERT INTO merchant_patterns (pattern, merchant, grp) VALUES ('ab', 'x', 'Other')")


def test_known_merchants_includes_taught():
    merchants.add_pattern("dunzo", "Dunzo", "Quick commerce")
    assert merchants.known_merchants()["Dunzo"] == "Quick commerce"
    assert merchants.known_merchants()["Blinkit"] == "Quick commerce"
