"""Review round 1, HIGH: plain dedupe keys written in laptop mode and keyed (`b1:`) keys written after the owner's claim must
never double-insert, and the owner sweep must always finish (CLAUDE.md rule 3: the database is the guard).

The claim here is the real one: an owner invite, `users.signup`, `users.confirm_signup`.
"""

from pathlib import Path

import pytest
from psycopg.rows import dict_row
from support.tenants import as_user, make_user

from rectifi import account, db, importer, seal_sweep, tools, userctx, users

CSV = (Path(__file__).resolve().parent / "fixtures" / "hdfc_style_statement.csv").read_bytes()
SMS_WITH_REF = "Rs.120.00 debited from A/c XX1234 on 06-10-26 to VPA zomato@ybl UPI Ref 512345678901"
SMS_NO_REF = "Rs.40.00 debited from A/c XX1234 on 20-10-26 to chai point"
STAMP = 1761900000000
PASSWORD = "correct horse battery staple"


def n_txns(ctx=None):
    with db.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM transactions").fetchone()["n"]


def raw(sql, params=(), who=1):
    with as_user(who), db.connect() as conn:
        conn.row_factory = dict_row
        return conn.execute(sql, params).fetchall()


def laptop_history():
    """What the owner did on the laptop: a statement, one SMS with a UPI reference, one without, a hand entry with a reference."""
    from datetime import datetime, timezone

    tools.add_account("HDFC", "1234")
    account_id = tools.list_accounts()[0]["id"]
    first = importer.import_file(CSV, "oct.csv", account_id)
    assert first.added > 0
    when = datetime.fromtimestamp(STAMP / 1000, tz=timezone.utc)
    assert tools.ingest_sms(None, "AX-HDFCBK", SMS_WITH_REF, when)["status"] == "added"
    assert tools.ingest_sms(None, "AX-HDFCBK", SMS_NO_REF, when)["status"] == "added"
    assert tools.add_transaction("75", "debit", "2026-10-08", payee="Tea", upi_ref="123456789012")["status"] == "added"
    return account_id, first, when


def claim_owner(monkeypatch):
    """The owner's real claim: invite, step one, step two (which re-keys the dedupe columns)."""
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "x" * 40)
    users.reset_limiters()
    code = users.create_invite("owner")["code"]
    token, _recovery = users.signup(code, "prag", PASSWORD)
    return users.confirm_signup(token)


def test_reimport_and_resent_sms_after_the_claim_add_nothing_and_the_sweep_finishes(monkeypatch):
    account_id, first, when = laptop_history()
    before = n_txns()
    signed_in = claim_owner(monkeypatch)
    with userctx.bound(signed_in.ctx):
        assert n_txns() == before
        again = importer.import_file(CSV, "oct.csv", account_id)                     # the same statement, after the claim
        assert again.added == 0, "an old plain fingerprint must still be found"
        assert tools.ingest_sms(None, "AX-HDFCBK", SMS_WITH_REF, when)["status"] == "duplicate"      # has a UPI reference
        assert tools.ingest_sms(None, "AX-HDFCBK", SMS_NO_REF, when)["status"] == "duplicate"        # no reference: sms_seen
        assert tools.add_transaction("75", "debit", "2026-10-08", payee="Tea", upi_ref="123456789012")["status"] == "duplicate"
        assert n_txns() == before
        result = seal_sweep.run(signed_in.ctx)
        assert result["finished"] is True and result["merged"] == 0
    assert account.sweep_state(1)[1] is True                                         # users.encrypted
    with userctx.bound(signed_in.ctx):
        assert n_txns() == before                                                     # still one row per payment


def test_without_the_claim_step_the_old_dedupe_misses(monkeypatch):
    """The regression: with the up-front re-key disabled the same statement is added twice (this is what the review found)."""
    account_id, first, when = laptop_history()
    monkeypatch.setattr(users, "_rekey_owner_dedupe", lambda *a, **k: None)
    signed_in = claim_owner(monkeypatch)
    with userctx.bound(signed_in.ctx):
        again = importer.import_file(CSV, "oct.csv", account_id)
        assert again.added > 0                                                        # the double insert, now visible
        assert tools.ingest_sms(None, "AX-HDFCBK", SMS_NO_REF, when)["status"] == "added"


def test_the_sweep_survives_a_clash_when_the_rekey_never_ran(monkeypatch):
    """A write that landed under the keyed key before the old row was re-keyed: the sweep drops the newer duplicate, counts it,
    and still finishes. Money is not doubled and nothing raises."""
    account_id, first, when = laptop_history()
    monkeypatch.setattr(users, "_rekey_owner_dedupe", lambda *a, **k: None)
    signed_in = claim_owner(monkeypatch)
    with userctx.bound(signed_in.ctx):
        before = n_txns()
        again = importer.import_file(CSV, "oct.csv", account_id)
        assert again.added > 0
        assert tools.add_transaction("75", "debit", "2026-10-08", payee="Tea", upi_ref="123456789012")["status"] == "added"
        assert tools.ingest_sms(None, "AX-HDFCBK", SMS_WITH_REF, when)["status"] == "added"
        doubled = n_txns()
        assert doubled > before
        result = seal_sweep.run(signed_in.ctx)
        assert result["finished"] is True
        assert result["merged"] >= 3
        assert n_txns() == before                                                     # one row per payment again
        assert tools.add_transaction("75", "debit", "2026-10-08", payee="Tea", upi_ref="123456789012")["status"] == "duplicate"
    assert account.sweep_state(1) == ("done", True)


def test_a_clash_keeps_the_category_the_person_chose_on_the_newer_copy(monkeypatch):
    laptop_history()
    monkeypatch.setattr(users, "_rekey_owner_dedupe", lambda *a, **k: None)
    signed_in = claim_owner(monkeypatch)
    with userctx.bound(signed_in.ctx):
        dup = tools.add_transaction("75", "debit", "2026-10-08", payee="Tea", upi_ref="123456789012", category="Entertainment")
        assert dup["status"] == "added"
        seal_sweep.run(signed_in.ctx)
        rows = [t for t in tools.list_transactions(limit=500) if t["payee"] == "Tea"]
        assert len(rows) == 1 and rows[0]["category_source"] == "user" and rows[0]["category"] == "Entertainment"


def test_a_rule_taught_after_the_claim_wins_over_the_old_plain_rule(monkeypatch):
    laptop_history()
    with as_user(1):
        txn = next(t for t in tools.list_transactions(limit=500) if t["payee"] == "Tea")
        tools.set_category(txn["id"], "Entertainment", remember=True)                # learned rule, plain pattern
    monkeypatch.setattr(users, "_rekey_owner_dedupe", lambda *a, **k: None)
    signed_in = claim_owner(monkeypatch)
    with userctx.bound(signed_in.ctx):
        tea = next(t for t in tools.list_transactions(limit=500) if t["payee"] == "Tea")
        tools.set_category(tea["id"], "Food & Dining", remember=True)               # same pattern, new keyed row (or an update)
        result = seal_sweep.run(signed_in.ctx)
        assert result["finished"] is True
    assert account.sweep_state(1)[1] is True


def test_the_phone_gets_no_keys_until_the_claim_is_confirmed(monkeypatch):
    """Between the owner's step one and step two the gateway must not write anything keyed: the old rows are re-keyed at step two."""
    laptop_history()
    token = tools.create_device("Prag phone")["token"]
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "x" * 40)
    users.reset_limiters()
    pending_token, _ = users.signup(users.create_invite("owner")["code"], "prag", PASSWORD)
    ctx = tools.authenticate_device(token)["ctx"]
    assert ctx.ingest_salt is None and ctx.public_key is None
    with userctx.bound(ctx), pytest.raises(Exception) as err:
        tools.ingest_sms(None, "AX-HDFCBK", SMS_NO_REF)
    assert "VaultLocked" in type(err.value).__name__
    users.confirm_signup(pending_token)
    assert tools.authenticate_device(token)["ctx"].ingest_salt is not None


def test_laptop_mode_is_untouched():
    account_id, first, when = laptop_history()
    assert importer.import_file(CSV, "oct.csv", account_id).added == 0
    assert tools.ingest_sms(None, "AX-HDFCBK", SMS_NO_REF, when)["status"] == "duplicate"
