"""Row level security and the composite keys: one person cannot read, write or point at another person's rows.

Every test here runs in laptop mode (the default) with RLS on: that is what the whole suite runs under. Each test says
what it proves. The role the tests log in as is the table OWNER, so FORCE ROW LEVEL SECURITY is what makes this real."""

import re
from pathlib import Path

import psycopg
import pytest
from support.tenants import as_user, count, populate, two_users

from rectifi import config, db, sealed_fields

ROOT = Path(__file__).resolve().parent.parent
PEOPLE_TABLES = {"users", "invites", "user_sessions", "allowed_merchants"}      # no user_id column; each has FORCE + a policy


def public_tables():
    with db.connect_system("test: list tables") as c:
        return [r["tablename"] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1").fetchall()]


def test_every_public_table_has_user_id_force_and_a_policy_or_is_on_the_allow_list():
    """Walks pg_tables. A new table that is in none of the lists fails here, so nobody can forget row level security."""
    library = tuple(sealed_fields.LIBRARY_TABLES)           # LangGraph's own tables: owner-only by route gate, no user_id
    with db.connect_system("test: inspect") as c:
        info = {r["relname"]: r for r in c.execute(
            """SELECT c.relname, c.relrowsecurity AS rls, c.relforcerowsecurity AS forced,
                      EXISTS (SELECT 1 FROM pg_policies p WHERE p.schemaname = 'public' AND p.tablename = c.relname) AS has_policy,
                      EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'user_id' AND NOT a.attisdropped) AS has_uid
               FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'""").fetchall()}
    unknown = []
    for name, row in info.items():
        if name in PEOPLE_TABLES:
            assert row["rls"] and row["forced"] and row["has_policy"], f"{name}: needs FORCE row level security and a policy"
        elif name in db.TENANT_TABLES:
            assert row["has_uid"], f"{name}: needs a user_id column"
            assert row["rls"] and row["forced"] and row["has_policy"], f"{name}: needs FORCE row level security and a policy"
        elif name.startswith(library):
            assert not row["has_uid"]
        else:
            unknown.append(name)
    assert not unknown, f"Tables in no list (add user_id + RLS, or allow-list them with a reason): {unknown}"
    assert set(db.TENANT_TABLES) <= set(info), "db.TENANT_TABLES names a table that does not exist"
    assert PEOPLE_TABLES <= set(info)


def test_user_id_is_required_and_defaults_to_the_current_person():
    with db.connect() as c:
        c.execute("INSERT INTO accounts (name) VALUES ('A')")
        assert c.execute("SELECT user_id FROM accounts").fetchone()["user_id"] == 1
    with db.connect() as c:
        cols = {r["table_name"]: (r["is_nullable"], r["column_default"]) for r in c.execute(
            "SELECT table_name, is_nullable, column_default FROM information_schema.columns WHERE table_schema = 'public' AND column_name = 'user_id'").fetchall()}
    assert set(cols) - {"user_sessions"} == set(db.TENANT_TABLES)          # user_sessions has a user_id too, but no default: sessions are made by connect_system
    assert all(nullable == "NO" and "app_uid()" in (default or "") for table, (nullable, default) in cols.items() if table != "user_sessions")


def test_as_b_a_bare_select_star_returns_no_rows_of_a_in_any_table():
    a, b = two_users()
    populate(a)
    populate(b)
    for table in db.TENANT_TABLES:
        assert count(table, a) >= 1, f"{table}: A has no rows, so this test would prove nothing"
        assert count(table, b) >= 1, f"{table}: B has no rows, so this test would prove nothing"
        for who, other in ((a, b), (b, a)):
            with as_user(who), db.connect() as c:
                assert c.execute(f'SELECT count(*) AS n FROM "{table}" WHERE user_id = %s', (other.uid,)).fetchone()["n"] == 0, table
                assert c.execute(f'SELECT count(*) AS n FROM "{table}" WHERE user_id <> %s', (who.uid,)).fetchone()["n"] == 0, table


def test_with_no_person_every_table_shows_zero_rows_and_an_insert_errors():
    a, b = two_users()
    populate(a)
    with db.connect_system("test: look without a person") as c:
        for table in db.TENANT_TABLES:
            if table == "phone_devices":
                continue                      # the one table a system connection may READ (device lookup); tested below
            assert c.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()["n"] == 0, table
    for sql in ("INSERT INTO accounts (name) VALUES ('ghost')", "INSERT INTO app_settings (key, value) VALUES ('k', '1'::jsonb)"):
        with pytest.raises(psycopg.Error):
            with db.connect_system("test: write without a person") as c:
                c.execute(sql)


def test_a_raw_owner_connection_without_the_setting_sees_nothing():
    populate(1)
    with psycopg.connect(config.database_url()) as raw:
        assert raw.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
        assert raw.execute("SELECT count(*) FROM categories").fetchone()[0] == 0
    with psycopg.connect(config.database_url(), options="-c app.user_id=1") as raw:
        assert raw.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1


def test_a_system_connection_may_read_devices_but_not_write_them():
    a, _ = two_users()
    populate(a)
    with db.connect_system("test: device lookup") as c:
        rows = c.execute("SELECT user_id FROM phone_devices WHERE token_hash = %s", ("hash1",)).fetchall()
        assert [r["user_id"] for r in rows] == [1]
    with db.connect_system("test: device write") as c:
        assert c.execute("UPDATE phone_devices SET name = 'x'").rowcount == 0        # RLS hides the rows from an UPDATE


def test_b_cannot_insert_a_row_that_points_at_a_account():
    a, b = two_users()
    ids = populate(a)
    with as_user(b):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with db.connect() as c:
                c.execute("INSERT INTO transactions (account_id, amount, direction, txn_date, source) VALUES (%s, 5, 'debit', '2026-10-01', 'manual')",
                          (ids["account"],))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with db.connect() as c:
                c.execute("INSERT INTO budgets (category_id, month, limit_amount) VALUES (%s, '2026-10-01', 5)", (ids["food"],))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with db.connect() as c:
                c.execute("INSERT INTO alerts (dedupe_key, kind, severity, title, detail, push_title, push_body, numbers, txn_id) "
                          "VALUES ('x', 'cap', 'info', 't', 'd', 'p', 'b', '{}'::jsonb, %s)", (ids["txn"],))
        # B's own rows can still be moved to B's own parents, and not to A's
        populate(b)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with db.connect() as c:
                c.execute("UPDATE transactions SET account_id = %s", (ids["account"],))
    assert count("transactions", a) == 1


def test_b_cannot_insert_or_update_a_row_for_a():
    a, b = two_users()
    with as_user(b):
        with pytest.raises(psycopg.Error):
            with db.connect() as c:
                c.execute("INSERT INTO accounts (user_id, name) VALUES (%s, 'planted')", (a.uid,))
        with db.connect() as c:
            c.execute("INSERT INTO accounts (name) VALUES ('mine')")
        with pytest.raises(psycopg.Error):
            with db.connect() as c:
                c.execute("UPDATE accounts SET user_id = %s", (a.uid,))
    assert count("accounts", a) == 0


def test_two_people_can_share_names_references_and_patterns_but_one_person_cannot_repeat_them():
    a, b = two_users()
    populate(a)
    populate(b)       # same account name, same categories, same merchant patterns
    insert = "INSERT INTO transactions (amount, direction, txn_date, source, upi_ref) VALUES (10, 'debit', '2026-10-02', 'sms', '123456789012')"
    for who in (a, b):
        with as_user(who), db.connect() as c:             # the same UPI reference for both people: allowed
            c.execute(insert)
    for who in (a, b):
        with as_user(who):
            with pytest.raises(psycopg.errors.UniqueViolation):
                with db.connect() as c:
                    c.execute(insert)
            with pytest.raises(psycopg.errors.UniqueViolation):
                with db.connect() as c:
                    c.execute("INSERT INTO accounts (name) VALUES ('SBI')")
    assert count("transactions", a) == 2 and count("transactions", b) == 2


def test_the_setting_is_empty_again_after_commit_and_after_rollback():
    def setting():
        with db.get_pool().connection() as raw:
            return raw.execute("SELECT current_setting('app.user_id', true) AS v").fetchone()["v"]

    with db.connect() as c:
        assert c.execute("SELECT current_setting('app.user_id') AS v").fetchone()["v"] == "1"
    assert setting() in ("", None)
    with pytest.raises(RuntimeError):
        with db.connect() as c:
            c.execute("INSERT INTO accounts (name) VALUES ('x')")
            raise RuntimeError("roll back")
    assert setting() in ("", None)
    with db.connect() as c:
        assert c.execute("SELECT count(*) AS n FROM accounts").fetchone()["n"] == 0


def test_set_null_clears_only_the_foreign_column_and_keeps_user_id():
    a, b = two_users()
    ids = populate(a)
    populate(b)
    with as_user(a), db.connect() as c:
        c.execute("DELETE FROM accounts WHERE id = %s", (ids["account"],))
        row = c.execute("SELECT user_id, account_id FROM transactions WHERE id = %s", (ids["txn"],)).fetchone()
        assert row == {"user_id": a.uid, "account_id": None}
        assert c.execute("SELECT count(*) AS n FROM balance_snapshots").fetchone()["n"] == 0        # CASCADE
        assert c.execute("SELECT account_id FROM import_batches WHERE id = %s", (ids["batch"],)).fetchone()["account_id"] is None
    assert count("transactions", b) == 1 and count("balance_snapshots", b) == 1


def test_deleting_a_transaction_cascades_its_chunks_and_nulls_its_alert_under_rls():
    a, _ = two_users()
    ids = populate(a)
    with as_user(a), db.connect() as c:
        c.execute("DELETE FROM transactions WHERE id = %s", (ids["txn"],))
        assert c.execute("SELECT count(*) AS n FROM rag_chunks WHERE corpus = 'txn'").fetchone()["n"] == 0
        assert c.execute("SELECT txn_id FROM alerts WHERE id = %s", (ids["alert"],)).fetchone()["txn_id"] is None


def test_deleting_a_person_removes_every_row_in_every_table_and_nothing_else():
    a, b = two_users()
    populate(a)
    populate(b)
    before = {t: count(t, a) for t in db.TENANT_TABLES}
    with db.connect_system("test: delete a person") as c:
        assert c.execute("DELETE FROM users WHERE id = %s", (b.uid,)).rowcount == 1
    for table in db.TENANT_TABLES:
        assert count(table, b) == 0, f"{table}: rows of a deleted person are left"
        assert count(table, a) == before[table], f"{table}: someone else's rows changed"


def test_people_tables_are_closed_to_a_normal_connection():
    a, b = two_users()
    with db.connect_system("test: make an invite and a session") as c:
        c.execute("INSERT INTO invites (code_hash, kind, expires_at) VALUES ('h', 'friend', now() + interval '1 day')")
        c.execute("INSERT INTO user_sessions (id, user_id, expires_at) VALUES (gen_random_uuid(), %s, now() + interval '1 day')", (a.uid,))
    with as_user(a), db.connect() as c:
        assert c.execute("SELECT count(*) AS n FROM invites").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM user_sessions").fetchone()["n"] == 0
        assert [r["id"] for r in c.execute("SELECT id FROM users").fetchall()] == [a.uid]          # only their own row
    with as_user(a):
        for sql in ("UPDATE users SET session_epoch = 99", "DELETE FROM users", "INSERT INTO users (username) VALUES ('sneaky')"):
            try:
                with db.connect() as c:
                    c.execute(sql)
            except psycopg.Error:
                pass
        with db.connect_system("test: check nothing changed") as c:
            assert c.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 2
            assert c.execute("SELECT max(session_epoch) AS m FROM users").fetchone()["m"] == 0


def test_a_second_init_changes_no_policy_trigger_or_constraint():
    def shape():
        with db.connect_system("test: shape") as c:
            return [tuple(r.values()) for r in c.execute(
                """SELECT 'policy', tablename || '.' || policyname || ' ' || coalesce(qual, '') FROM pg_policies WHERE schemaname = 'public'
                   UNION ALL SELECT 'constraint', conrelid::regclass::text || '.' || conname || ' ' || pg_get_constraintdef(oid) FROM pg_constraint
                     WHERE connamespace = 'public'::regnamespace
                   UNION ALL SELECT 'trigger', tgrelid::regclass::text || '.' || tgname || ' ' || encode(tgargs, 'escape') FROM pg_trigger WHERE NOT tgisinternal
                   UNION ALL SELECT 'index', indexname || ' ' || indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1, 2""").fetchall()]

    first = shape()
    db.init_db()
    assert shape() == first and len(first) > 100


def test_connect_system_is_only_called_from_the_allow_listed_modules():
    """Callers: db.py (init_db), users.py (sign-up and login, WS-AUTH), tools.py (authenticate_device, WS-CORE) and alerts.py (tick)."""
    allowed = {"rectifi/db.py", "rectifi/users.py", "rectifi/tools.py", "rectifi/alerts.py", "rectifi/account.py"}  # account: sweep state, delete
    found = set()
    for path in (ROOT / "rectifi").rglob("*.py"):
        if re.search(r"\bconnect_system\(", path.read_text(encoding="utf-8")):
            found.add(path.relative_to(ROOT).as_posix())
    assert found <= allowed, f"connect_system called from {sorted(found - allowed)}"
    with pytest.raises(ValueError):
        with db.connect_system(""):
            pass
