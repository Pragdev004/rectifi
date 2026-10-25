"""Database connection pool + schema setup. Raw SQL with psycopg 3, no ORM."""

from __future__ import annotations

import atexit
import logging
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from . import config
from .categorize import FALLBACK_EXPENSE, FALLBACK_INCOME, LEGACY_FALLBACK_EXPENSE

log = logging.getLogger("rectifi.db")

_pool: ConnectionPool | None = None
_pool_url: str | None = None


class DatabaseUnavailable(RuntimeError):
    """Postgres can't be reached. The message is safe to show: it never contains the password."""

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

DEFAULT_CATEGORIES = [
    ("Food & Dining", "expense"),
    ("Groceries", "expense"),
    ("Transport", "expense"),
    ("Fuel", "expense"),
    ("Shopping", "expense"),
    ("Bills & Utilities", "expense"),
    ("Rent", "expense"),
    ("Health", "expense"),
    ("Education", "expense"),
    ("Entertainment", "expense"),
    ("Subscriptions", "expense"),
    ("Travel", "expense"),
    ("Personal Care", "expense"),
    ("EMI & Loans", "expense"),
    ("Fees & Charges", "expense"),
    ("Gifts & Donations", "expense"),
    ("Cash Withdrawal", "expense"),        # an expense on purpose: for a student, ATM cash is spending
    (FALLBACK_EXPENSE, "expense"),         # "Miscellaneous", was "Other"
    ("Salary & Allowance", "income"),
    ("Refunds", "income"),
    ("Interest & Cashback", "income"),
    (FALLBACK_INCOME, "income"),
    ("Transfers", "transfer"),
    ("Investments", "transfer"),           # SIP, stocks, FD, PPF: money you still own, so it is not spending
]

# Substring rules ("pattern is somewhere in payee + narration", longest first). Words that hide inside other words
# ("rent" in "parent", "emi" in "premium", "atm") are NOT here: they are word-boundary heuristics in categorize.py.
DEFAULT_RULES = {
    "Food & Dining": ["swiggy", "zomato", "dominos", "mcdonald", "starbucks", "eatsure", "kfc", "cafe"],
    "Groceries": ["bigbasket", "blinkit", "zepto", "dmart", "jiomart", "instamart", "reliance fresh"],
    "Transport": ["uber", "ola", "rapido", "irctc", "metro", "redbus", "fastag"],
    "Fuel": ["petrol", "fuel", "indian oil", "indianoil", "iocl", "bpcl", "hpcl", "bharat petroleum", "hindustan petroleum"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa"],
    "Bills & Utilities": ["airtel", "jio", "vodafone", "vi recharge", "bescom", "kseb", "tneb", "electricity", "broadband", "gas"],
    "Subscriptions": ["netflix", "spotify", "hotstar", "prime video", "youtube premium", "chatgpt", "claude.ai", "google one"],
    "Health": ["apollo", "pharmeasy", "1mg", "netmeds", "medplus", "hospital", "clinic"],
    "Entertainment": ["bookmyshow", "pvr", "inox", "steam"],
    "Education": ["coursera", "udemy", "iitm", "college fee"],
    "Travel": ["makemytrip", "goibibo", "cleartrip", "indigo", "airbnb", "oyo"],
    "Personal Care": ["salon", "barber", "urban company", "urbancompany", "jawed habib", "naturals"],
    "EMI & Loans": ["bajaj finserv", "bajaj finance", "kreditbee", "moneyview", "zestmoney", "lazypay"],
    "Fees & Charges": ["sms charge", "annual fee", "amc charge", "processing fee", "late payment fee", "penalty", "atm fee"],
    "Gifts & Donations": ["donation", "ketto", "milaap", "give india", "giveindia", "goonj"],
    "Cash Withdrawal": ["atm wdl", "atm cash", "cash withdrawal", "nfs atm", "atm-cash"],
    "Interest & Cashback": ["cashback", "interest credit", "interest paid", "int pd", "int.pd"],
    "Investments": ["zerodha", "groww", "upstox", "kuvera", "indmoney", "smallcase", "paytm money", "mutual fund", "iccl"],
}

MIGRATION_MARKER = "categories_v2"       # app_settings key: the one-time rule moves have been done


def describe_target(url: str) -> str:
    """'localhost:5432/rectifi' — where we are trying to connect, without the user or password."""
    try:
        parts = conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        return "the configured database"
    return f"{parts.get('host') or 'localhost'}:{parts.get('port') or 5432}/{parts.get('dbname') or ''}"


def get_pool() -> ConnectionPool:
    """One shared pool. Fails fast (about 5 seconds) with DatabaseUnavailable if Postgres isn't there."""
    global _pool, _pool_url
    url = config.database_url()
    if _pool is None or _pool_url != url:
        if _pool is not None:
            _pool.close()
            _pool, _pool_url = None, None
        pool = ConnectionPool(
            url, min_size=1, max_size=5, open=False,
            kwargs={"row_factory": dict_row, "connect_timeout": 3},
        )
        try:
            pool.open(wait=True, timeout=5)
        except (PoolTimeout, psycopg.OperationalError) as exc:
            pool.close()
            raise DatabaseUnavailable(
                f"Can't reach the database at {describe_target(url)}. "
                "Start it with `python scripts/dev.py db`, or check DATABASE_URL in .env."
            ) from exc
        _pool, _pool_url = pool, url
    return _pool


def close_pool() -> None:
    global _pool, _pool_url
    if _pool is not None:
        _pool.close()
    _pool, _pool_url = None, None


atexit.register(close_pool)


@contextmanager
def connect():
    """A connection inside a transaction: commits when the block ends, rolls back on an exception."""
    with get_pool().connection() as conn:
        yield conn


def _migrate_categories(conn) -> None:
    """Bring an older database to the current categories and rules. Safe to run again; a second run changes nothing.

    1. Rename "Other" to "Miscellaneous" IN PLACE (same id), so its transactions, budgets and rules carry over. If the
       person already has both, leave both alone: the app uses "Miscellaneous" as the catch-all from now on.
    2. Add the new default categories.
    3. (once) Move the default petrol/fuel rules from Transport to Fuel. Done once only, so a person who later teaches
       "petrol -> Transport" on purpose is not overruled at the next start.
    4. (once) Mark the rules the person taught as 'learned' (every pattern that is not a built-in one).
    5. Add the new default rules.
    """
    has = {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM categories WHERE name IN (%s, %s)", (LEGACY_FALLBACK_EXPENSE, FALLBACK_EXPENSE)).fetchall()}
    if LEGACY_FALLBACK_EXPENSE in has and FALLBACK_EXPENSE not in has:
        conn.execute("UPDATE categories SET name = %s WHERE id = %s", (FALLBACK_EXPENSE, has[LEGACY_FALLBACK_EXPENSE]))
        log.info("Renamed category %r to %r (same id).", LEGACY_FALLBACK_EXPENSE, FALLBACK_EXPENSE)
    elif LEGACY_FALLBACK_EXPENSE in has:
        log.info("Both %r and %r exist; leaving both alone.", LEGACY_FALLBACK_EXPENSE, FALLBACK_EXPENSE)

    for name, kind in DEFAULT_CATEGORIES:
        conn.execute("INSERT INTO categories (name, kind) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING", (name, kind))

    first_time = conn.execute("SELECT 1 FROM app_settings WHERE key = %s", (MIGRATION_MARKER,)).fetchone() is None
    if first_time:
        conn.execute(
            """UPDATE merchant_rules SET category_id = (SELECT id FROM categories WHERE name = 'Fuel')
               WHERE pattern IN ('petrol', 'fuel') AND source = 'default'
                 AND category_id = (SELECT id FROM categories WHERE name = 'Transport')""")
        every_default = [p for patterns in DEFAULT_RULES.values() for p in patterns]
        conn.execute("UPDATE merchant_rules SET source = 'learned' WHERE source = 'default' AND pattern <> ALL(%s)", (every_default,))
        conn.execute("INSERT INTO app_settings (key, value) VALUES (%s, 'true'::jsonb) ON CONFLICT (key) DO NOTHING", (MIGRATION_MARKER,))

    for category, patterns in DEFAULT_RULES.items():
        for pattern in patterns:
            conn.execute(
                """INSERT INTO merchant_rules (pattern, category_id)
                   SELECT %s, id FROM categories WHERE name = %s
                   ON CONFLICT (pattern) DO NOTHING""",
                (pattern, category),
            )


def init_db() -> None:
    """Create tables, move an older database forward, and seed default categories + merchant rules. Idempotent."""
    with connect() as conn:
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        _migrate_categories(conn)
