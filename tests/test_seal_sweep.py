"""WS-ACCOUNT: the owner sweep seals what laptop mode wrote in plain text, can be stopped and resumed, changes nothing the
second time, keeps old statements dedupe-able, and the sign-in drain moves rs1. text to rx1.

The owner's old rows are written in laptop mode (plain), then multi-user mode is switched on with the owner's keys.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from support.tenants import as_user, make_user, populate

from rectifi import account, db, hooks, importer, merchant_allowlist, sealed_fields, seal_sweep, tools, userctx, vault

OLD: dict = {}
CSV = (Path(__file__).resolve().parent / "fixtures" / "hdfc_style_statement.csv").read_bytes()


def raw(sql, params=(), who=1):
    """Rows exactly as stored: envelopes NOT opened."""
    with as_user(who), db.connect() as conn:
        conn.row_factory = dict_row
        return conn.execute(sql, params).fetchall()


def sealed(v):
    return isinstance(v, str) and v.startswith(("rx1.", "rs1."))


def old_owner_data():
    """What the owner has after months of laptop use: plain text everywhere, in every table the registry names."""
    ids = populate(1)
    tools.add_transaction("120", "debit", "2026-10-06", payee="Chaayos Cafe", description="UPI/DR/55/Chaayos", note="with team",
                          upi_ref="123456789012", raw_text="Rs.120 debited to chaayos")
    tools.add_transaction("300", "debit", "2026-10-07", payee="ZOMATO LTD", description="ZOMATO ORDER", note="late night")
    txn = tools.list_transactions()[0]
    tools.set_category(txn["id"], "Entertainment", remember=True)                     # a learned rule (its pattern is sealed)
    result = importer.import_file(CSV, "hdfc-oct.csv", tools.list_accounts()[0]["id"])
    assert result.added > 0
    with as_user(1), db.connect() as conn:
        conn.execute("UPDATE push_subscriptions SET label = 'Prag phone'")
        conn.execute("INSERT INTO merchant_patterns (pattern, merchant, grp) VALUES ('tea tapri', 'Tea Tapri', 'Other')")
        conn.execute("INSERT INTO alert_rules (scope, target, period, cap) VALUES ('merchant', 'Chaayos', 'week', 400)")
        conn.execute("INSERT INTO alert_rules (scope, target, period, cap) VALUES ('merchant', 'Swiggy', 'week', 900)")
        conn.execute("INSERT INTO alert_rules (scope, target, period, cap) VALUES ('category', 'Food & Dining', 'month', 5000)")
        for key, scope in (("streak:Chaayos:2026-10-05:5", "streak:Chaayos"), ("streak:Swiggy:2026-10-05:5", "streak:Swiggy"),
                           ("cap:merchant:Chaayos:week:2026-10-05:80", "merchant:Chaayos:week")):
            conn.execute("INSERT INTO alerts (dedupe_key, kind, severity, title, detail, push_title, push_body, numbers, scope_key) "
                         "VALUES (%s, 'streak', 'watch', 'Chaayos again', 'five orders', 'pt', 'pb', '{}'::jsonb, %s)", (key, scope))
    return ids, result


@pytest.fixture
def owner(monkeypatch):
    """Old plain data written in laptop mode, then multi-user mode with the owner's fresh keys (not yet encrypted)."""
    ids, result = old_owner_data()
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    ctx = make_user("owner", uid=1)
    OLD.update(ids=ids, result=result)
    return ctx


def snapshot(who=1):
    """Money totals and counts: the sweep must not change a number."""
    with as_user(who), db.connect() as conn:
        txn = conn.execute("SELECT count(*) AS n, COALESCE(sum(amount), 0) AS s FROM transactions").fetchone()
        return {"txns": txn["n"], "sum": txn["s"],
                "budgets": conn.execute("SELECT COALESCE(sum(limit_amount), 0) AS s FROM budgets").fetchone()["s"],
                "balances": conn.execute("SELECT COALESCE(sum(balance), 0) AS s FROM balance_snapshots").fetchone()["s"],
                "caps": conn.execute("SELECT COALESCE(sum(cap), 0) AS s FROM alert_rules").fetchone()["s"]}


def plain_left():
    """Every (table, column) that still holds plain text where the registry says it must be sealed or hashed.
    A cap on a brand, group, category or total (alert_rules) is plain on purpose; a custom merchant's is not."""
    left = set()
    for table, spec in sealed_fields.SEALED.items():
        where = f" WHERE \"{spec.when[0]}\" = '{spec.when[1]}'" if spec.when else ""
        for r in raw(f'SELECT * FROM "{table}"{where}'):
            if table == "alert_rules":
                brand_or_other = r["scope"] != "merchant" or (not sealed(r["target"]) and merchant_allowlist.plain_key(r["target"]))
                custom_plain = r["scope"] == "merchant" and not sealed(r["target"]) and not merchant_allowlist.plain_key(r["target"])
                if custom_plain:
                    left.add((table, "target"))
                if r["scope"] == "merchant" and sealed(r["target"]) and not str(r["target_blind"]).startswith("b1:"):
                    left.add((table, "target_blind"))
                continue
            for col in spec.sealed:
                if r[col] not in (None, "") and not sealed(r[col]):
                    left.add((table, col))
            for col in spec.blind:
                if r[col] not in (None, "") and not str(r[col]).startswith("b1:"):
                    left.add((table, col))
    return sorted(left)


# ───────────────────────── the sweep ─────────────────────────

def test_the_sweep_seals_every_plain_value_and_changes_no_number(owner):
    before = snapshot()
    assert plain_left(), "the fixture has no plain data, so the test proves nothing"
    result = seal_sweep.run(owner)
    assert result["finished"] is True and result["sealed"] > 0
    assert plain_left() == []
    assert snapshot() == before
    state = account.sweep_state(1)
    assert state == ("done", True)                                          # encrypted from now: the trigger takes over
    # and everything still reads back through tools
    got = {t["payee"]: t for t in tools.list_transactions()} if False else None
    with userctx.bound(owner):
        by_payee = {t["payee"]: t for t in tools.list_transactions(limit=200)}
        assert by_payee["Chaayos Cafe"]["note"] == "with team"
        assert by_payee["Chaayos Cafe"]["upi_ref"] == "123456789012"
        assert tools.list_devices()[0]["name"] == "Pixel"
        with db.connect() as conn:
            assert conn.execute("SELECT commitment FROM weekly_reviews").fetchone()["commitment"] == "no more than 3 orders"


def test_the_sweep_fills_merchant_key_for_brands_only_and_before_sealing(owner):
    seal_sweep.run(owner)
    rows = {r["id"]: r["merchant_key"] for r in raw("SELECT id, merchant_key FROM transactions")}
    assert rows[OLD["ids"]["txn"]] == "swiggy"                              # populate() made one 'Swiggy' payment
    with userctx.bound(owner):
        keys = {t["payee"]: k for t in tools.list_transactions(limit=200) for k in [rows[t["id"]]]}
    assert keys["ZOMATO LTD"] == "zomato" and keys["Chaayos Cafe"] is None


def test_second_sweep_changes_nothing(owner):
    seal_sweep.run(owner)
    stored = raw("SELECT * FROM transactions ORDER BY id")
    stored_rules = raw("SELECT * FROM merchant_rules ORDER BY id")
    again = seal_sweep.run(owner)
    assert again["finished"] is True and again["sealed"] == 0
    assert raw("SELECT * FROM transactions ORDER BY id") == stored             # even the nonces are the same: nothing was rewritten
    assert raw("SELECT * FROM merchant_rules ORDER BY id") == stored_rules


def test_a_sweep_stopped_halfway_resumes_and_ends_with_no_plain_value(owner):
    first = seal_sweep.run(owner, batch=2, max_batches=3)
    assert first["finished"] is False and first["sealed"] > 0
    assert plain_left() != []                                                  # genuinely stopped halfway
    state, encrypted = account.sweep_state(1)
    assert encrypted is False and seal_sweep.parse_state(state)["phase"] == "running"
    assert seal_sweep.parse_state(state)["done"] == first["sealed"]
    # the owner can keep working in between: a new row is sealed by the normal write path, not left for the sweep
    with userctx.bound(owner):
        tools.add_transaction("45", "debit", "2026-10-08", payee="Chai Point", note="between")
    rest = seal_sweep.run(owner, batch=2)
    assert rest["finished"] is True
    assert plain_left() == []
    assert account.sweep_state(1) == ("done", True)


def test_a_crash_in_one_batch_leaves_no_half_row_and_the_next_run_finishes(owner, monkeypatch):
    calls = []
    real = seal_sweep.changes

    def flaky(table, spec, row):
        calls.append(table)
        if len(calls) == 7:
            raise RuntimeError("boom")
        return real(table, spec, row)

    monkeypatch.setattr(seal_sweep, "changes", flaky)
    with pytest.raises(RuntimeError):
        seal_sweep.run(owner, batch=3)
    monkeypatch.setattr(seal_sweep, "changes", real)
    assert account.sweep_state(1)[1] is False
    for r in raw("SELECT payee, upi_ref, upi_ref_sealed FROM transactions"):   # a row is either all old or all new
        if sealed(r["payee"]):
            assert r["upi_ref"] is None or r["upi_ref"].startswith("b1:")
    assert seal_sweep.run(owner)["finished"] is True and plain_left() == []


def test_two_sweeps_at_once_do_not_collide(owner):
    lock = seal_sweep._lock_for(1)
    assert lock.acquire(blocking=False)
    try:
        assert seal_sweep.run(owner)["busy"] is True
    finally:
        lock.release()


def test_an_old_statement_imported_again_after_the_sweep_still_finds_its_duplicates(owner):
    account_id = raw("SELECT id FROM accounts ORDER BY id")[0]["id"]
    before = raw("SELECT count(*) AS n FROM transactions")[0]["n"]
    seal_sweep.run(owner)
    with userctx.bound(owner):
        again = importer.import_file(CSV, "hdfc-oct.csv", account_id)
    assert again.added == 0 and again.duplicates + again.merged >= OLD["result"].added
    assert raw("SELECT count(*) AS n FROM transactions")[0]["n"] == before


def test_old_upi_references_still_dedupe_after_the_sweep(owner):
    seal_sweep.run(owner)
    with userctx.bound(owner):
        again = tools.add_transaction("120", "debit", "2026-10-06", payee="Chaayos Cafe", upi_ref="123456789012")
        assert again["status"] == "duplicate"
    assert raw("SELECT count(*) AS n FROM transactions WHERE amount = 120")[0]["n"] == 1


def test_the_sweep_hashes_exactly_as_the_write_path_does(owner):
    seal_sweep.run(owner)
    row = [r for r in raw("SELECT * FROM transactions WHERE payee IS NOT NULL") if r["unlocked"]][0]
    with userctx.bound(owner):
        refs = {r["upi_ref"] for r in raw("SELECT upi_ref FROM transactions WHERE upi_ref IS NOT NULL")}
        assert vault.blind_ingest("123456789012") in refs
        assert vault.blind_ingest(f"ref1") in refs                           # populate()'s reference
        assert row["upi_ref"] is None or row["upi_ref"].startswith("b1:")
        learned = raw("SELECT pattern_blind FROM merchant_rules WHERE source = 'learned'")
        assert learned and all(r["pattern_blind"].startswith("b1:") for r in learned)
        seen = raw("SELECT text_hash FROM sms_seen")[0]["text_hash"]
        assert seen == vault.blind_ingest(f"h1")                             # populate() stored 'h1' as the plain digest


def test_alert_caps_and_keys_seal_custom_merchants_only(owner):
    seal_sweep.run(owner)
    rules = {r["scope"] + ":" + str(r["target"]): r for r in raw("SELECT * FROM alert_rules")}
    assert "merchant:Swiggy" in rules and rules["merchant:Swiggy"]["target_blind"] == "Swiggy"        # a brand stays plain (D3)
    assert "category:Food & Dining" in rules
    custom = [r for r in raw("SELECT * FROM alert_rules WHERE scope = 'merchant'") if r["target"] != "Swiggy"]
    assert len(custom) == 1 and sealed(custom[0]["target"]) and custom[0]["target_blind"].startswith("b1:")
    keys = [(r["dedupe_key"], r["scope_key"]) for r in raw("SELECT dedupe_key, scope_key FROM alerts")]
    assert not any("Chaayos" in k or "Chaayos" in (s or "") for k, s in keys)
    assert any(k.startswith("streak:Swiggy:") and s == "streak:Swiggy" for k, s in keys)               # the brand stays readable


def test_the_old_ledger_text_copies_in_the_library_index_are_dropped(owner):
    assert raw("SELECT count(*) AS n FROM rag_chunks WHERE corpus = 'txn'")[0]["n"] == 1
    seal_sweep.run(owner)
    assert raw("SELECT count(*) AS n FROM rag_chunks WHERE corpus = 'txn'")[0]["n"] == 0
    assert raw("SELECT count(*) AS n FROM rag_chunks WHERE corpus = 'note'")[0]["n"] == 1             # the owner's notes stay


def test_after_the_sweep_the_database_refuses_plain_text(owner):
    seal_sweep.run(owner)
    import psycopg

    with as_user(owner), pytest.raises(psycopg.errors.CheckViolation), db.connect() as conn:
        conn.execute("UPDATE transactions SET payee = 'plain again' WHERE id = %s", (OLD["ids"]["txn"],))


def test_the_sweep_does_nothing_in_laptop_mode_or_signed_out(owner, monkeypatch):
    locked = userctx.UserContext(uid=1, key=None, public_key=owner.public_key, ingest_salt=owner.ingest_salt, strict=False)
    assert "signed out" in seal_sweep.run(locked)["skipped"]
    monkeypatch.delenv("RECTIFI_MULTIUSER")
    assert "laptop" in seal_sweep.run(owner)["skipped"]
    assert plain_left() != []


def test_every_keyed_hash_in_the_registry_has_a_rule():
    assert {k for k in seal_sweep.BLIND_RULES} == set(sealed_fields.all_blind())


# ───────────────────────── the hook ─────────────────────────

def test_the_sign_in_hook_starts_the_sweep_for_the_owner_only_and_not_twice(owner, monkeypatch):
    started = []
    monkeypatch.setattr(userctx, "spawn", lambda target, *a, name, **k: started.append((target.__name__, a, name)))
    friend = make_user("friend", encrypted=True)
    with userctx.bound(friend):
        hooks.account_after_sign_in(friend)
    assert started == []
    with userctx.bound(owner):
        hooks.account_after_sign_in(owner)
    assert [s[0] for s in started] == ["run_in_background"]
    seal_sweep.run(owner)
    started.clear()
    with userctx.bound(owner):
        hooks.account_after_sign_in(owner)
    assert started == []                                                    # done: nothing to resume


def test_the_hook_really_runs_the_sweep_in_a_background_thread(owner):
    with userctx.bound(owner):
        owner_threads = []
        real = userctx.spawn

        def keep(*a, **k):
            thread = real(*a, **k)
            owner_threads.append(thread)
            return thread

        userctx.spawn = keep
        try:
            hooks.account_after_sign_in(owner)
        finally:
            userctx.spawn = real
    for thread in owner_threads:
        thread.join(timeout=60)
    assert account.sweep_state(1) == ("done", True)


def test_a_failing_sweep_thread_logs_the_type_and_not_the_row(owner, monkeypatch, caplog):
    def boom(ctx, **k):
        raise RuntimeError("Failing row contains (Chaayos Cafe)")

    monkeypatch.setattr(seal_sweep, "run", boom)
    seal_sweep.run_in_background(owner)
    assert "RuntimeError" in caplog.text and "Chaayos" not in caplog.text


# ───────────────────────── rs1. -> rx1. ─────────────────────────

def test_the_sign_in_drain_moves_signed_out_alert_text_to_the_data_key_and_leaves_core_tables_to_core():
    friend = make_user("friend", encrypted=True)
    locked = userctx.UserContext(uid=friend.uid, key=None, public_key=friend.public_key, ingest_salt=friend.ingest_salt, strict=False)
    import os

    os.environ["RECTIFI_MULTIUSER"] = "1"
    try:
        with as_user(locked), db.connect() as conn:                        # written while signed out: sealed to the public key
            conn.execute("INSERT INTO alerts (dedupe_key, kind, severity, title, detail, push_title, push_body, numbers) "
                         "VALUES ('k1', 'cap', 'info', %s, %s, 'pt', 'pb', '{}'::jsonb)",
                         (vault.seal("Chai cap", locked), vault.seal("one more chai", locked)))
        assert all(r["title"].startswith("rs1.") for r in raw("SELECT title FROM alerts", who=friend.uid))
        with userctx.bound(friend):
            assert seal_sweep.drain_rs1(friend) == 2
            assert seal_sweep.drain_rs1(friend) == 0                       # second run: nothing left
        row = raw("SELECT title, detail FROM alerts", who=friend.uid)[0]
        assert row["title"].startswith("rx1.") and row["detail"].startswith("rx1.")
        with userctx.bound(friend), db.connect() as conn:
            assert conn.execute("SELECT title, detail FROM alerts").fetchone() == {"title": "Chai cap", "detail": "one more chai"}
        assert "transactions" in seal_sweep.DRAINED_BY_CORE and "sms_inbox" in seal_sweep.DRAINED_BY_CORE
    finally:
        os.environ.pop("RECTIFI_MULTIUSER", None)


def test_old_push_labels_and_taught_merchant_names_are_sealed_and_their_dedupe_hashes_match_the_write_path(owner):
    seal_sweep.run(owner)
    [sub] = raw("SELECT label FROM push_subscriptions")
    assert sealed(sub["label"])
    patterns = raw("SELECT pattern, merchant, pattern_blind FROM merchant_patterns")
    assert len(patterns) == 2 and all(sealed(p["pattern"]) and sealed(p["merchant"]) for p in patterns)
    with userctx.bound(owner):
        assert {p["pattern_blind"] for p in patterns} == {vault.blind("chaayos"), vault.blind("tea tapri")}
        with db.connect() as conn:
            assert {r["merchant"] for r in conn.execute("SELECT merchant FROM merchant_patterns").fetchall()} == {"Chaayos", "Tea Tapri"}
            assert conn.execute("SELECT label FROM push_subscriptions").fetchone()["label"] == "Prag phone"
            titles = {r["title"] for r in conn.execute("SELECT title FROM alerts").fetchall()}
        custom_key = [r["dedupe_key"] for r in raw("SELECT dedupe_key FROM alerts")]
        assert f"streak:{vault.blind_ingest('Chaayos')}:2026-10-05:5" in custom_key       # exactly blind_ingest(name), as WS-ALERTS writes it
    assert titles == {"T", "Chaayos again"}
