"""enforce_sealed: once a person is encrypted, plain text in a sealed column is an error (the database is the real guard).
A laptop user (encrypted = false) is untouched. Also the registry, and the fill_blind default."""

import psycopg
import pytest
from support.tenants import as_user, make_user

from rectifi import db, sealed_fields


def insert_txn(**columns):
    names = ["amount", "direction", "txn_date", "source", *columns]
    values = [10, "debit", "2026-10-05", "sms", *columns.values()]
    with db.connect() as c:
        return c.execute(f"INSERT INTO transactions ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(values))}) RETURNING id", values).fetchone()["id"]


def test_a_laptop_user_may_store_plain_text():
    insert_txn(payee="Swiggy", description="UPI/DR/1", raw_text="full sms", note="n", upi_ref="123456789012", fingerprint="fp")
    with db.connect() as c:
        c.execute("INSERT INTO sms_inbox (raw_text, text_hash) VALUES ('plain', 'plain-hash')")
        c.execute("INSERT INTO alerts (dedupe_key, kind, severity, title, detail, push_title, push_body, numbers) VALUES ('k', 'c', 'info', 'T', 'D', 'p', 'b', '{}'::jsonb)")


@pytest.mark.parametrize("column", ["payee", "description", "raw_text", "note", "category_reason", "upi_ref_sealed"])
def test_an_encrypted_user_cannot_store_plain_text_in_a_sealed_transaction_column(column):
    friend = make_user("friend", encrypted=True)
    with as_user(friend):
        insert_txn(**{column: "rx1.AAAA"})                              # an envelope is accepted
        insert_txn(**{column: "rs1.AAAA"})
        insert_txn(**{column: None})
        with pytest.raises(psycopg.errors.CheckViolation, match="sealed"):
            insert_txn(**{column: "SWIGGY BLR"})


@pytest.mark.parametrize("column", ["upi_ref", "fingerprint"])
def test_an_encrypted_user_must_use_keyed_hashes_for_dedupe_keys(column):
    friend = make_user("friend", encrypted=True)
    with as_user(friend):
        insert_txn(**{column: "b1:abcdef"})
        with pytest.raises(psycopg.errors.CheckViolation, match="keyed hashes"):
            insert_txn(**{column: "123456789012"})


def test_only_the_encrypted_person_is_guarded():
    owner = make_user("owner", uid=1, encrypted=False)
    friend = make_user("friend", encrypted=True)
    with as_user(owner):
        insert_txn(payee="plain is fine for the laptop user")
    with as_user(friend):
        with pytest.raises(psycopg.errors.CheckViolation):
            insert_txn(payee="plain is not fine here")
    with as_user(owner):
        insert_txn(payee="still fine")


def test_update_checks_only_the_columns_that_changed():
    """A row the sweep has not reached yet (still plain) can be touched, for example to change its category."""
    friend = make_user("friend", encrypted=False)
    with as_user(friend):
        tid = insert_txn(payee="plain row", upi_ref="123456789012")
    with db.connect_system("test: flip to encrypted") as c:
        c.execute("UPDATE users SET encrypted = true WHERE id = %s", (friend.uid,))
    with as_user(friend):
        with db.connect() as c:
            c.execute("UPDATE transactions SET category_source = 'user' WHERE id = %s", (tid,))              # no sealed column changed: fine
            c.execute("UPDATE transactions SET payee = %s WHERE id = %s", ("rx1.AAAA", tid))                  # sealing it: fine
        with pytest.raises(psycopg.errors.CheckViolation):
            with db.connect() as c:
                c.execute("UPDATE transactions SET payee = 'plain again' WHERE id = %s", (tid,))


def test_other_tables_in_the_registry_are_guarded_too():
    friend = make_user("friend", encrypted=True)
    with as_user(friend):
        for sql, ok, bad in [
            ("INSERT INTO sms_inbox (raw_text, text_hash, sender) VALUES (%s, 'b1:x', 'rx1.s')", "rx1.A", "plain"),
            ("INSERT INTO phone_devices (name, token_hash) VALUES (%s, 'th1')", "rx1.A", "Pixel"),
            ("INSERT INTO weekly_reviews (week_start, commitment) VALUES ('2026-10-26', %s)", "rx1.A", "no orders"),
            ("INSERT INTO import_batches (filename) VALUES (%s)", "rx1.A", "oct.csv"),
            ("INSERT INTO push_subscriptions (endpoint, p256dh, auth, label) VALUES ('https://e/1', 'p', 'a', %s)", "rx1.A", "my phone"),
            ("INSERT INTO alerts (dedupe_key, kind, severity, title, detail, push_title, push_body, numbers) "
             "VALUES ('k1', 'c', 'info', %s, 'rx1.D', 'p', 'b', '{}'::jsonb)", "rx1.A", "Swiggy over cap"),
        ]:
            with db.connect() as c:
                c.execute(sql, (ok,))
            with pytest.raises(psycopg.errors.CheckViolation):
                with db.connect() as c:
                    c.execute(sql.replace("th1", "th2").replace("e/1", "e/2").replace("'k1'", "'k2'"), (bad,))


def test_a_learned_rule_is_sealed_but_a_shipped_default_stays_plain():
    friend = make_user("friend", encrypted=True)
    with as_user(friend), db.connect() as c:
        food = c.execute("SELECT id FROM categories WHERE name = 'Food & Dining'").fetchone()["id"]
        c.execute("INSERT INTO merchant_rules (pattern, category_id, source) VALUES ('newdefault', %s, 'default')", (food,))
    with as_user(friend):
        with pytest.raises(psycopg.errors.CheckViolation):
            with db.connect() as c:
                c.execute("INSERT INTO merchant_rules (pattern, category_id, source) VALUES ('chaayos', %s, 'learned')", (food,))
        with db.connect() as c:
            c.execute("INSERT INTO merchant_rules (pattern, pattern_blind, category_id, source) VALUES ('rx1.AA', 'b1:aa', %s, 'learned')", (food,))
        with pytest.raises(psycopg.errors.CheckViolation):          # sealed text but an unhashed blind column
            with db.connect() as c:
                c.execute("INSERT INTO merchant_rules (pattern, pattern_blind, category_id, source) VALUES ('rx1.BB', 'bb', %s, 'learned')", (food,))


def test_pattern_blind_is_filled_from_the_pattern_when_left_out_and_decides_uniqueness():
    with db.connect() as c:
        food = c.execute("SELECT id FROM categories WHERE name = 'Food & Dining'").fetchone()["id"]
        c.execute("INSERT INTO merchant_rules (pattern, category_id) VALUES ('mytea', %s)", (food,))
        assert c.execute("SELECT pattern_blind FROM merchant_rules WHERE pattern = 'mytea'").fetchone()["pattern_blind"] == "mytea"
        c.execute("INSERT INTO merchant_rules (pattern, category_id) VALUES ('mytea', %s) ON CONFLICT (user_id, pattern_blind) DO NOTHING", (food,))
        assert c.execute("SELECT count(*) AS n FROM merchant_rules WHERE pattern = 'mytea'").fetchone()["n"] == 1
        # two different sealed texts with the same blind value are the same pattern
        c.execute("INSERT INTO alert_rules (scope, target, period, cap) VALUES ('total', 'all', 'week', 10)")
        assert c.execute("SELECT target_blind FROM alert_rules").fetchone()["target_blind"] == "all"


def test_the_triggers_in_the_database_match_the_registry():
    with db.connect_system("test: triggers") as c:
        rows = {r["relname"]: bytes(r["tgargs"]).decode().split("\0")[:-1] for r in c.execute(
            "SELECT c.relname, t.tgargs FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE t.tgname = 'b_enforce_sealed'").fetchall()}
    expected = {t for t, spec in sealed_fields.SEALED.items() if spec.enforce}
    assert set(rows) == expected
    for table in expected:
        spec = sealed_fields.SEALED[table]
        assert rows[table][:2] == [",".join(spec.sealed), ",".join(spec.blind)]
    assert all(table in db.TENANT_TABLES for table in sealed_fields.SEALED)
    with db.connect_system("test: columns") as c:
        for table, column in sealed_fields.all_sealed() + sealed_fields.all_blind():
            assert c.execute("SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s", (table, column)).fetchone(), (table, column)


def test_a_changed_registry_is_picked_up_on_the_next_init(monkeypatch):
    patched = dict(sealed_fields.SEALED)
    patched["charts"] = sealed_fields.Table(sealed=("title",))
    monkeypatch.setattr(sealed_fields, "SEALED", patched)
    db.init_db()
    friend = make_user("friend", encrypted=True)
    with as_user(friend):
        with pytest.raises(psycopg.errors.CheckViolation):
            with db.connect() as c:
                c.execute("INSERT INTO charts (title, prompt, code, kind, attempts) VALUES ('plain title', 'p', 'c', 'png', 1)")
