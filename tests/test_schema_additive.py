"""The new tables and columns are additive: init on a database built with the old schema keeps every row."""

from decimal import Decimal

from support.olddb import rebuild_old_database

from rectifi import db

NEW_TABLES = {"classify_jobs", "charts", "rag_documents", "rag_chunks", "conversations", "trace_spans", "app_settings"}
NEW_TXN_COLUMNS = {"category_source", "category_confidence", "category_reason", "suggested_category_id", "suggested_confidence",
                   "classify_state"}


def q(sql, params=()):
    with db.connect() as conn:
        return conn.execute(sql, params).fetchall()


def test_init_on_an_old_database_keeps_every_row():
    rebuild_old_database()
    with db.connect() as conn:
        food = conn.execute("SELECT id FROM categories WHERE name = 'Food & Dining'").fetchone()["id"]
        account = conn.execute("INSERT INTO accounts (name, last4) VALUES ('SBI', '1234') RETURNING id").fetchone()["id"]
        batch = conn.execute("INSERT INTO import_batches (account_id, filename, added) VALUES (%s, 'x.csv', 2) RETURNING id", (account,)).fetchone()["id"]
        conn.execute("INSERT INTO transactions (account_id, amount, direction, txn_date, payee, upi_ref, category_id, source, import_batch_id)"
                     " VALUES (%s, 250.50, 'debit', '2026-10-05', 'Swiggy', '123456789012', %s, 'statement', %s)", (account, food, batch))
        conn.execute("INSERT INTO transactions (amount, direction, txn_date, payee, category_id, source) VALUES (80, 'debit', '2026-10-06', 'Tea', %s, 'manual')", (food,))
        conn.execute("INSERT INTO budgets (category_id, month, limit_amount) VALUES (%s, '2026-10-01', 4000)", (food,))
        conn.execute("INSERT INTO balance_snapshots (account_id, as_of, balance) VALUES (%s, '2026-10-01', 1000)", (account,))
        conn.execute("INSERT INTO phone_devices (name, token_hash) VALUES ('Pixel', 'abc')")
        conn.execute("INSERT INTO sms_seen (text_hash) VALUES ('h1')")
    db.init_db()
    rows = q("SELECT amount, payee, upi_ref, import_batch_id FROM transactions ORDER BY id")
    assert [(r["amount"], r["payee"]) for r in rows] == [(Decimal("250.50"), "Swiggy"), (Decimal("80.00"), "Tea")]
    assert rows[0]["upi_ref"] == "123456789012" and rows[0]["import_batch_id"] == batch
    assert q("SELECT limit_amount FROM budgets")[0]["limit_amount"] == Decimal("4000.00")
    assert q("SELECT balance FROM balance_snapshots")[0]["balance"] == Decimal("1000.00")
    assert q("SELECT COUNT(*) AS n FROM phone_devices")[0]["n"] == 1 and q("SELECT COUNT(*) AS n FROM sms_seen")[0]["n"] == 1
    assert q("SELECT name FROM accounts")[0]["name"] == "SBI"


def test_new_tables_and_columns_exist_and_old_rows_have_no_classification_yet():
    rebuild_old_database()
    with db.connect() as conn:
        conn.execute("INSERT INTO transactions (amount, direction, txn_date, payee, source) VALUES (80, 'debit', '2026-10-06', 'Tea', 'manual')")
    db.init_db()
    tables = {r["tablename"] for r in q("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert NEW_TABLES <= tables
    columns = {r["column_name"] for r in q("SELECT column_name FROM information_schema.columns WHERE table_name = 'transactions'")}
    assert NEW_TXN_COLUMNS <= columns
    row = q("SELECT category_source, category_confidence, classify_state FROM transactions")[0]
    assert row["category_source"] is None and row["category_confidence"] is None and row["classify_state"] is None


def test_running_the_schema_twice_is_safe():
    db.init_db()
    db.init_db()
    assert q("SELECT COUNT(*) AS n FROM trace_spans")[0]["n"] == 0


def test_checks_reject_bad_values():
    import psycopg
    import pytest

    with db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO transactions (amount, direction, txn_date, source, category_source) VALUES (1, 'debit', '2026-10-01', 'manual', 'magic')")
    with db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO transactions (amount, direction, txn_date, source, category_confidence) VALUES (1, 'debit', '2026-10-01', 'manual', 101)")
    with db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO classify_jobs (status) VALUES ('sleeping')")
