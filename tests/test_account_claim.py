"""The owner claim end to end, through the real routes: old laptop-mode rows, `invite --owner`, sign-up and confirm, the real
sign-in hook starts the sweep in a background thread, and afterwards everything reads back, nothing plain is left, a second
sign-in changes nothing, and the owner's export holds the same totals as before.
"""

import csv
import io
import threading
import zipfile
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from starlette.testclient import TestClient
from support.tenants import as_user, populate

from rectifi import account, db, importer, seal_sweep, tools, users
from rectifi.api import auth
from rectifi.api.app import create_app

PASSWORD = "correct horse battery"
CSV = (Path(__file__).resolve().parent / "fixtures" / "hdfc_style_statement.csv").read_bytes()


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "s" * 48)
    users.reset_limiters()

    async def no_pause(seconds):
        return None

    monkeypatch.setattr(auth, "pause", no_pause)
    yield
    users.reset_limiters()


def wait_for_sweep():
    for thread in threading.enumerate():
        if thread.name == "rectifi-seal-sweep":
            thread.join(timeout=60)
            assert not thread.is_alive()


def stored_plain_payees():
    with as_user(1), db.connect() as conn:
        conn.row_factory = psycopg.rows.dict_row
        return [r["payee"] for r in conn.execute("SELECT payee FROM transactions WHERE payee IS NOT NULL").fetchall()
                if not r["payee"].startswith(("rx1.", "rs1."))]


def export_rows(client) -> dict:
    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/account/export").content))
    return {n[:-4]: list(csv.DictReader(io.TextIOWrapper(archive.open(n), encoding="utf-8", newline="")))
            for n in archive.namelist() if n.endswith(".csv")}


def test_owner_claim_seals_old_data_in_the_background_and_every_total_stays_the_same(monkeypatch):
    # months of laptop use: plain rows, a statement import, a learned rule
    populate(1)
    tools.add_transaction("120", "debit", "2026-10-06", payee="Chaayos Cafe", note="with team", upi_ref="123456789012")
    first = importer.import_file(CSV, "oct.csv", tools.list_accounts()[0]["id"])
    assert first.added > 0
    with as_user(1), db.connect() as conn:
        total_before = conn.execute("SELECT count(*) AS n, sum(amount) AS s FROM transactions").fetchone()
        budgets_before = conn.execute("SELECT sum(limit_amount) AS s FROM budgets").fetchone()["s"]
    assert stored_plain_payees()

    # Prag turns multi-user mode on and claims row 1 with the owner invite
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    client = TestClient(create_app(), base_url="http://127.0.0.1:8501", headers={"X-Rectifi-Request": "1"}, raise_server_exceptions=False)
    code = users.create_invite("owner")["code"]
    body = client.post("/api/auth/signup", json={"invite": code, "username": "prag", "password": PASSWORD}).json()
    assert account.sweep_state(1) == (None, False)                                  # unconfirmed: nothing starts yet
    confirmed = client.post("/api/auth/signup/confirm", json={"signup_token": body["signup_token"]})
    assert confirmed.status_code == 200 and confirmed.json()["is_owner"] is True
    wait_for_sweep()                                                                # the real hook started the real thread

    assert account.sweep_state(1) == ("done", True)
    assert stored_plain_payees() == []
    rows = export_rows(client)                                                       # decrypted through the owner's session
    assert len(rows["transactions"]) == total_before["n"]
    assert sum(Decimal(r["amount"]) for r in rows["transactions"]) == total_before["s"]
    assert sum(Decimal(r["limit_amount"]) for r in rows["budgets"]) == budgets_before
    assert {"Chaayos Cafe"} <= {r["payee"] for r in rows["transactions"]}
    assert any(r["upi_ref"] == "123456789012" for r in rows["transactions"])

    # signing in again: nothing to resume, nothing changes; the old statement still finds its duplicates
    stored = [dict(r) for r in _raw("SELECT * FROM transactions ORDER BY id")]
    again = TestClient(create_app(), base_url="http://127.0.0.1:8501", headers={"X-Rectifi-Request": "1"}, raise_server_exceptions=False)
    assert again.post("/api/auth/login", json={"username": "prag", "password": PASSWORD}).status_code == 200
    wait_for_sweep()
    assert [dict(r) for r in _raw("SELECT * FROM transactions ORDER BY id")] == stored
    assert seal_sweep.status(1)["phase"] == "done"


def _raw(sql):
    with as_user(1), db.connect() as conn:
        conn.row_factory = psycopg.rows.dict_row
        return conn.execute(sql).fetchall()
