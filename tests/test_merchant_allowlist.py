"""The plain-text brand list: each brand and alias matches, look-alikes and people do not, nothing sensitive is on it."""

import psycopg
import pytest

from rectifi import db, merchant_allowlist as al

MATCHES = [
    ("swiggy@icici", None, "swiggy"), ("SWIGGY", "UPI/DR/412345678901/SWIGGY/YESB", "swiggy"),
    ("BUNDL TECHNOLOGIES PVT LTD", None, "swiggy"), ("swiggy instamart", None, "instamart"), ("Instamart", None, "instamart"),
    ("zomato-order@hdfcbank", None, "zomato"), ("blinkit.payu@hdfcbank", None, "blinkit"), ("GROFERS INDIA", None, "blinkit"),
    ("zepto", None, "zepto"), ("KIRANAKART TECHNOLOGIES", None, "zepto"), ("BigBasket", None, "bigbasket"),
    ("BBNOW", None, "bigbasket"), ("jiomart", None, "jiomart"), ("Dominos Pizza", None, "dominos"), ("DOMINO'S", None, "dominos"),
    ("JUBILANT FOODWORKS", None, "dominos"), ("EatSure", None, "eatsure"), ("amazonpay@apl", None, "amazon"),
    ("AMZN Mktp IN", None, "amazon"), ("amazon", None, "amazon"), ("Flipkart Internet", None, "flipkart"),
    ("FLIPKART MINUTES", None, "flipkart"), ("myntra", None, "myntra"), ("AJIO RETAIL", None, "ajio"),
    ("meesho", None, "meesho"), ("NYKAA E RETAIL", None, "nykaa"), ("uber india systems", None, "uber"),
    ("OLA CABS", None, "ola"), ("olacabs", None, "ola"), ("ANI TECHNOLOGIES OLA", None, "ola"), ("rapido@axl", None, "rapido"),
    ("IRCTC", None, "irctc"), ("BookMyShow", None, "bookmyshow"), ("PVR Cinemas", None, "pvr"), ("PVRCINEMAS", None, "pvr"),
    ("NETFLIX.COM", None, "netflix"), ("spotify@ybl", None, "spotify"), ("Disney Hotstar", None, "hotstar"),
    ("UPI-123", "paid to swiggy via upi", "swiggy"),
]

NEGATIVES = [
    ("COLA HOUSE", None), ("AMAZING TAILORS", None), ("SWIGGLE TOYS", None), ("rahul.sharma@okaxis", None),
    ("priya@ybl", "UPI/DR/payment for rent"), ("Ubersmith", None), ("BAJIO FOODS", None), ("PVRK STORES", None),
    ("Apollo Pharmacy", None), ("Zerodha Broking", None), ("Bajaj Finserv", None), ("Give India Donation", None),
    ("", None), (None, None), ("SHREE SWEETS", "UPI/DR/412345678901"),
]


@pytest.mark.parametrize("payee,desc,key", MATCHES)
def test_each_brand_and_alias_matches(payee, desc, key):
    assert al.plain_key(payee, desc) == key


@pytest.mark.parametrize("payee,desc", NEGATIVES)
def test_look_alikes_and_people_give_none(payee, desc):
    assert al.plain_key(payee, desc) is None


def test_every_listed_brand_has_a_test_fixture():
    assert {k for _, _, k in MATCHES} == set(al.KEYS)


def test_nothing_sensitive_is_on_the_list():
    banned = ("health", "pharm", "apollo", "hospital", "clinic", "lend", "loan", "bajaj", "kreditbee", "zerodha", "groww",
              "mutual", "sip", "donat", "ketto", "milaap", "goonj", "insur", "emi")
    for brand in al.ALLOWED:
        text = " ".join((brand.key, brand.name, *brand.aliases)).lower()
        assert not any(word in text for word in banned), brand


def test_init_db_seeds_the_table_from_the_list():
    with db.connect_system("test") as conn:
        keys = {r["key"] for r in conn.execute("SELECT key FROM allowed_merchants").fetchall()}
    assert keys == set(al.KEYS)


def test_the_database_refuses_a_merchant_key_that_is_not_listed():
    with db.connect() as conn:
        conn.execute("INSERT INTO transactions (amount, direction, txn_date, source, merchant_key) VALUES (10, 'debit', '2026-10-01', 'manual', 'swiggy')")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.connect() as conn:
            conn.execute("INSERT INTO transactions (amount, direction, txn_date, source, merchant_key) VALUES (10, 'debit', '2026-10-01', 'manual', 'apollo')")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.connect() as conn:
            conn.execute("UPDATE transactions SET merchant_key = 'rahul'")


def test_a_user_cannot_edit_the_allow_list():
    with pytest.raises(psycopg.Error):
        with db.connect() as conn:
            conn.execute("INSERT INTO allowed_merchants (key, name) VALUES ('apollo', 'Apollo')")
