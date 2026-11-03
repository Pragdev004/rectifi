"""WS-ACCOUNT through the real routes (RECTIFI_MULTIUSER=1): export, delete, and the public privacy facts.

People sign up with the real sign-up and confirm calls. Test data goes in through `tools` under the person's context, so the
rows are sealed exactly as the app seals them.
"""

import csv
import io
import json
import logging
import re
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from support.tenants import as_user, populate

from rectifi import account, config, db, hooks, merchant_allowlist, sealed_fields, tools, userctx, users
from rectifi.api import auth, security
from rectifi.api.app import api_routes, create_app

SECRET = "s" * 48
HEADERS = {"X-Rectifi-Request": "1"}
PASSWORD = "correct horse battery"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", SECRET)
    monkeypatch.setattr(hooks, "after_sign_in", lambda ctx: None)          # no sweep thread behind these tests
    users.reset_limiters()

    async def no_pause(seconds):
        return None

    monkeypatch.setattr(auth, "pause", no_pause)
    yield
    users.reset_limiters()


def new_client() -> TestClient:
    return TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS, raise_server_exceptions=False)


def sign_up(username: str, kind="friend"):
    """A confirmed person with a cookie. Returns (client, context with the data key)."""
    client = new_client()
    code = users.create_invite(kind)["code"]
    body = client.post("/api/auth/signup", json={"invite": code, "username": username, "password": PASSWORD}).json()
    assert client.post("/api/auth/signup/confirm", json={"signup_token": body["signup_token"]}).status_code == 200
    attempt = users.login(username, PASSWORD)
    assert attempt.status == "ok"
    return client, attempt.signed_in.ctx


def unseal_guard_off(uid: int) -> None:
    """Let a test write plain rows for this person (tenants.populate does raw INSERTs)."""
    with db.connect_system("test: switch the guard off") as conn:
        conn.execute("UPDATE users SET encrypted = false WHERE id = %s", (uid,))


def add_ledger(ctx, tag: str):
    with userctx.bound(ctx):
        tools.add_account("Savings", "1234")
        tools.add_transaction("250.50", "debit", "2026-10-05", payee=f"{tag} Swiggy Order", category="Food & Dining", note=f"{tag} note",
                              upi_ref=f"{abs(hash(tag)) % 10**12:012d}", raw_text=f"{tag} raw sms text")
        tools.add_transaction("1000", "credit", "2026-10-06", payee=f"{tag} Pocket Money", category="Salary & Allowance")
        tools.add_transaction("99.99", "debit", "2026-10-07", payee=f"{tag} Chai", category="Food & Dining")


def unzip(response) -> dict[str, list[dict]]:
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    return {name[:-4]: list(csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8", newline="")))
            for name in archive.namelist() if name.endswith(".csv")}


def row_count(table: str, uid: int) -> int:
    with as_user(uid), db.connect() as conn:
        return conn.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()["n"]


# ───────────────────────── export ─────────────────────────

def test_export_has_exactly_the_signed_in_persons_rows_decrypted_and_money_equal_to_tools_totals():
    a_client, a = sign_up("alice")
    b_client, b = sign_up("bob")
    add_ledger(a, "AAA")
    add_ledger(b, "BBB")
    files = unzip(a_client.get("/api/account/export"))
    txns = files["transactions"]
    assert len(txns) == 3
    assert {r["payee"] for r in txns} == {"AAA Swiggy Order", "AAA Pocket Money", "AAA Chai"}       # decrypted, and none of Bob's
    assert "BBB" not in json.dumps(files)
    first = next(r for r in txns if r["payee"] == "AAA Swiggy Order")
    assert first["note"] == "AAA note" and first["raw_text"] == "AAA raw sms text" and first["upi_ref"].isdigit()
    assert first["amount"] == "250.50" and first["category"] == "Food & Dining" and first["merchant_key"] == "swiggy"
    # money equals tools: the same sums, as exact decimals
    with userctx.bound(a):
        summary = tools.spending_summary(date(2026, 10, 1), date(2026, 10, 31))
    kinds = {r["name"]: r["kind"] for r in files["categories"]}
    spent = sum((Decimal(r["amount"]) for r in txns if r["direction"] == "debit" and kinds[r["category"]] == "expense"), Decimal(0))
    earned = sum((Decimal(r["amount"]) for r in txns if r["direction"] == "credit" and kinds[r["category"]] == "income"), Decimal(0))
    assert spent == summary["spending"] == Decimal("350.49") and earned == summary["income"] == Decimal("1000")
    # Bob's export is his own
    assert {r["payee"] for r in unzip(b_client.get("/api/account/export"))["transactions"]} == {"BBB Swiggy Order", "BBB Pocket Money", "BBB Chai"}


def test_export_defuses_formula_text_but_leaves_numbers_alone():
    client, ctx = sign_up("alice")
    with userctx.bound(ctx):
        tools.add_account("Card", "9999")
        for payee in ("=HYPERLINK(\"http://x\")", "+cmd|calc", "-2+3", "@SUM(A1)", "normal shop"):
            tools.add_transaction("10", "debit", "2026-10-05", payee=payee, note="\t=tab")
    unseal_guard_off(ctx.uid)
    with as_user(ctx), db.connect() as conn:
        conn.execute("INSERT INTO balance_snapshots (account_id, as_of, balance) VALUES ((SELECT id FROM accounts LIMIT 1), '2026-10-01', -500.00)")
    files = unzip(client.get("/api/account/export"))
    payees = {r["payee"] for r in files["transactions"]}
    assert payees == {"'=HYPERLINK(\"http://x\")", "'+cmd|calc", "'-2+3", "'@SUM(A1)", "normal shop"}
    assert {r["note"] for r in files["transactions"]} == {"'\t=tab"}
    assert files["balance_snapshots"][0]["balance"] == "-500.00"                  # a number, not text: untouched
    for rows in files.values():
        for r in rows:
            for key, value in r.items():
                assert not value or value[0] not in "=+@\t\r", (key, value)        # no cell can start a formula
                assert not value.startswith("-") or re.fullmatch(r"-\d+(\.\d+)?", value), (key, value)


def test_export_leaves_out_secrets_hashes_and_envelopes():
    client, ctx = sign_up("alice")
    add_ledger(ctx, "AAA")
    unseal_guard_off(ctx.uid)
    populate(ctx, "p")
    unseal_guard_off(ctx.uid)
    body = client.get("/api/account/export")
    files = unzip(body)
    flat = json.dumps(files)
    assert "rx1." not in flat and "rs1." not in flat and "b1:" not in flat
    assert "token_hash" not in files["phone_devices"][0] and files["phone_devices"][0]["name"] == "Pixel"
    assert not {"endpoint", "p256dh", "auth"} & set(files["push_subscriptions"][0])
    assert "https://push.example" not in flat and "hashp" not in flat
    assert "user_id" not in flat and "fingerprint" not in flat
    assert body.headers["cache-control"] == "no-store" and "attachment" in body.headers["content-disposition"]
    assert "formula" in zipfile.ZipFile(io.BytesIO(body.content)).read("README.txt").decode()


def test_export_needs_a_session_and_a_friend_may_call_it():
    assert new_client().get("/api/account/export").status_code == 401
    assert security.friend_allowed("GET", "/api/account/export") and security.friend_allowed("DELETE", "/api/account")
    assert security.friend_allowed("GET", "/api/privacy")


def test_export_in_laptop_mode_is_the_laptop_persons_plain_rows(monkeypatch):
    monkeypatch.delenv("RECTIFI_MULTIUSER")
    monkeypatch.delenv("RECTIFI_SESSION_SECRET")
    tools.add_account("Savings", "1234")
    tools.add_transaction("70", "debit", "2026-10-05", payee="Plain Cafe", upi_ref="123456789012")
    files = unzip(TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS).get("/api/account/export"))
    assert files["transactions"][0]["payee"] == "Plain Cafe" and files["transactions"][0]["upi_ref"] == "123456789012"


def test_every_tenant_table_is_exported_or_excluded_with_a_reason_and_wiped_for_the_owner():
    assert set(account.EXPORT_ORDER) | set(account.EXCLUDED_TABLES) == set(db.TENANT_TABLES)
    assert not set(account.EXPORT_ORDER) & set(account.EXCLUDED_TABLES)
    assert all(account.EXCLUDED_TABLES.values())
    assert set(account.EXPORT_ORDER) <= set(account.DROP_COLUMNS) | {"transactions"}
    for table, cols in account.DROP_COLUMNS.items():
        assert "user_id" in cols, table


def test_cell_formats_money_as_exact_text_never_through_float():
    assert account.cell(Decimal("0.10")) == "0.10" and account.cell(Decimal("1E+3")) == "1000"
    assert account.cell(Decimal("-5.00")) == "-5.00" and account.cell(None) == ""
    assert account.cell("=1+1") == "'=1+1" and account.cell("fine") == "fine"
    assert account.cell(date(2026, 10, 5)) == "2026-10-05" and account.cell(True) == "true"


# ───────────────────────── delete ─────────────────────────

def delete(client, **body):
    return client.request("DELETE", "/api/account", json=body)


def test_delete_leaves_zero_rows_for_that_person_in_every_table_others_untouched_and_the_cookie_dead():
    a_client, a = sign_up("alice")
    b_client, b = sign_up("bob")
    for ctx in (a, b):
        unseal_guard_off(ctx.uid)
        populate(ctx)
    before_b = {t: row_count(t, b.uid) for t in db.TENANT_TABLES}
    before_owner = {t: row_count(t, 1) for t in db.TENANT_TABLES}
    assert sum(row_count(t, a.uid) for t in db.TENANT_TABLES) > 20
    cookie = a_client.cookies[auth.COOKIE_NAME]

    r = delete(a_client, password=PASSWORD, confirm_username="alice")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert auth.COOKIE_NAME in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()

    with db.connect_system("test: look at the people tables") as conn:
        assert conn.execute("SELECT count(*) AS n FROM users WHERE id = %s", (a.uid,)).fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM user_sessions WHERE user_id = %s", (a.uid,)).fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM invites WHERE reserved_by = %s", (a.uid,)).fetchone()["n"] == 0
    # walk pg_tables: every table with a user_id column holds nothing for that person
    with db.connect_system("test: list tables") as conn:
        tables = [r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' AND column_name = 'user_id'").fetchall()]
    assert set(db.TENANT_TABLES) <= set(tables)
    for table in set(tables) - {"user_sessions"}:
        assert row_count(table, a.uid) == 0, f"{table} still has rows of the deleted person"
    assert {t: row_count(t, b.uid) for t in db.TENANT_TABLES} == before_b                 # others untouched
    assert {t: row_count(t, 1) for t in db.TENANT_TABLES} == before_owner
    # the cookie is dead, also if someone kept a copy; the login is gone too
    assert a_client.get("/api/meta").status_code == 401
    stale = new_client()
    stale.cookies.set(auth.COOKIE_NAME, cookie)
    assert stale.get("/api/meta").status_code == 401
    assert users.login("alice", PASSWORD).status == "invalid"
    assert b_client.get("/api/meta").status_code == 200


def test_delete_with_a_wrong_password_deletes_nothing_and_counts_against_the_account():
    client, ctx = sign_up("alice")
    add_ledger(ctx, "AAA")
    r = delete(client, password="not my password", confirm_username="alice")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_password"
    with db.connect_system("test: failures") as conn:
        assert len(conn.execute("SELECT failures FROM users WHERE id = %s", (ctx.uid,)).fetchone()["failures"]) == 1
    assert row_count("transactions", ctx.uid) == 3 and client.get("/api/meta").status_code == 200


def test_delete_needs_the_typed_username_and_the_password():
    client, ctx = sign_up("alice")
    add_ledger(ctx, "AAA")
    assert delete(client, password=PASSWORD, confirm_username="bob").json()["error"]["field"] == "confirm_username"
    assert delete(client, password=PASSWORD).status_code == 422
    assert delete(client, confirm_username="alice").status_code == 422
    assert delete(client).status_code == 422
    assert row_count("transactions", ctx.uid) == 3
    assert delete(client, password=PASSWORD, confirm_username="ALICE").status_code == 200       # names are not case sensitive


def test_delete_without_a_session_is_refused():
    sign_up("alice")
    r = new_client().request("DELETE", "/api/account", json={"password": PASSWORD, "confirm_username": "alice"})
    assert r.status_code == 401
    assert users.login("alice", PASSWORD).status == "ok"


def test_the_owner_cannot_delete_row_1_while_friends_exist():
    owner_client, owner = sign_up("owner-person", kind="owner")
    assert owner.uid == 1
    _, friend = sign_up("friend")
    add_ledger(friend, "FFF")
    r = delete(owner_client, password=PASSWORD, confirm_username="owner-person")
    assert r.status_code == 409 and r.json()["error"]["code"] == "owner_has_friends"
    assert row_count("transactions", friend.uid) == 3
    with db.connect_system("test: owner still there") as conn:
        assert conn.execute("SELECT verifier IS NOT NULL AS has FROM users WHERE id = 1").fetchone()["has"] is True


def test_a_lone_owner_can_wipe_everything_and_start_again_with_a_new_owner_invite():
    owner_client, owner = sign_up("owner-person", kind="owner")
    unseal_guard_off(1)
    populate(owner)
    assert row_count("transactions", 1) > 0
    r = delete(owner_client, password=PASSWORD, confirm_username="owner-person")
    assert r.status_code == 200
    kept = {"categories", "merchant_rules", "app_settings"}              # seed_user puts the defaults and its marker back
    assert {t: row_count(t, 1) for t in set(db.TENANT_TABLES) - kept} == {t: 0 for t in set(db.TENANT_TABLES) - kept}
    with as_user(1), db.connect() as conn:
        assert [r["key"] for r in conn.execute("SELECT key FROM app_settings").fetchall()] == ["categories_v2"]
    assert owner_client.get("/api/meta").status_code == 401
    with db.connect_system("test: row 1 is unclaimed") as conn:
        row = conn.execute("SELECT username, verifier, dek_by_password, confirmed_at, encrypted, sweep_state FROM users WHERE id = 1").fetchone()
    assert dict(row) == {"username": "owner", "verifier": None, "dek_by_password": None, "confirmed_at": None, "encrypted": False, "sweep_state": None}
    assert users.create_invite("owner")["code"]                                           # the door is open again
    assert row_count("categories", 1) > 5                                                 # the defaults are back


def test_delete_in_laptop_mode_is_refused(monkeypatch):
    monkeypatch.delenv("RECTIFI_MULTIUSER")
    monkeypatch.delenv("RECTIFI_SESSION_SECRET")
    r = TestClient(create_app(), base_url="http://127.0.0.1:8501", headers=HEADERS).request(
        "DELETE", "/api/account", json={"password": "x" * 12, "confirm_username": "owner"})
    assert r.status_code in (400, 401) and r.json()["error"]["code"] in ("not_multiuser", "auth_required", "auth_off")
    assert db.connect_system and row_count("categories", 1) > 5


def test_a_password_never_reaches_the_log(caplog):
    client, ctx = sign_up("alice")
    with caplog.at_level(logging.DEBUG):
        delete(client, password="a secret passphrase xyz", confirm_username="alice")
        delete(client, password=PASSWORD, confirm_username="alice")
    assert "a secret passphrase" not in caplog.text and PASSWORD not in caplog.text


def test_the_stop_signal_that_a_deleted_person_leaves_in_memory_is_cleared():
    from rectifi import categorize

    categorize._index_cache[(77, "stamp")] = object()
    categorize._index_cache[(78, "stamp")] = object()
    account._forget_in_memory(77)
    assert (77, "stamp") not in categorize._index_cache and (78, "stamp") in categorize._index_cache
    categorize._index_cache.pop((78, "stamp"))


# ───────────────────────── privacy ─────────────────────────

def test_privacy_is_public_and_lists_every_sealed_field_and_every_brand_and_agrees_with_config():
    sign_up("alice")
    r = new_client().get("/api/privacy")                                                 # no cookie
    assert r.status_code == 200
    facts = r.json()
    assert facts["mode"] == "multiuser" and facts["encryption_at_rest"] is True and config.multiuser()
    assert set(facts["sealed_fields"]) == {f"{t}.{c}" for t, c in sealed_fields.all_sealed()}
    assert set(facts["keyed_hash_fields"]) == {f"{t}.{c}" for t, c in sealed_fields.all_blind()}
    assert set(facts["hidden_key_fields"]) == set(sealed_fields.all_hidden())
    assert {b["name"] for b in facts["merchant_allowlist"]} == set(merchant_allowlist.NAMES.values())
    assert {b["key"] for b in facts["merchant_allowlist"]} == set(merchant_allowlist.KEYS)
    assert facts["plain_fields"] == list(sealed_fields.PLAIN_ON_PURPOSE)
    assert facts["ai_for_friends"] is False and facts["unsealed_owner_tables"] == list(sealed_fields.OWNER_ONLY_TABLES)
    assert len(facts["who_can_read"]) == 4 and any("pass through the server in clear" in w["sealed"] for w in facts["who_can_read"])
    assert any("recovery code" in line for line in facts["limits"]) and "no admin reset" in facts["recovery"]


def test_privacy_model_provider_and_mode_follow_the_settings(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert account.privacy_facts()["model_provider"] == "local"
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com")
    assert account.privacy_facts()["model_provider"] == "cloud"
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    assert account.privacy_facts()["model_provider"] == "cloud" and account.privacy_facts()["model"] == "openrouter"
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:1234/v1")
    assert account.privacy_facts()["model_provider"] == "local"
    monkeypatch.delenv("RECTIFI_MULTIUSER")
    facts = account.privacy_facts()
    assert facts["mode"] == "laptop" and facts["encryption_at_rest"] is False


def test_privacy_backup_days_match_the_compose_file():
    compose = (ROOT / "deploy" / "compose.oracle.yml").read_text(encoding="utf-8")
    keep = int(re.search(r"tail -n \+(\d+)", compose).group(1)) - 1
    assert account.BACKUP_DAYS == keep == 7


def test_privacy_holds_no_user_data_and_needs_no_database(monkeypatch):
    client, ctx = sign_up("alice")
    add_ledger(ctx, "CANARY")
    body = new_client().get("/api/privacy").text
    assert "CANARY" not in body and "alice" not in body
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:nothing@127.0.0.1:1/none")      # unreachable: still answers
    db.close_pool()
    assert new_client().get("/api/privacy").status_code == 200


def test_the_privacy_document_lists_every_sealed_field_every_brand_and_every_plain_item():
    text = (ROOT / "docs" / "PRIVACY.md").read_text(encoding="utf-8")
    low = text.lower()
    assert len(text.splitlines()) < 200
    for table, column in sealed_fields.all_sealed() + sealed_fields.all_blind():
        assert f"{table}.{column}" in text, f"PRIVACY.md does not mention {table}.{column}"
    for name in sealed_fields.all_hidden():
        assert name in text, f"PRIVACY.md does not mention the hidden key {name}"
    for brand in merchant_allowlist.ALLOWED:
        assert brand.name.lower() in low, f"PRIVACY.md does not mention {brand.name}"
    for item in sealed_fields.PLAIN_ON_PURPOSE:
        assert item.lower() in low, f"PRIVACY.md does not mention the plain item {item!r}"
    for table in sealed_fields.OWNER_ONLY_TABLES:
        assert table in text
    for line in account.WHO_CAN_READ:
        assert line["who"].split(",")[0].lower() in low
    for needle in ("no password reset", "recovery code", "has not been reviewed", "backup"):
        assert needle in low, needle


def test_the_account_routes_are_registered_once_and_laptop_routes_are_unchanged():
    paths = [(r.path, tuple(sorted(r.methods or ()))) for r in api_routes()]
    assert paths.count(("/account/export", ("GET", "HEAD"))) <= 1
    names = {p for p, _ in paths}
    assert {"/account/export", "/account", "/privacy"} <= names
