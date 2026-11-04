"""Integration: one friend's whole life through the real routes in multi-user mode (docs/MULTIUSER_PLAN.md section 7, last step).

invite (the real `dev.py invite`) -> sign-up -> confirm -> account -> statement import -> ledger -> a second person cannot see any of
it -> an SMS through the gateway while signed out -> sign-in unlocks it. Reads the database to prove what was stored.
"""

from __future__ import annotations

import os

from support import proof


def test_a_friend_signs_up_imports_gets_an_sms_while_signed_out_and_unlocks_it(monkeypatch):
    proof.multiuser_env(monkeypatch)
    proof.pin_clock(monkeypatch)
    app, gw = proof.make_app(), proof.gateway_client()

    # invite -> sign-up -> confirm
    alice = proof.new_friend(app, "alice")
    status = alice.get("/api/auth/status").json()
    assert status["signed_in"] and status["multiuser"] and status["username"] == "alice" and not status["is_owner"]
    bob = proof.new_friend(app, "bob")

    # an account and a statement import (the CSV's own account digits are 4417)
    word = "zqxflow" + os.urandom(3).hex()
    acct = proof.first_id(alice.post("/api/accounts", {"name": "Main", "last4": "4417"}).json(), "account")
    rows = [("03/10/26", f"UPI-{word.upper()}-{word}@icici-ICIC0000103-429183746501-PAYMENT", "0000429183746501", "123.45")]
    done = proof.upload_statement(alice.client, "oct.csv", proof.statement_csv(rows)).json()
    if done.get("outcome") == "preview":
        done = proof.commit_preview(alice.client, done, acct).json()
    assert done.get("outcome") == "imported"
    assert proof.count_rows(alice.uid, "transactions") == 1

    # the ledger shows it to Alice, as text; the table holds an envelope; Bob sees nothing
    ledger = proof.items_of(alice.get("/api/transactions").json())
    assert len(ledger) == 1 and str(ledger[0]["amount"]) == "123.45"
    assert word in alice.get("/api/transactions", params={"search": word}).text.lower()
    with proof.raw_conn(alice.uid) as conn:
        stored = conn.execute("SELECT payee, description FROM transactions").fetchone()
    assert all(v is None or v.startswith("rx1.") for v in stored), stored
    assert proof.items_of(bob.get("/api/transactions").json()) == []
    assert word not in bob.get("/api/transactions", params={"search": word}).text.lower()
    assert bob.get("/api/imports/%s" % proof.first_id(done, "batch")).status_code in (403, 404)

    # a phone SMS while Alice is signed out: accepted, stored sealed to her public key, nobody can read it from the table
    token = alice.post("/api/phone/devices", {"name": "Alice phone"}).json()["token"]
    alice.logout()
    assert alice.get("/api/transactions").status_code == 401
    sms_word = "zqxsms" + os.urandom(3).hex()
    sent = gw.post("/sms", headers={"Authorization": f"Bearer {token}"}, json={"from": "AX-HDFCBK", "text": proof.sms_debit("77.77", sms_word)})
    assert sent.status_code == 200 and sent.json() == {"status": "added"}
    with proof.raw_conn(alice.uid) as conn:
        payee, unlocked = conn.execute("SELECT payee, unlocked FROM transactions WHERE amount = 77.77").fetchone()
    assert payee.startswith("rs1.") and unlocked is False and sms_word not in payee
    assert proof.count_rows(bob.uid, "transactions") == 0

    # signing in again opens it (rs1 to rx1) and the ledger reads it
    alice.login()
    for _ in range(60):
        with proof.raw_conn(alice.uid) as conn:
            payee, unlocked = conn.execute("SELECT payee, unlocked FROM transactions WHERE amount = 77.77").fetchone()
        if unlocked and payee.startswith("rx1."):
            break
        proof.time.sleep(0.25)
    assert unlocked and payee.startswith("rx1."), "sign-in did not open the SMS the phone sent while signed out"
    assert sms_word in alice.get("/api/transactions", params={"search": sms_word}).text.lower()
    assert proof.count_rows(alice.uid, "transactions") == 2 and proof.count_rows(bob.uid, "transactions") == 0


def test_starting_the_server_in_multiuser_mode_logs_no_error(monkeypatch, caplog):
    """The lifespan must not call per-person work with nobody bound (it used to log NoUser from jobs.requeue_unfinished)."""
    from starlette.testclient import TestClient

    proof.multiuser_env(monkeypatch)
    monkeypatch.delenv("RECTIFI_NO_ALERT_BACKGROUND", raising=False)
    with caplog.at_level("WARNING"):
        with TestClient(proof.make_app(), base_url=proof.BASE, headers=proof.HEADERS):
            pass
    bad = [r.getMessage() for r in caplog.records if r.levelname in ("ERROR", "CRITICAL") or "NoUser" in (r.exc_text or "")]
    assert not bad, bad
