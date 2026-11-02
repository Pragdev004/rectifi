"""Build a database the way Rectifi's previous version left it (the schema and seeds from before the AI phase)."""

from __future__ import annotations

from pathlib import Path

import psycopg

from rectifi import db

OLD_SCHEMA = Path(__file__).resolve().parent.parent / "fixtures" / "schema_before_ai_phase.sql"

OLD_CATEGORIES = [
    ("Food & Dining", "expense"), ("Groceries", "expense"), ("Transport", "expense"), ("Shopping", "expense"),
    ("Bills & Utilities", "expense"), ("Rent", "expense"), ("Health", "expense"), ("Education", "expense"),
    ("Entertainment", "expense"), ("Subscriptions", "expense"), ("Travel", "expense"), ("Other", "expense"),
    ("Salary & Allowance", "income"), ("Refunds", "income"), ("Other income", "income"), ("Transfers", "transfer"),
]
OLD_RULES = {"Transport": ["uber", "petrol", "fuel"], "Food & Dining": ["swiggy"], "Groceries": ["blinkit"]}


def rebuild_old_database() -> None:
    """Drop every table and recreate the old schema with the old default categories and rules. Pool is closed afterwards."""
    from rectifi import config

    db.close_pool()
    with psycopg.connect(config.database_url(), autocommit=True, options="-c app.user_id=1") as conn:
        tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]
        if tables:
            conn.execute("DROP TABLE IF EXISTS " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")
        conn.execute(OLD_SCHEMA.read_text(encoding="utf-8"))
        for name, kind in OLD_CATEGORIES:
            conn.execute("INSERT INTO categories (name, kind) VALUES (%s, %s)", (name, kind))
        for category, patterns in OLD_RULES.items():
            for pattern in patterns:
                conn.execute("INSERT INTO merchant_rules (pattern, category_id) SELECT %s, id FROM categories WHERE name = %s",
                             (pattern, category))


# ───────────── a database as it was just before multi-user support ─────────────

PRE_TENANCY_SCHEMA = Path(__file__).resolve().parent.parent / "fixtures" / "schema_before_tenancy.sql"

# One row in EVERY table, in the previous shape (no user_id anywhere).
PRE_TENANCY_ROWS = [
    "INSERT INTO categories (name, kind) VALUES ('Food & Dining', 'expense'), ('Groceries', 'expense'), ('Miscellaneous', 'expense'), ('Salary & Allowance', 'income')",
    "INSERT INTO accounts (name, last4) VALUES ('SBI Savings', '1234'), ('HDFC', '9876')",
    "INSERT INTO import_batches (account_id, filename, added) VALUES ((SELECT id FROM accounts WHERE name = 'SBI Savings'), 'oct.csv', 2)",
    "INSERT INTO transactions (account_id, amount, direction, txn_date, payee, description, upi_ref, category_id, source, import_batch_id, category_source) "
    "VALUES ((SELECT id FROM accounts WHERE name = 'SBI Savings'), 250.50, 'debit', '2026-10-05', 'Swiggy', 'UPI/DR/swiggy', '123456789012', "
    "(SELECT id FROM categories WHERE name = 'Food & Dining'), 'statement', (SELECT id FROM import_batches LIMIT 1), 'rule')",
    "INSERT INTO transactions (amount, direction, txn_date, payee, fingerprint, category_id, source) VALUES (1200.00, 'credit', '2026-10-06', 'Employer', 'fp-1', "
    "(SELECT id FROM categories WHERE name = 'Salary & Allowance'), 'manual')",
    "INSERT INTO budgets (category_id, month, limit_amount) VALUES ((SELECT id FROM categories WHERE name = 'Food & Dining'), '2026-10-01', 4000.75)",
    "INSERT INTO balance_snapshots (account_id, as_of, balance) VALUES ((SELECT id FROM accounts WHERE name = 'SBI Savings'), '2026-10-01', 15000.25)",
    "INSERT INTO merchant_rules (pattern, category_id, source) VALUES ('swiggy', (SELECT id FROM categories WHERE name = 'Food & Dining'), 'default'), "
    "('chaayos', (SELECT id FROM categories WHERE name = 'Food & Dining'), 'learned')",
    "INSERT INTO weekly_reviews (week_start, commitment) VALUES ('2026-10-26', 'no more than 3 orders')",
    "INSERT INTO phone_devices (name, token_hash) VALUES ('Pixel', 'tokenhash1')",
    "INSERT INTO sms_inbox (device_id, sender, raw_text, text_hash) VALUES ((SELECT id FROM phone_devices LIMIT 1), 'AX-BANK', 'some sms', 'texthash1')",
    "INSERT INTO sms_seen (text_hash) VALUES ('texthash2')",
    "INSERT INTO classify_jobs (batch_id, total) VALUES ((SELECT id FROM import_batches LIMIT 1), 2)",
    "INSERT INTO charts (title, prompt, code, kind, attempts) VALUES ('t', 'p', 'c', 'png', 1)",
    "INSERT INTO rag_documents (corpus, title, source, sha256, content) VALUES ('note', 'n', 'note.md', 'abc', 'body')",
    "INSERT INTO rag_chunks (document_id, corpus, content, content_hash, embed_model, embedding) VALUES ((SELECT id FROM rag_documents LIMIT 1), 'note', 'body', 'h', 'm', '{0.1,0.2}')",
    "INSERT INTO rag_chunks (txn_id, corpus, content, content_hash, embed_model, embedding) VALUES ((SELECT id FROM transactions WHERE upi_ref = '123456789012'), 'txn', 'body', 'h', 'm', '{0.1,0.2}')",
    "INSERT INTO conversations (thread_id, title) VALUES ('thread-1', 'chat')",
    "INSERT INTO trace_spans (run_id, root_run_id, name, run_type, started_at) VALUES (gen_random_uuid(), gen_random_uuid(), 'step', 'chain', now())",
    "INSERT INTO app_settings (key, value) VALUES ('categories_v2', 'true'::jsonb), ('session_epoch', '3'::jsonb), ('alerts.prefs', '{\"x\": 1}'::jsonb)",
    "INSERT INTO merchant_patterns (pattern, merchant, grp) VALUES ('chaayos', 'Chaayos', 'Other')",
    "INSERT INTO alert_rules (scope, target, period, cap) VALUES ('merchant', 'Swiggy', 'week', 1500.00)",
    "INSERT INTO alerts (dedupe_key, kind, severity, title, detail, push_title, push_body, numbers, rule_id, txn_id) VALUES ('cap:1', 'cap', 'act', 'T', 'D', 'pt', 'pb', "
    "'{}'::jsonb, (SELECT id FROM alert_rules LIMIT 1), (SELECT id FROM transactions WHERE upi_ref = '123456789012'))",
    "INSERT INTO push_subscriptions (endpoint, p256dh, auth) VALUES ('https://push.example/abc', 'p', 'a')",
]


def rebuild_pre_tenancy_database(with_rows: bool = True) -> None:
    """Drop every table and recreate the schema from just before multi-user support, with a row in every table.
    Pool is closed afterwards."""
    from rectifi import config

    db.close_pool()
    with psycopg.connect(config.database_url(), autocommit=True, options="-c app.user_id=1") as conn:
        tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]
        if tables:
            conn.execute("DROP TABLE IF EXISTS " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")
        conn.execute(PRE_TENANCY_SCHEMA.read_text(encoding="utf-8"))
        if with_rows:
            for statement in PRE_TENANCY_ROWS:
                conn.execute(statement)


def catalog_and_rows(uid: int | None = None) -> dict:
    """Everything that makes a database what it is: columns, constraints, indexes, policies, triggers, functions, the RLS
    flags, and a checksum of the rows of every table. Two equal results mean 'byte for byte the same'.
    `uid` is the person whose rows to checksum once row level security is on."""
    from rectifi import config

    kwargs = {"options": f"-c app.user_id={uid}"} if uid else {}
    shot: dict = {}
    with psycopg.connect(config.database_url(), **kwargs) as c:
        tables = [r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1").fetchall()]
        shot["tables"] = tables
        shot["columns"] = c.execute("SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
                                    "WHERE table_schema = 'public' ORDER BY 1, 2").fetchall()
        shot["constraints"] = c.execute("SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                                        "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2").fetchall()
        shot["indexes"] = c.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1").fetchall()
        shot["policies"] = c.execute("SELECT tablename, policyname, cmd, qual, with_check FROM pg_policies WHERE schemaname = 'public' ORDER BY 1, 2").fetchall()
        shot["triggers"] = c.execute("SELECT tgrelid::regclass::text, tgname, encode(tgargs, 'escape') FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1, 2").fetchall()
        shot["functions"] = c.execute("SELECT proname, md5(prosrc) FROM pg_proc WHERE pronamespace = 'public'::regnamespace ORDER BY 1").fetchall()
        shot["rls"] = c.execute("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                                "AND relkind = 'r' ORDER BY 1").fetchall()
        shot["rows"] = {t: c.execute(f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) FROM \"{t}\" x").fetchone()
                        for t in tables}
    return shot
