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

from . import config, merchant_allowlist, sealed_fields, userctx
from .categorize import FALLBACK_EXPENSE, FALLBACK_INCOME, LEGACY_FALLBACK_EXPENSE

log = logging.getLogger("rectifi.db")

_pool: ConnectionPool | None = None
_pool_url: str | None = None


class DatabaseUnavailable(RuntimeError):
    """Postgres can't be reached. The message is safe to show: it never contains the password."""



class TenancyMigrationNeeded(RuntimeError):
    """This database was made before multi-user support. Starting would change it, so Rectifi stops and asks first (D10).
    The message is safe to show. Nothing has been changed when this is raised."""


class UnsafeDatabaseRole(RuntimeError):
    """The app logs in as a superuser or a role with BYPASSRLS, so row level security would be skipped silently."""


SCHEMA_FILE = Path(__file__).with_name("schema.sql")
PART_B = "-- @@ PART B @@"
INIT_LOCK_ID = 7_314_190             # pg_advisory_xact_lock: two processes starting at once take turns running the schema
AUTO_MIGRATE_SUFFIXES = ("_test", "_demo")     # only these databases may be upgraded without `dev.py migrate-tenancy`
TENANCY_MARKER = "tenancy_v1"        # app_settings key (user 1): the upgrade to per-person rows has been done

# Every table that holds a person's rows. Each has user_id, FORCE row level security and a policy (tests/test_rls.py).
TENANT_TABLES = (
    "accounts", "categories", "transactions", "budgets", "balance_snapshots", "merchant_rules", "weekly_reviews",
    "phone_devices", "sms_inbox", "sms_seen", "import_batches", "classify_jobs", "charts", "rag_documents", "rag_chunks",
    "conversations", "trace_spans", "app_settings", "merchant_patterns", "alert_rules", "alerts", "push_subscriptions",
)
# Money columns whose totals must be the same before and after the upgrade (table, column).
MONEY_COLUMNS = (("transactions", "amount"), ("budgets", "limit_amount"), ("balance_snapshots", "balance"), ("alert_rules", "cap"))

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


def _opening_row_factory(ctx):
    """dict rows whose sealed cells ('rx1.' / 'rs1.') are opened for this person. Only used in multi-user mode."""
    from . import vault

    def factory(cursor):
        make = dict_row(cursor)

        def build(values):
            row = make(values)
            for key, value in row.items():
                if value.__class__ is str and (value.startswith("rx1.") or value.startswith("rs1.")):
                    row[key] = vault.open_cell(value, ctx)
            return row

        return build

    return factory


@contextmanager
def connect():
    """A connection inside a transaction: commits when the block ends, rolls back on an exception.

    It belongs to the current person (userctx.current()): the first statement sets app.user_id for this transaction only,
    so row level security shows just their rows, and the GUC is empty again after commit or rollback. In multi-user mode
    sealed cells are opened on the way out; with no key a strict person gets VaultLocked, anyone else gets None.
    """
    ctx = userctx.current()
    with get_pool().connection() as conn:
        conn.execute("SELECT set_config('app.user_id', %s, true)", (str(ctx.uid),))
        conn.row_factory = _opening_row_factory(ctx) if config.multiuser() else dict_row
        yield conn


@contextmanager
def connect_system(reason: str):
    """A connection for the few jobs that act on no one person: init_db, sign-up and login (users, invites, sessions), the
    SMS device lookup, the alert ticker listing people. It sees only the people tables and device lookup; tenant tables
    show nothing without a person. `reason` says why, and a test allow-lists the callers (tests/test_rls.py)."""
    if not reason or not reason.strip():
        raise ValueError("connect_system needs a reason.")
    log.debug("system connection: %s", reason)
    with get_pool().connection() as conn:
        conn.execute("SELECT set_config('app.scope', 'system', true)")
        conn.row_factory = dict_row
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
        conn.execute("INSERT INTO categories (name, kind) VALUES (%s, %s) ON CONFLICT (user_id, name) DO NOTHING", (name, kind))

    first_time = conn.execute("SELECT 1 FROM app_settings WHERE key = %s", (MIGRATION_MARKER,)).fetchone() is None
    if first_time:
        conn.execute(
            """UPDATE merchant_rules SET category_id = (SELECT id FROM categories WHERE name = 'Fuel')
               WHERE pattern IN ('petrol', 'fuel') AND source = 'default'
                 AND category_id = (SELECT id FROM categories WHERE name = 'Transport')""")
        every_default = [p for patterns in DEFAULT_RULES.values() for p in patterns]
        conn.execute("UPDATE merchant_rules SET source = 'learned' WHERE source = 'default' AND pattern <> ALL(%s)", (every_default,))
        conn.execute("INSERT INTO app_settings (key, value) VALUES (%s, 'true'::jsonb) ON CONFLICT (user_id, key) DO NOTHING", (MIGRATION_MARKER,))

    for category, patterns in DEFAULT_RULES.items():
        for pattern in patterns:
            conn.execute(
                """INSERT INTO merchant_rules (pattern, category_id)
                   SELECT %s, id FROM categories WHERE name = %s
                   ON CONFLICT (user_id, pattern_blind) DO NOTHING""",
                (pattern, category),
            )




# ───────────── the role the app logs in as (decision D11) ─────────────

def _role_attributes(conn) -> dict:
    return conn.execute("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()


def role_problems(conn=None) -> list[str]:
    """Plain-English problems with the login role: a superuser or a BYPASSRLS role skips row level security silently."""
    if conn is None:
        with connect_system("role check") as own:
            row = _role_attributes(own)
    else:
        row = _role_attributes(conn)
    problems = []
    if row["rolsuper"]:
        problems.append(f"The database login '{row['rolname']}' is a superuser, so row level security is skipped.")
    if row["rolbypassrls"]:
        problems.append(f"The database login '{row['rolname']}' has BYPASSRLS, so row level security is skipped.")
    return problems


def assert_app_role(conn=None, *, strict: bool | None = None) -> list[str]:
    """Multi-user or public mode refuses to start as a superuser or BYPASSRLS role. Laptop mode only warns (doctor shows it).
    Returns the problems found."""
    problems = role_problems(conn)
    if not problems:
        return problems
    if strict is None:
        strict = config.multiuser() or config.public_mode()
    message = (" ".join(problems) + " Log in with a plain role: see db/init.sql (the 'rectifi' role) and docs/HOSTING.md."
               " An existing Docker volume keeps its old role: recreate it with the new compose file.")
    if strict:
        raise UnsafeDatabaseRole(message)
    log.warning(message)
    return problems


# ───────────── schema, the tenancy upgrade, seeding ─────────────

def _dbname() -> str:
    return conninfo_to_dict(config.database_url()).get("dbname") or ""


def _may_auto_migrate() -> bool:
    """Only a throwaway database (name ends in _test or _demo) may be upgraded without the owner's say-so."""
    return _dbname().endswith(AUTO_MIGRATE_SUFFIXES)


def _tables_before_tenancy(conn) -> list[str]:
    """Tenant tables that exist but have no user_id yet. Empty for a new database and for one already upgraded."""
    rows = conn.execute(
        """SELECT c.relname FROM pg_class c
           WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND c.relname = ANY(%s)
             AND NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'user_id' AND NOT a.attisdropped)""",
        (list(TENANT_TABLES),)).fetchall()
    return sorted(r["relname"] for r in rows)


def _existing_tenant_tables(conn) -> list[str]:
    rows = conn.execute("SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND c.relname = ANY(%s)",
                        (list(TENANT_TABLES),)).fetchall()
    return sorted(r["relname"] for r in rows)


def _snapshot(conn, tables: list[str]) -> dict:
    """Row counts and money totals for the tables in `tables`. Run before and after the upgrade; they must match."""
    shot: dict = {}
    for table in tables:
        shot[table] = conn.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()["n"]
    for table, column in MONEY_COLUMNS:
        if table in tables:
            shot[f"sum {table}.{column}"] = conn.execute(f'SELECT COALESCE(SUM("{column}"), 0) AS s FROM "{table}"').fetchone()["s"]
    return shot


def _migrate_tenancy(conn) -> None:
    """Between Part A and Part B of schema.sql, on a database made before multi-user support: drop the one-person unique
    keys and single-column foreign keys. Part B then fills user_id with 1 and adds the per-person versions. All inside the
    one transaction of init_db, so any failure leaves the old database exactly as it was.

    The two generic queries find the old objects by shape, so they do not depend on Postgres' automatic names."""
    tenant = list(TENANT_TABLES)
    old_fks = conn.execute(
        """SELECT cl.relname AS tbl, c.conname FROM pg_constraint c
           JOIN pg_class cl ON cl.oid = c.conrelid JOIN pg_class ref ON ref.oid = c.confrelid
           WHERE c.contype = 'f' AND cl.relnamespace = 'public'::regnamespace AND cl.relname = ANY(%s)
             AND ref.relname = ANY(%s) AND array_length(c.conkey, 1) = 1""", (tenant, tenant)).fetchall()
    for row in old_fks:
        conn.execute(f'ALTER TABLE "{row["tbl"]}" DROP CONSTRAINT "{row["conname"]}"')
    old_unique = conn.execute(
        """SELECT cl.relname AS tbl, i.indexrelid::regclass::text AS idx, c.conname
           FROM pg_index i JOIN pg_class cl ON cl.oid = i.indrelid
           LEFT JOIN pg_constraint c ON c.conindid = i.indexrelid AND c.contype = 'u'
           WHERE cl.relnamespace = 'public'::regnamespace AND cl.relname = ANY(%s) AND i.indisunique AND NOT i.indisprimary
             AND NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = i.indrelid AND a.attname = 'user_id' AND a.attnum = ANY(i.indkey))
             AND i.indexrelid::regclass::text <> 'phone_devices_token_hash_key'""", (tenant,)).fetchall()
    for row in old_unique:
        if row["conname"]:
            conn.execute(f'ALTER TABLE "{row["tbl"]}" DROP CONSTRAINT "{row["conname"]}"')
        else:
            conn.execute(f'DROP INDEX "{row["idx"]}"')


def _verify_migration(conn, before: dict) -> None:
    """Re-count as user 1 with row level security on. Any difference raises, and init_db's transaction rolls back."""
    conn.execute("SELECT set_config('app.user_id', '1', true)")
    tables = [key for key in before if not key.startswith("sum ")]
    after = _snapshot(conn, tables)
    for key, was in before.items():
        if after.get(key) != was:
            raise RuntimeError(f"The upgrade to per-person rows was stopped and undone: {key} was {was} and would have become "
                               f"{after.get(key)}. Nothing was changed.")
    conn.execute("SELECT set_config('app.user_id', '', true)")


def _seed_allowed_merchants(conn) -> None:
    for key, name in merchant_allowlist.rows():
        conn.execute("INSERT INTO allowed_merchants (key, name) VALUES (%s, %s) "
                     "ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name WHERE allowed_merchants.name <> EXCLUDED.name", (key, name))


def _ensure_sealed_triggers(conn) -> None:
    """One trigger per table in rectifi/sealed_fields.py. Re-made only when the list changed, so a normal start takes no locks."""
    from psycopg import sql

    existing = {r["relname"]: [a.decode() for a in bytes(r["tgargs"]).split(b"\0")[:-1]] for r in conn.execute(
        """SELECT c.relname, t.tgargs FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
           WHERE t.tgname = 'b_enforce_sealed' AND c.relnamespace = 'public'::regnamespace""").fetchall()}
    for table, spec in sealed_fields.SEALED.items():
        if not spec.enforce:
            continue
        args = [",".join(spec.sealed), ",".join(spec.blind), spec.when[0] if spec.when else "", spec.when[1] if spec.when else ""]
        if existing.get(table) == args:
            continue
        conn.execute(sql.SQL("DROP TRIGGER IF EXISTS b_enforce_sealed ON {}").format(sql.Identifier(table)))
        conn.execute(sql.SQL("CREATE TRIGGER b_enforce_sealed BEFORE INSERT OR UPDATE ON {} FOR EACH ROW EXECUTE FUNCTION enforce_sealed({})")
                     .format(sql.Identifier(table), sql.SQL(", ").join(sql.Literal(a) for a in args)))


def _refuse_message() -> str:
    return (f"The database '{_dbname()}' was made before multi-user support, so it needs a one-time upgrade "
            "(every row is given to the owner, user 1). Nothing has been changed. "
            "Back it up first (pg_dump), then run:  python scripts/dev.py migrate-tenancy")


def init_db(*, migrate: bool = False) -> None:
    """Create tables, move an older database forward, and seed default categories + merchant rules. Idempotent.

    A database made before multi-user support is upgraded here only when its name ends in _test or _demo, or when
    `migrate=True` (the command `dev.py migrate-tenancy`, after the owner typed yes). Any other old database raises
    TenancyMigrationNeeded and is left exactly as it was."""
    part_a, part_b = SCHEMA_FILE.read_text(encoding="utf-8").split(PART_B)
    with connect_system("init_db") as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (INIT_LOCK_ID,))
        assert_app_role(conn)
        old = _tables_before_tenancy(conn)
        before = None
        if old:
            if not (migrate or _may_auto_migrate()):
                raise TenancyMigrationNeeded(_refuse_message())
            before = _snapshot(conn, _existing_tenant_tables(conn))
        conn.execute(part_a)
        if old:
            _migrate_tenancy(conn)
        conn.execute(part_b)
        _ensure_sealed_triggers(conn)
        _seed_allowed_merchants(conn)
        if before is not None:
            _verify_migration(conn, before)
        conn.execute("SELECT set_config('app.user_id', '1', true)")
        conn.execute("INSERT INTO app_settings (user_id, key, value) VALUES (1, %s, 'true'::jsonb) ON CONFLICT (user_id, key) DO NOTHING",
                     (TENANCY_MARKER,))
        conn.execute("SELECT set_config('app.user_id', '', true)")
        user_ids = [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id").fetchall()]
    for uid in user_ids:
        seed_user(uid)


def seed_user(uid: int) -> None:
    """Give one person their own copy of the default categories and merchant rules (decision D5). Safe to run again.
    Sign-up calls this for each new person."""
    with userctx.bound(userctx.UserContext(uid=uid, strict=False)):
        with connect() as conn:
            _migrate_categories(conn)
