"""Database connection pool + schema setup. Raw SQL with psycopg 3, no ORM."""

from __future__ import annotations

import atexit
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from . import config

_pool: ConnectionPool | None = None
_pool_url: str | None = None


class DatabaseUnavailable(RuntimeError):
    """Postgres can't be reached. The message is safe to show: it never contains the password."""

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

DEFAULT_CATEGORIES = [
    ("Food & Dining", "expense"),
    ("Groceries", "expense"),
    ("Transport", "expense"),
    ("Shopping", "expense"),
    ("Bills & Utilities", "expense"),
    ("Rent", "expense"),
    ("Health", "expense"),
    ("Education", "expense"),
    ("Entertainment", "expense"),
    ("Subscriptions", "expense"),
    ("Travel", "expense"),
    ("Other", "expense"),
    ("Salary & Allowance", "income"),
    ("Refunds", "income"),
    ("Other income", "income"),
    ("Transfers", "transfer"),
]

DEFAULT_RULES = {
    "Food & Dining": ["swiggy", "zomato", "dominos", "mcdonald", "starbucks", "eatsure", "kfc", "cafe"],
    "Groceries": ["bigbasket", "blinkit", "zepto", "dmart", "jiomart", "instamart", "reliance fresh"],
    "Transport": ["uber", "ola", "rapido", "irctc", "metro", "redbus", "fastag", "petrol", "fuel"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa"],
    "Bills & Utilities": ["airtel", "jio", "vodafone", "vi recharge", "bescom", "kseb", "tneb", "electricity", "broadband", "gas"],
    "Subscriptions": ["netflix", "spotify", "hotstar", "prime video", "youtube premium", "chatgpt", "claude.ai", "google one"],
    "Health": ["apollo", "pharmeasy", "1mg", "netmeds", "medplus", "hospital", "clinic"],
    "Entertainment": ["bookmyshow", "pvr", "inox", "steam"],
    "Education": ["coursera", "udemy", "iitm", "college fee"],
    "Travel": ["makemytrip", "goibibo", "cleartrip", "indigo", "airbnb", "oyo"],
}


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


def init_db() -> None:
    """Create tables and seed default categories + merchant rules. Idempotent."""
    with connect() as conn:
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        for name, kind in DEFAULT_CATEGORIES:
            conn.execute(
                "INSERT INTO categories (name, kind) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
                (name, kind),
            )
        for category, patterns in DEFAULT_RULES.items():
            for pattern in patterns:
                conn.execute(
                    """INSERT INTO merchant_rules (pattern, category_id)
                       SELECT %s, id FROM categories WHERE name = %s
                       ON CONFLICT (pattern) DO NOTHING""",
                    (pattern, category),
                )
