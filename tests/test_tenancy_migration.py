"""The upgrade from one person to per-person rows (multi-user plan, decision D10).

An old database is built from the real previous schema (tests/fixtures/schema_before_tenancy.sql) with a row in every table.
These tests only ever touch the throwaway test database. The guard (only *_test and *_demo upgrade by themselves) is tested
by pretending the name is something else: the name check is monkeypatched, and the run is on the test database."""

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from support.olddb import PRE_TENANCY_ROWS, catalog_and_rows, rebuild_pre_tenancy_database

from rectifi import config, db

ROOT = Path(__file__).resolve().parent.parent
MONEY = [("transactions", "amount"), ("budgets", "limit_amount"), ("balance_snapshots", "balance"), ("alert_rules", "cap")]


def raw(uid=1):
    return psycopg.connect(config.database_url(), options=f"-c app.user_id={uid}")


def counts_and_sums():
    with raw() as c:
        counts = {t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in db.TENANT_TABLES}
        sums = {f"{t}.{col}": c.execute(f'SELECT sum("{col}") FROM "{t}"').fetchone()[0] for t, col in MONEY}
    return counts, sums


def test_the_old_database_has_a_row_in_every_table_and_no_tenancy():
    rebuild_pre_tenancy_database()
    with raw() as c:
        for table in db.TENANT_TABLES:
            assert c.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] >= 1, f"{table} has no row in the old fixture"
            assert c.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = %s AND column_name = 'user_id'", (table,)).fetchone()[0] == 0
        assert c.execute("SELECT to_regclass('public.users')").fetchone()[0] is None
    assert len(PRE_TENANCY_ROWS) > 20


def test_an_old_database_upgrades_keeps_every_row_and_gives_them_all_to_user_1():
    rebuild_pre_tenancy_database()
    old_counts, old_sums = counts_and_sums()
    assert old_sums["transactions.amount"] == Decimal("1450.50") and old_sums["alert_rules.cap"] == Decimal("1500.00")
    db.init_db()
    new_counts, new_sums = counts_and_sums()               # read as user 1, with row level security on
    # the one thing that grows: init_db tops up the default categories and rules; every row that was there is still there
    for table in db.TENANT_TABLES:
        assert new_counts[table] >= old_counts[table], table
    for table in ("accounts", "transactions", "budgets", "balance_snapshots", "weekly_reviews", "phone_devices", "sms_inbox", "sms_seen",
                  "import_batches", "classify_jobs", "charts", "rag_documents", "rag_chunks", "conversations", "trace_spans",
                  "merchant_patterns", "alert_rules", "alerts", "push_subscriptions"):
        assert new_counts[table] == old_counts[table], table
    assert new_sums == old_sums, "every money total is the same"
    with raw() as c:
        for table in db.TENANT_TABLES:
            assert c.execute(f'SELECT count(*) FROM "{table}" WHERE user_id = 1').fetchone()[0] == c.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        assert c.execute("SELECT key FROM app_settings WHERE key IN ('tenancy_v1', 'categories_v2', 'session_epoch') ORDER BY 1").fetchall() == [
            ("categories_v2",), ("session_epoch",), ("tenancy_v1",)]
        assert c.execute("SELECT value FROM app_settings WHERE key = 'session_epoch'").fetchone()[0] == 3        # the sign-in epoch moved over untouched
        # links survive: the alert still points at its rule and transaction, the transaction at its account, batch and category
        link = c.execute("""SELECT a.rule_id IS NOT NULL, a.txn_id IS NOT NULL, t.account_id IS NOT NULL, t.import_batch_id IS NOT NULL, t.category_id IS NOT NULL
                            FROM alerts a JOIN transactions t ON t.id = a.txn_id""").fetchone()
        assert link == (True, True, True, True, True)
    with db.connect_system("test: owner row") as c:
        row = c.execute("SELECT id, username, encrypted FROM users").fetchall()
        assert row == [{"id": 1, "username": "owner", "encrypted": False}]


def test_dedupe_and_unique_keys_still_work_after_the_upgrade():
    rebuild_pre_tenancy_database()
    db.init_db()
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.connect() as c:
            c.execute("INSERT INTO transactions (amount, direction, txn_date, source, upi_ref) VALUES (1, 'debit', '2026-10-05', 'sms', '123456789012')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.connect() as c:
            c.execute("INSERT INTO sms_inbox (raw_text, text_hash) VALUES ('x', 'texthash1')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.connect() as c:
            c.execute("INSERT INTO accounts (name) VALUES ('SBI Savings')")
    with db.connect() as c:                                # ON CONFLICT with the new targets works on migrated tables
        c.execute("INSERT INTO app_settings (key, value) VALUES ('session_epoch', '4'::jsonb) ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value")
        assert c.execute("SELECT value FROM app_settings WHERE key = 'session_epoch'").fetchone()["value"] == 4


def test_the_old_one_person_keys_are_gone():
    rebuild_pre_tenancy_database()
    db.init_db()
    with db.connect_system("test: inspect") as c:
        bad = c.execute(
            """SELECT cl.relname || '.' || i.indexrelid::regclass::text AS name FROM pg_index i JOIN pg_class cl ON cl.oid = i.indrelid
               WHERE cl.relnamespace = 'public'::regnamespace AND cl.relname = ANY(%s) AND i.indisunique AND NOT i.indisprimary
                 AND NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = i.indrelid AND a.attname = 'user_id' AND a.attnum = ANY(i.indkey))
                 AND i.indexrelid::regclass::text <> 'phone_devices_token_hash_key'""", (list(db.TENANT_TABLES),)).fetchall()
        assert bad == []
        singles = c.execute(
            """SELECT conrelid::regclass::text || '.' || conname AS name FROM pg_constraint c JOIN pg_class ref ON ref.oid = c.confrelid
               WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace AND ref.relname = ANY(%s) AND array_length(c.conkey, 1) = 1""",
            (list(db.TENANT_TABLES),)).fetchall()
        assert singles == []
        pks = c.execute("SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype = 'p' AND conrelid::regclass::text IN "
                        "('sms_seen', 'weekly_reviews', 'app_settings')").fetchall()
        assert all("user_id" in row["pg_get_constraintdef"] for row in pks) and len(pks) == 3


def test_a_second_run_changes_nothing():
    rebuild_pre_tenancy_database()
    db.init_db()
    first = catalog_and_rows(uid=1)
    db.init_db()
    db.init_db()
    assert catalog_and_rows(uid=1) == first


def test_a_new_database_and_an_upgraded_one_end_in_the_same_shape():
    """Both take the same path through schema.sql Part B, so nothing can differ. This test catches a change that makes them drift."""
    rebuild_pre_tenancy_database(with_rows=False)
    db.init_db()
    upgraded = catalog_and_rows(uid=1)
    db.close_pool()
    with psycopg.connect(config.database_url(), autocommit=True) as c:
        tables = [r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]
        c.execute("DROP TABLE IF EXISTS " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")
    db.init_db()
    fresh = catalog_and_rows(uid=1)
    for part in ("tables", "constraints", "indexes", "policies", "triggers", "functions", "rls"):
        assert upgraded[part] == fresh[part], part
    columns = lambda shot: sorted(shot["columns"])          # column ORDER may differ (added later); the columns may not
    assert columns(upgraded) == columns(fresh)


def test_a_forced_failure_leaves_the_old_schema_and_data_intact(monkeypatch):
    rebuild_pre_tenancy_database()
    before = catalog_and_rows()

    def boom(conn):
        raise RuntimeError("simulated failure after the whole upgrade ran")

    monkeypatch.setattr(db, "_seed_allowed_merchants", boom)
    with pytest.raises(RuntimeError, match="simulated"):
        db.init_db()
    assert catalog_and_rows() == before


def test_a_difference_in_the_recount_undoes_everything(monkeypatch):
    rebuild_pre_tenancy_database()
    before = catalog_and_rows()
    real = db._snapshot
    calls = []

    def lying_snapshot(conn, tables):
        shot = real(conn, tables)
        calls.append(1)
        if len(calls) == 2:                                 # the recount after the upgrade
            shot["transactions"] += 1
        return shot

    monkeypatch.setattr(db, "_snapshot", lying_snapshot)
    with pytest.raises(RuntimeError, match="stopped and undone"):
        db.init_db()
    assert len(calls) == 2
    assert catalog_and_rows() == before


# ── the guard (D10) ──

def test_an_old_database_with_any_other_name_refuses_and_is_byte_for_byte_unchanged(monkeypatch):
    rebuild_pre_tenancy_database()
    before = catalog_and_rows()
    monkeypatch.setattr(db, "_may_auto_migrate", lambda: False)         # pretend it is called "rectifi"
    with pytest.raises(db.TenancyMigrationNeeded) as caught:
        db.init_db()
    message = str(caught.value)
    assert "migrate-tenancy" in message and "Nothing has been changed" in message and "Back it up" in message
    assert catalog_and_rows() == before
    with pytest.raises(db.TenancyMigrationNeeded):                      # and again: still refuses, still unchanged
        db.init_db()
    assert catalog_and_rows() == before


def test_migrate_true_is_the_only_way_through_the_guard(monkeypatch):
    rebuild_pre_tenancy_database()
    monkeypatch.setattr(db, "_may_auto_migrate", lambda: False)
    with pytest.raises(db.TenancyMigrationNeeded):
        db.init_db()
    db.init_db(migrate=True)
    counts, _ = counts_and_sums()
    assert counts["transactions"] == 2
    db.init_db()                                                        # now upgraded: starts normally with the guard still on


def test_only_test_and_demo_names_upgrade_by_themselves(monkeypatch):
    names = {"rectifi_test": True, "rectifi_demo": True, "rectifi_foundation_test": True, "rectifi": False, "rectifi_prod": False,
             "rectifi_test_backup": False, "test": False, "mydemo": False}
    for name, allowed in names.items():
        monkeypatch.setenv("DATABASE_URL", f"postgresql://u:p@localhost:5432/{name}")
        assert db._may_auto_migrate() is allowed, name
    monkeypatch.setenv("DATABASE_URL", "user=u password=p dbname=rectifi host=localhost")
    assert db._may_auto_migrate() is False


def test_a_new_empty_database_never_needs_the_guard(monkeypatch):
    monkeypatch.setattr(db, "_may_auto_migrate", lambda: False)
    db.init_db()                                                        # the fresh_db fixture's database is new and empty: fine
    db.init_db()


# ── the command ──

def load_users_devtool():
    spec = importlib.util.spec_from_file_location("rectifi_devtool_users_test", ROOT / "scripts" / "devtools" / "users.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_migrate_tenancy_refuses_without_a_terminal(capsys):
    rebuild_pre_tenancy_database()
    before = catalog_and_rows()
    users = load_users_devtool()
    with pytest.raises(SystemExit) as caught:
        users.migrate_tenancy([])                                       # pytest has no terminal on stdin
    assert "terminal" in str(caught.value) and "Nothing was changed" in str(caught.value)
    assert catalog_and_rows() == before


class FakeTerminal:
    def isatty(self):
        return True


def test_migrate_tenancy_changes_nothing_unless_yes_is_typed(monkeypatch, capsys):
    rebuild_pre_tenancy_database()
    before = catalog_and_rows()
    users = load_users_devtool()
    monkeypatch.setattr(sys, "stdin", FakeTerminal())
    for answer in ("", "y", "YES", "no", "yes please", " "):
        monkeypatch.setattr("builtins.input", lambda prompt="", a=answer: a)
        users.migrate_tenancy([])
        assert catalog_and_rows() == before, f"answer {answer!r} must not upgrade"
    out = capsys.readouterr().out
    assert "BACK UP FIRST" in out and "Not upgraded" in out
    assert "transactions" in out and "2 rows" in out                    # it shows what it will change


def test_migrate_tenancy_with_a_typed_yes_upgrades_the_database(monkeypatch, capsys):
    rebuild_pre_tenancy_database()
    users = load_users_devtool()
    monkeypatch.setattr(sys, "stdin", FakeTerminal())
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    users.migrate_tenancy([])                                           # runs the real upgrade in a child process
    assert "Upgraded" in capsys.readouterr().out
    counts, sums = counts_and_sums()
    assert counts["transactions"] == 2 and sums["transactions.amount"] == Decimal("1450.50")
    users.migrate_tenancy([])                                           # asked again on an upgraded database: nothing to do (no prompt)
    assert "Nothing to do" in capsys.readouterr().out


def test_migrate_tenancy_takes_no_flags_such_as_a_yes_flag():
    users = load_users_devtool()
    with pytest.raises(SystemExit):
        users.migrate_tenancy(["--yes"])


def test_the_invite_commands_say_they_are_not_built_and_change_nothing():
    users = load_users_devtool()
    assert set(users.COMMANDS) == {"migrate-tenancy", "invite", "invites", "revoke-invite"}
    for name in ("invite", "invites", "revoke-invite"):
        with pytest.raises(SystemExit, match="not built yet"):
            users.COMMANDS[name][0]([])


def test_dev_py_lists_the_users_commands():
    spec = importlib.util.spec_from_file_location("rectifi_dev_script", ROOT / "scripts" / "dev.py")
    dev = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dev
    spec.loader.exec_module(dev)
    assert "users" in dev.DEVTOOL_MODULES
    assert {"migrate-tenancy", "invite", "invites", "revoke-invite"} <= set(dev.devtool_commands())
