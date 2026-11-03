"""Your account, as a person: take your data out (export), delete everything, and the public "what is protected" facts.

Multi-user plan section 4, "API": GET /api/account/export, DELETE /api/account, GET /api/privacy. The routes are in
api/routes/account.py and only call the functions here. No langchain, no rectifi.api.

Export. One ZIP of CSV files with exactly the signed-in person's rows (row level security decides which rows; the
connection decrypts the sealed text). Money is text from a Decimal ("1250.50"), never a float. A text cell that starts with
= + - @ (or a tab or carriage return) gets a leading ' so a spreadsheet will not run it as a formula. A number is not
text, so "-500.00" stays a number. Left out on purpose: password material, device tokens, push endpoints and keys, keyed
hashes, embeddings and chart images (see EXCLUDED_TABLES and DROP_COLUMNS).

Delete. Needs the password (checked through `users`, a miss counts against the account) and the typed username. A friend's
users row is deleted and the foreign keys (ON DELETE CASCADE) remove every row in every table, sessions included. The owner
(row 1) cannot be deleted while friends exist; with no friends the owner's rows are wiped and row 1 goes back to an unclaimed
state so a new owner invite can be issued. Old backups still hold the encrypted text for up to BACKUP_DAYS days.

This module also holds the few queries on the people tables that the sealing sweep needs (sweep_state, encrypted), so
`db.connect_system` is called from one extra file only (see docs/handoffs/ws-account.md).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import date, datetime, time as dtime
from decimal import Decimal

from . import config, db, merchant_allowlist, sealed_fields, userctx, users

log = logging.getLogger("rectifi.account")

OWNER_UID = userctx.OWNER_UID
BACKUP_DAYS = 7                      # deploy/compose.oracle.yml keeps the newest 7 daily copies (tests/test_account.py checks it)

# ───────────────────────── export ─────────────────────────

# table -> (columns to leave out). Everything else in the table is exported, in column order.
DROP_COLUMNS: dict[str, tuple[str, ...]] = {
    "accounts": ("user_id",),
    "categories": ("user_id",),
    "budgets": ("user_id",),
    "balance_snapshots": ("user_id",),
    "merchant_rules": ("user_id", "pattern_blind"),
    "weekly_reviews": ("user_id",),
    "import_batches": ("user_id",),
    "merchant_patterns": ("user_id", "pattern_blind"),
    "alert_rules": ("user_id", "target_blind"),
    "alerts": ("user_id", "dedupe_key", "scope_key"),
    "sms_inbox": ("user_id", "text_hash"),
    "phone_devices": ("user_id", "token_hash"),                # the token hash is how a phone finds you: never exported
    "push_subscriptions": ("user_id", "endpoint", "p256dh", "auth"),   # the endpoint is a capability URL: whoever has it can push
    "app_settings": ("user_id",),
    "charts": ("user_id", "image_light", "image_dark"),
    "conversations": ("user_id",),
    "rag_documents": ("user_id",),
}

# Why a tenant table is not in the export. tests/test_account.py checks that every table is in one list or the other,
# so a new table has to be decided on purpose.
EXCLUDED_TABLES: dict[str, str] = {
    "sms_seen": "only keyed hashes, nothing a person can read",
    "classify_jobs": "internal job bookkeeping",
    "trace_spans": "internal run traces of the owner's chat",
    "rag_chunks": "vectors made from other exported data",
}

# The transactions export reads the readable UPI reference and the category name, not the keyed hashes.
TRANSACTIONS_SQL = """
SELECT t.id, t.txn_date, t.amount, t.direction, t.payee, t.description, t.note, t.source,
       COALESCE(t.upi_ref_sealed, CASE WHEN left(t.upi_ref, 3) = 'b1:' THEN NULL ELSE t.upi_ref END) AS upi_ref,
       a.name AS account, c.name AS category, t.category_source, t.category_confidence, t.category_reason,
       t.merchant_key, t.raw_text, t.created_at
FROM transactions t
LEFT JOIN accounts a ON a.id = t.account_id
LEFT JOIN categories c ON c.id = t.category_id
ORDER BY t.txn_date, t.id
"""

EXPORT_ORDER = ("accounts", "categories", "transactions", "budgets", "balance_snapshots", "merchant_rules", "weekly_reviews",
                "import_batches", "merchant_patterns", "alert_rules", "alerts", "sms_inbox", "phone_devices",
                "push_subscriptions", "app_settings", "charts", "conversations", "rag_documents")

README = """Rectifi export
==============
Made {stamp}. One CSV per table, holding only your own rows.

* Money is text with two decimals (for example 1250.50). Nothing was rounded or turned into a float.
* Dates are ISO (2026-10-05); times are ISO with a time zone.
* A text cell that would start with = + - or @ has a ' in front, so a spreadsheet shows it instead of running it as a
  formula. Remove the ' if you want the original text. Numbers are never changed.
* Files are UTF-8 (no byte order mark). In Excel use Data > From Text/CSV and pick UTF-8.
* Not included: your password or keys, phone tokens, push addresses, keyed hashes, chart pictures, search vectors.
* Your AI chat messages live in a separate store and are not in this export.
"""


def defuse(value: str) -> str:
    """Text a spreadsheet could run as a formula gets a leading '."""
    return "'" + value if value and value[0] in "=+-@\t\r" else value


def cell(value) -> str:
    """One CSV cell. Numbers and dates are not text, so they are written as they are; text is defused."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")                       # exact: never through float
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return defuse(json.dumps(value, separators=(",", ":"), default=str, ensure_ascii=False))
    return defuse(str(value))


def _csv(columns: list[str], rows: list[dict]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([cell(row[c]) for c in columns])
    return out.getvalue().encode("utf-8")


def export_tables() -> dict[str, tuple[list[str], list[dict]]]:
    """{table: (columns, rows)} for the current person, decrypted by the connection."""
    found: dict[str, tuple[list[str], list[dict]]] = {}
    with db.connect() as conn:
        for table in EXPORT_ORDER:
            if table == "transactions":
                cursor = conn.execute(TRANSACTIONS_SQL)
            else:
                cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')
            columns = [d.name for d in cursor.description if d.name not in DROP_COLUMNS.get(table, ())]
            found[table] = (columns, cursor.fetchall())
    return found


def build_export(now: datetime | None = None) -> tuple[bytes, str]:
    """(zip bytes, file name) with every exportable table of the person bound in userctx."""
    now = now or datetime.now().astimezone()
    tables = export_tables()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", README.format(stamp=now.strftime("%Y-%m-%d %H:%M %Z")))
        for table, (columns, rows) in tables.items():
            archive.writestr(f"{table}.csv", _csv(columns, rows))
    return buffer.getvalue(), f"rectifi-export-{now.strftime('%Y%m%d')}.zip"


# ───────────────────────── people-table helpers (the only connect_system calls outside users.py) ─────────────────────────

def sweep_state(uid: int) -> tuple[str | None, bool]:
    """(users.sweep_state, users.encrypted) for a person."""
    with db.connect_system("account: read the sweep state") as conn:
        row = conn.execute("SELECT sweep_state, encrypted FROM users WHERE id = %s", (int(uid),)).fetchone()
    return (row["sweep_state"], bool(row["encrypted"])) if row else (None, False)


def set_sweep_state(uid: int, state: str | None) -> None:
    with db.connect_system("account: save the sweep progress") as conn:
        conn.execute("UPDATE users SET sweep_state = %s WHERE id = %s", (state, int(uid)))


def blind_fp(uid: int) -> str | None:
    """The fingerprint of the server blind key that made this person's ingest hashes (never the key itself)."""
    with db.connect_system("account: read the blind key fingerprint") as conn:
        row = conn.execute("SELECT blind_fp FROM users WHERE id = %s", (int(uid),)).fetchone()
    return row["blind_fp"] if row else None


def set_blind_fp(uid: int, fingerprint: str) -> None:
    with db.connect_system("account: save the blind key fingerprint") as conn:
        conn.execute("UPDATE users SET blind_fp = %s WHERE id = %s", (fingerprint, int(uid)))


def finish_sweep(uid: int) -> None:
    """The last step of the owner sweep: from now the database trigger refuses plain text for this person."""
    with db.connect_system("account: finish the sweep") as conn:
        conn.execute("UPDATE users SET encrypted = true, sweep_state = 'done' WHERE id = %s", (int(uid),))


# ───────────────────────── delete ─────────────────────────

def _friend_count(conn) -> int:
    return conn.execute("SELECT count(*) AS n FROM users WHERE id <> %s", (OWNER_UID,)).fetchone()["n"]


def _forget_in_memory(uid: int) -> None:
    from . import categorize

    for key in [k for k in list(categorize._index_cache) if k and k[0] == uid]:     # noqa: SLF001  (a cache keyed by person)
        categorize._index_cache.pop(key, None)                                        # noqa: SLF001


def _wipe_owner_rows() -> None:
    """The owner's rows, deleted under the owner's own context (row level security keeps it to user 1). Children first;
    the foreign keys' ON DELETE rules do the rest. tests/test_account.py checks the list covers db.TENANT_TABLES."""
    from psycopg import errors

    order = ("rag_chunks", "alerts", "alert_rules", "push_subscriptions", "sms_inbox", "sms_seen", "transactions", "budgets",
             "balance_snapshots", "merchant_rules", "merchant_patterns", "weekly_reviews", "classify_jobs", "import_batches",
             "phone_devices", "accounts", "categories", "charts", "rag_documents", "trace_spans", "app_settings")
    with db.connect() as conn:
        threads = [r["thread_id"] for r in conn.execute("SELECT thread_id FROM conversations").fetchall()]
        for table in order:
            conn.execute(f'DELETE FROM "{table}"')
        conn.execute("DELETE FROM conversations")
        for library in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):          # the chat's own store, by thread
            try:
                with conn.transaction():
                    conn.execute(f'DELETE FROM "{library}" WHERE thread_id = ANY(%s)', (threads,))
            except errors.UndefinedTable:
                pass


def delete_account(uid: int, username: str, password: str, confirm_username: str) -> dict:
    """Delete the signed-in person's account and every row they own. Raises users.AuthError (status, code, message, field)."""
    if not config.multiuser():
        raise users.AuthError(400, "not_multiuser", "Accounts only exist when Rectifi runs in multi-user mode.")
    if not isinstance(confirm_username, str) or confirm_username.strip().lower() != (username or "").lower():
        raise users.AuthError(422, "invalid", "Type your username exactly to confirm.", "confirm_username")
    users._verify_current(int(uid), password)             # counts a wrong password against the account  # noqa: SLF001
    if int(uid) == OWNER_UID:
        with db.connect_system("account: count the other people") as conn:
            if _friend_count(conn):
                raise users.AuthError(409, "owner_has_friends", "Other people still use this Rectifi. Remove their accounts first.")
        with userctx.bound(userctx.UserContext(uid=OWNER_UID, strict=False)):
            _wipe_owner_rows()
        with db.connect_system("account: reset the owner row") as conn:
            conn.execute("DELETE FROM user_sessions WHERE user_id = %s", (OWNER_UID,))
            conn.execute("UPDATE users SET username = 'owner', scrypt_salt = NULL, verifier = NULL, dek_by_password = NULL, "
                         "dek_by_recovery = NULL, recovery_salt = NULL, public_key = NULL, private_key_wrapped = NULL, "
                         "ingest_salt = NULL, session_epoch = session_epoch + 1, encrypted = false, confirmed_at = NULL, "
                         "sweep_state = NULL, failures = '[]'::jsonb, locked_until = NULL WHERE id = %s", (OWNER_UID,))
        db.seed_user(OWNER_UID)
    else:
        with db.connect_system("account: delete a person") as conn:
            deleted = conn.execute("DELETE FROM users WHERE id = %s", (int(uid),)).rowcount
        if deleted != 1:
            raise users.AuthError(404, "not_found", "That account does not exist.")
    _forget_in_memory(int(uid))
    log.info("account deleted (uid %s)", uid)           # an id only: never a name or a value
    return {"ok": True}


# ───────────────────────── /api/privacy ─────────────────────────

WHO_CAN_READ = (
    {"who": "Another friend, through the app",
     "plain": "No (the app checks, and the database shows each person only their own rows)",
     "sealed": "No (the same checks, and a different key)"},
    {"who": "Someone with a database dump or a stolen backup",
     "plain": "Yes, readable",
     "sealed": "No, unless they crack the password (scrypt makes each guess slow). The keyed hashes of UPI references and SMS "
               "need the server's blind key too, which is not in the database or its backups."},
    {"who": "The person who runs the server (the owner), browsing the database",
     "plain": "Yes, readable",
     "sealed": "No"},
    {"who": "The owner changing the code, or reading server memory or the login request",
     "plain": "Yes",
     "sealed": "Yes. Passwords and SMS pass through the server in clear"},
)

LIMITS = (
    "There is no password reset. The recovery code is the only way back. If you lose your password and your recovery code, "
    "your sealed text is gone for good.",
    "The encryption protects data at rest. While the server works, your password and your text are in its memory in clear.",
    "The keyed hashes of UPI references and SMS use a server secret (RECTIFI_BLIND_KEY) that is not in the database or its backups. "
    "They stop a leaked database, not the owner: whoever holds that secret and the database can test guesses against them.",
    "Rectifi's owner can change the code, so he could read your text if he chose to. Trust him, or do not join.",
    "The encryption is built from standard parts (AES-256-GCM, X25519, scrypt) put together by one person. No outside expert has reviewed it.",
    "Amounts, dates, categories, account names, last 4 digits and spending caps are plain so alerts work while you are signed out.",
    "A payment to a well-known shop on the list below also has its shop name stored in plain text. Every other payee is sealed.",
    "Your phone's alert address and keys are plain, because a push must be sent while you are signed out.",
    "Backups can hold old copies. After you delete your account, its sealed text stays in backups for up to the backup days shown here.",
    "Server memory and swap can hold decrypted text until it is overwritten.",
    "The owner's own AI tables (chat, charts, library, traces) are not sealed in this phase. They hold only the owner's data.",
    "The model that sorts payments and answers the owner's chat is the one named under 'model'. Friends' data is never sent to any model.",
)


def _model_where() -> str:
    """'local' when the model runs on this machine, else 'cloud'."""
    provider = config.llm_provider()
    if provider == "ollama":
        host = config.ollama_base_url()
    elif provider == "openai_compat":
        host = config.openai_style_settings("openai_compat").get("base_url", "")
    else:
        return "cloud"
    return "local" if any(h in host for h in ("localhost", "127.0.0.1", "[::1]")) else "cloud"


def privacy_facts() -> dict:
    """Facts only, no user data: this is what the public Privacy page shows. Read from the same registries the code uses."""
    multi = config.multiuser()
    return {
        "mode": "multiuser" if multi else "laptop",
        "encryption_at_rest": multi,
        "model_provider": _model_where(),
        "model": config.llm_provider(),
        "ai_for_friends": False,
        "backup_days": BACKUP_DAYS,
        "sealed_fields": [f"{t}.{c}" for t, c in sealed_fields.all_sealed()],
        "keyed_hash_fields": [f"{t}.{c}" for t, c in sealed_fields.all_blind()],
        "hidden_key_fields": sealed_fields.all_hidden(),
        "plain_fields": list(sealed_fields.PLAIN_ON_PURPOSE),
        "unsealed_owner_tables": list(sealed_fields.OWNER_ONLY_TABLES),
        "merchant_allowlist": [{"key": b.key, "name": b.name} for b in merchant_allowlist.ALLOWED],
        "who_can_read": [dict(row) for row in WHO_CAN_READ],
        "limits": list(LIMITS),
        "recovery": "A recovery code is the only way back. There is no email and no admin reset.",
    }
