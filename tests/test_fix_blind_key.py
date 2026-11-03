"""Review round 1, MED: the ingest hashes (UPI references, SMS digests) are keyed with a server secret that is not in the database.

RECTIFI_BLIND_KEY is mixed with each person's ingest salt. If it is lost or changed, each person's hashes are made again at their next
sign-in from their own decrypted data, dedupe keeps working and nothing is lost.
"""

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from rectifi import account, config, db, hooks, importer, seal_sweep, sms_gateway, tools, userctx, users, vault
from rectifi.api import auth
from rectifi.api.app import create_app
from rectifi.api.security import install_session_lock

CSV = (Path(__file__).resolve().parent / "fixtures" / "hdfc_style_statement.csv").read_bytes()
SMS_WITH_REF = "Rs.120.00 debited from A/c XX1234 on 06-10-26 to VPA zomato@ybl UPI Ref 512345678901"
SMS_NO_REF = "Rs.40.00 debited from A/c XX1234 on 20-10-26 to chai point"
SMS_REVIEW = "Rs.500.00 transferred from A/c XX1234 to your wallet"
PASSWORD = "correct horse battery staple"
OLD_KEY = "old-blind-key-0123456789abcdefghijklmnop"
NEW_KEY = "new-blind-key-zyxwvutsrqponmlkjihgfedcba0123"
WHEN = datetime.fromtimestamp(1761900000, tz=timezone.utc)


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("RECTIFI_BLIND_KEY", OLD_KEY)
    monkeypatch.setattr(hooks, "after_sign_in", lambda ctx: None)       # the tests call the pieces they mean
    users.reset_limiters()


def friend(name="zed"):
    token, _ = users.signup(users.create_invite("friend")["code"], name, PASSWORD)
    return users.confirm_signup(token)


def n_txns():
    with db.connect() as conn:
        return conn.execute("SELECT count(*) AS n, COALESCE(sum(amount), 0) AS s FROM transactions").fetchone()


def history():
    tools.add_account("HDFC", "1234")
    account_id = tools.list_accounts()[0]["id"]
    assert importer.import_file(CSV, "oct.csv", account_id).added > 0
    assert tools.ingest_sms(None, "AX-HDFCBK", SMS_WITH_REF, WHEN)["status"] == "added"
    assert tools.ingest_sms(None, "AX-HDFCBK", SMS_NO_REF, WHEN)["status"] == "added"
    assert tools.add_transaction("75", "debit", "2026-10-08", payee="Tea", upi_ref="123456789012")["status"] == "added"
    assert tools.ingest_sms(None, "AX-HDFCBK", SMS_REVIEW, None)["status"] == "needs_review"      # the inbox keeps a hash too
    return account_id


def signed_out(ctx):
    return userctx.UserContext(uid=ctx.uid, key=None, public_key=ctx.public_key, ingest_salt=ctx.ingest_salt, strict=False)


# ───────────────────────── the key is in the hash ─────────────────────────

def test_the_ingest_hash_depends_on_the_server_key_and_on_the_persons_salt(monkeypatch):
    who = friend()
    first = vault.blind_ingest("512345678901", who.ctx)
    monkeypatch.setenv("RECTIFI_BLIND_KEY", NEW_KEY)
    assert vault.blind_ingest("512345678901", who.ctx) != first               # another key, another hash
    assert vault.blind_ingest("512345678901", who.ctx) == vault.blind_ingest("512345678901", signed_out(who.ctx))   # still works signed out


def test_a_dump_holder_without_the_key_cannot_recompute_the_hash():
    """What the review showed was possible: HMAC under a key derived from the salt alone. Now the salt alone is not enough."""
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    who = friend()
    real = vault.blind_ingest("512345678901", who.ctx)
    from_salt_only = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"rectifi|ingest|%d" % who.ctx.uid).derive(who.ctx.ingest_salt)
    guess = "b1:" + hmac.new(from_salt_only, b"512345678901", hashlib.sha256).hexdigest()
    assert guess != real


def test_the_key_itself_is_never_stored():
    who = friend()
    with db.connect_system("tests: read the fingerprint") as conn:
        row = conn.execute("SELECT * FROM users WHERE id = %s", (who.uid,)).fetchone()
    stored = repr({k: v for k, v in row.items()})
    assert OLD_KEY not in stored
    assert row["blind_fp"] == vault.blind_key_fingerprint() and len(row["blind_fp"]) == 16


# ───────────────────────── lost or rotated key ─────────────────────────

def test_a_changed_key_is_remade_at_sign_in_dedupe_still_works_and_nothing_is_lost(monkeypatch):
    who = friend()
    with userctx.bound(who.ctx):
        account_id = history()
        before = n_txns()
    monkeypatch.setenv("RECTIFI_BLIND_KEY", NEW_KEY)                              # the operator lost the old key and made a new one
    assert seal_sweep.blind_key_changed(who.uid) is True
    with userctx.bound(who.ctx):
        result = seal_sweep.reblind(who.ctx)
        assert result["rebuilt"] >= 3 and result["merged"] == 0
        assert n_txns() == before                                                  # nothing lost
        assert importer.import_file(CSV, "oct.csv", account_id).added == 0          # fingerprints are unaffected
        assert tools.ingest_sms(None, "AX-HDFCBK", SMS_WITH_REF, WHEN)["status"] == "duplicate"       # UPI reference, new key
        assert tools.ingest_sms(None, "AX-HDFCBK", SMS_NO_REF, WHEN)["status"] == "duplicate"         # sms_seen, new key
        assert tools.ingest_sms(None, "AX-HDFCBK", SMS_REVIEW, None)["status"] == "duplicate"          # sms_inbox, new key
        assert tools.add_transaction("75", "debit", "2026-10-08", payee="Tea", upi_ref="123456789012")["status"] == "duplicate"
        assert n_txns() == before
    assert seal_sweep.blind_key_changed(who.uid) is False
    with userctx.bound(who.ctx):
        assert seal_sweep.reblind(who.ctx)["rebuilt"] == 0                          # safe to run again: nothing left to do


def test_a_resend_that_arrived_while_the_key_was_changed_is_merged_not_doubled(monkeypatch):
    who = friend()
    with userctx.bound(who.ctx):
        history()
        before = n_txns()
    monkeypatch.setenv("RECTIFI_BLIND_KEY", NEW_KEY)
    with userctx.bound(signed_out(who.ctx)):                                       # the phone resends while nobody is signed in
        assert tools.ingest_sms(None, "AX-HDFCBK", SMS_WITH_REF, WHEN)["status"] == "added"      # looks new under the new key (the window)
    with userctx.bound(who.ctx):
        assert n_txns()["n"] == before["n"] + 1
        result = seal_sweep.reblind(who.ctx)
        assert result["merged"] >= 1
        assert n_txns() == before                                                  # one row per payment, same money
    assert seal_sweep.blind_key_changed(who.uid) is False


def test_the_sign_in_hook_starts_the_remake_and_a_friend_with_the_same_key_starts_nothing(monkeypatch):
    who = friend()
    with userctx.bound(who.ctx):
        history()
    started = []
    real = userctx.spawn
    monkeypatch.setattr(userctx, "spawn", lambda target, *a, name, **k: started.append(name))
    hooks.account_after_sign_in(who.ctx)
    assert "rectifi-reblind" not in started
    monkeypatch.setenv("RECTIFI_BLIND_KEY", NEW_KEY)
    hooks.account_after_sign_in(who.ctx)
    assert "rectifi-reblind" in started
    monkeypatch.setattr(userctx, "spawn", real)
    hooks.account_after_sign_in(who.ctx)
    for t in threading.enumerate():
        if t.name == "rectifi-reblind":
            t.join(30)
    assert seal_sweep.blind_key_changed(who.uid) is False


def test_the_owner_claim_records_the_key_so_a_later_change_is_noticed(monkeypatch):
    token, _ = users.signup(users.create_invite("owner")["code"], "prag", PASSWORD)
    owner = users.confirm_signup(token)
    assert account.blind_fp(owner.uid) == vault.blind_key_fingerprint()
    monkeypatch.setenv("RECTIFI_BLIND_KEY", NEW_KEY)
    assert seal_sweep.blind_key_changed(owner.uid) is True


# ───────────────────────── the key is required ─────────────────────────

def test_multiuser_mode_refuses_to_start_without_the_key(monkeypatch):
    monkeypatch.delenv("RECTIFI_BLIND_KEY")
    assert "RECTIFI_BLIND_KEY" in (config.blind_key_problem() or "")
    assert "RECTIFI_BLIND_KEY" in (auth.startup_problem() or "")
    with pytest.raises(auth.StartupRefused, match="RECTIFI_BLIND_KEY"):
        install_session_lock(create_app())
    monkeypatch.setenv("RECTIFI_BLIND_KEY", "too short")
    assert config.blind_key_problem()
    monkeypatch.setenv("RECTIFI_BLIND_KEY", OLD_KEY)
    assert config.blind_key_problem() is None


def test_without_the_key_no_hash_is_made(monkeypatch):
    who = friend()
    monkeypatch.delenv("RECTIFI_BLIND_KEY")
    with pytest.raises(vault.BlindKeyMissing):
        vault.blind_ingest("512345678901", who.ctx)


def test_the_phone_gateway_will_not_start_without_the_key(monkeypatch):
    monkeypatch.delenv("RECTIFI_BLIND_KEY")
    with pytest.raises(RuntimeError, match="RECTIFI_BLIND_KEY"):
        with TestClient(sms_gateway.create_app()):
            pass


def test_laptop_mode_needs_no_key(monkeypatch):
    monkeypatch.delenv("RECTIFI_MULTIUSER")
    monkeypatch.delenv("RECTIFI_BLIND_KEY")
    assert config.blind_key_problem() is None and auth.startup_problem() is None
    assert vault.blind_ingest("512345678901") == "512345678901"
