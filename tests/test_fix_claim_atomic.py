"""Review round 2, N1 (MED): the owner's re-key and `confirmed_at` are ONE transaction, and a purge and a confirm take the
same locks in the same order. Either the confirm wins fully, or the purge wins and the old rows are untouched. Never rows
hashed under keys that were thrown away, and never 'log it and carry on'."""

import threading
import time

import pytest

from rectifi import db, seal_sweep, tools, userctx, users

PASSWORD = "correct horse battery staple"


def laptop_rows():
    tools.add_account("HDFC", "1234")
    tools.add_transaction("75", "debit", "2026-10-08", payee="Tea", upi_ref="123456789012")


def pending_claim(monkeypatch):
    """Laptop history is written first (single-user mode); then the server goes multi-user and the owner claims row 1."""
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "x" * 40)
    users.reset_limiters()
    return users.signup(users.create_invite("owner")["code"], "prag", PASSWORD)[0]


def row_one():
    with db.connect_system("tests: row 1") as conn:
        return conn.execute("SELECT username, confirmed_at IS NOT NULL AS confirmed, verifier IS NULL AS no_verifier, "
                            "ingest_salt IS NULL AS no_salt FROM users WHERE id = 1").fetchone()


def upi_ref():
    with userctx.bound(userctx.UserContext(uid=1, strict=False)), db.connect() as conn:
        return conn.execute("SELECT upi_ref FROM transactions").fetchone()["upi_ref"]


def pending_sessions():
    with db.connect_system("tests: pending sessions") as conn:
        return conn.execute("SELECT count(*) AS n FROM user_sessions WHERE pending").fetchone()["n"]


def test_a_purge_during_the_rekey_never_leaves_rows_keyed_under_discarded_keys(monkeypatch):
    """The reviewer's probe P4. A purge from another request runs while the confirm is re-keying. Before the fix Postgres
    aborted the confirm with a deadlock and row 1 was reset while the rows were already `b1:`."""
    laptop_rows()
    token = pending_claim(monkeypatch)
    real = seal_sweep.rekey_dedupe
    box = {}

    def other_request():
        try:
            box["purged"] = users.purge_unconfirmed(users._now() + 31 * 60)
        except Exception as exc:                                              # noqa: BLE001
            box["purge_error"] = type(exc).__name__

    def slow_rekey(ctx, **kw):
        out = real(ctx, **kw)
        box["thread"] = threading.Thread(target=other_request, daemon=True)
        box["thread"].start()
        time.sleep(1.5)                                                       # the purge runs while the confirm is still open
        return out

    monkeypatch.setattr(seal_sweep, "rekey_dedupe", slow_rekey)
    signed_in = users.confirm_signup(token)                                   # no deadlock: the confirm wins
    box["thread"].join(30)
    assert "purge_error" not in box, box
    row = row_one()
    assert row["confirmed"] and not row["no_verifier"] and not row["no_salt"]
    assert upi_ref().startswith("b1:")
    assert signed_in.is_owner


def test_when_the_purge_wins_the_old_rows_are_untouched(monkeypatch):
    laptop_rows()
    token = pending_claim(monkeypatch)
    users.purge_unconfirmed(users._now() + 31 * 60)                           # the claim expired and was cleared
    with pytest.raises(users.AuthError) as err:
        users.confirm_signup(token)
    assert err.value.code == "signup_expired"
    row = row_one()
    assert not row["confirmed"] and row["no_verifier"] and row["no_salt"] and row["username"] == "owner"
    assert upi_ref() == "123456789012"                                         # still the plain laptop value


def test_a_purge_that_holds_the_row_first_makes_the_confirm_stop_before_it_re_keys(monkeypatch):
    """The other order: the purge has row 1 locked when the confirm arrives. The confirm waits, then finds its session gone."""
    laptop_rows()
    token = pending_claim(monkeypatch)
    out = {}

    def confirm():
        try:
            users.confirm_signup(token)
            out["result"] = "confirmed"
        except users.AuthError as exc:
            out["result"] = exc.code

    with db.connect_system("tests: play the purge") as conn:
        conn.execute("SELECT 1 FROM users WHERE id = 1 FOR UPDATE")
        thread = threading.Thread(target=confirm, daemon=True)
        thread.start()
        time.sleep(1.0)                                                        # the confirm is now waiting for row 1
        users._reset_owner_claim(conn)
        conn.execute("UPDATE invites SET reserved_by = NULL WHERE reserved_by = 1 AND used_at IS NULL")
        conn.execute("DELETE FROM user_sessions WHERE pending")
    thread.join(30)
    assert out == {"result": "signup_expired"}
    assert row_one()["no_verifier"]
    assert upi_ref() == "123456789012"


def test_a_failed_rekey_fails_the_confirm_and_leaves_it_retryable(monkeypatch):
    laptop_rows()
    token = pending_claim(monkeypatch)
    real = seal_sweep.rekey_dedupe

    def fails_late(ctx, **kw):
        real(ctx, **kw)                                                        # the work is done on the confirm's connection ...
        raise RuntimeError("disk on fire")                                     # ... and then it breaks

    monkeypatch.setattr(seal_sweep, "rekey_dedupe", fails_late)
    with pytest.raises(users.AuthError) as err:
        users.confirm_signup(token)
    assert err.value.code == "confirm_failed" and "disk" not in err.value.message
    row = row_one()
    assert not row["confirmed"]                                                # not half-done
    assert upi_ref() == "123456789012"                                         # the re-key was rolled back with it
    assert pending_sessions() == 1                                             # the same token still works

    monkeypatch.setattr(seal_sweep, "rekey_dedupe", real)
    signed_in = users.confirm_signup(token)                                    # retry succeeds
    assert signed_in.is_owner and row_one()["confirmed"]
    assert upi_ref().startswith("b1:")
