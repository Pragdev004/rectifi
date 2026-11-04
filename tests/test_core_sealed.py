"""WS-CORE: every write of a sealed column is sealed, dedupe uses keyed hashes, merchant_key is the allow-list brand only,
caches and uploads are per person, and the sealed SMS flow (arrives signed out, finished at sign-in).

Multi-user mode is switched on per test (RECTIFI_MULTIUSER=1). People come from tests/support/tenants.py.
"""

from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from starlette.testclient import TestClient
from support.tenants import as_user, count, make_user

from rectifi import categorize, db, hooks, importer, sealed_fields, sms_gateway, tools, vault

CSV = (Path(__file__).resolve().parent / "fixtures" / "hdfc_style_statement.csv").read_bytes()
BANK_SMS = "Rs.250.00 debited from A/c XX1234 on 21-10-26 to VPA swiggy@icici UPI Ref No 429183746521"
OTHER_SMS = "Rs.40.00 debited from A/c XX1234 on 20-10-26 to chai point"
STAMP = 1792573200000


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")


@pytest.fixture
def client():
    return TestClient(sms_gateway.create_app())


def raw(who, sql, params=()):
    """Rows exactly as stored: the envelopes are NOT opened."""
    with as_user(who), db.connect() as conn:
        conn.row_factory = dict_row
        return conn.execute(sql, params).fetchall()


def post(client, token, text, stamp=STAMP, sender="AX-HDFCBK"):
    return client.post("/sms", headers={"Authorization": f"Bearer {token}"}, json={"from": sender, "text": text, "sentStamp": stamp})


def person(name, **kw):
    """A signed-in friend with an account and a paired phone."""
    ctx = make_user(name, encrypted=True, **kw)
    with as_user(ctx):
        tools.add_account("Savings", "1234")
        device = tools.create_device("Pixel")
    return ctx, device


def locked(ctx):
    """The same person with no data key: what the gateway sees."""
    return type(ctx)(uid=ctx.uid, key=None, public_key=ctx.public_key, ingest_salt=ctx.ingest_salt, strict=False)


def is_sealed(value) -> bool:
    return isinstance(value, str) and value.startswith(("rx1.", "rs1."))


# ───────────────────────── every sealed field is an envelope ─────────────────────────

def test_every_sealed_field_is_an_envelope_in_the_raw_table_and_reads_back_through_tools():
    friend, device = person("friend")
    with as_user(friend):
        tools.add_transaction("120", "debit", "2026-10-05", payee="Chaayos Cafe", description="UPI/DR/1/Chaayos", note="with team",
                              upi_ref="123456789012", raw_text="the full text of an sms")
        txn = tools.list_transactions()[0]
        tools.set_category(txn["id"], "Entertainment", remember=True)             # teaches a rule: its pattern is sealed
        tools.save_commitment("only two takeaways this week")
        account = tools.list_accounts()[0]["id"]
        importer.import_file(CSV, "my-hdfc-statement.csv", account)
        tools.ingest_sms(device["id"], "AX-HDFCBK", "Rs.500.00 transferred from A/c XX1234 to your wallet", None)   # needs review

        # read back through tools: plain text
        got = tools.get_transaction(txn["id"])
        assert (got["payee"], got["description"], got["note"], got["upi_ref"]) == ("Chaayos Cafe", "UPI/DR/1/Chaayos", "with team", "123456789012")
        assert got["category_reason"] == "you chose"
        assert tools.weekly_review()["commitment"] == "only two takeaways this week"
        assert tools.list_devices()[0]["name"] == "Pixel"
        [item] = tools.list_inbox()
        assert (item["sender"], item["raw_text"]) == ("AX-HDFCBK", "Rs.500.00 transferred from A/c XX1234 to your wallet")
        assert tools.get_import_batch(max(r["id"] for r in raw(friend, "SELECT id FROM import_batches")))["filename"] == "my-hdfc-statement.csv"

    # the raw tables hold envelopes for every sealed column this stream writes
    checked = set()
    for table, column in sealed_fields.all_sealed():
        if table not in ("transactions", "merchant_rules", "weekly_reviews", "phone_devices", "sms_inbox", "import_batches"):
            continue                                                           # the other tables belong to other streams
        where = " WHERE source = 'learned'" if table == "merchant_rules" else ""
        rows = raw(friend, f'SELECT "{column}" AS v FROM "{table}"{where}')
        values = [r["v"] for r in rows if r["v"] is not None]
        assert values, f"{table}.{column} was never written by this test, so it proves nothing"
        assert all(is_sealed(v) for v in values), f"{table}.{column} holds plain text"
        checked.add((table, column))
    assert ("transactions", "category_reason") in checked and ("merchant_rules", "pattern") in checked
    for column in ("upi_ref", "fingerprint"):
        values = [r["v"] for r in raw(friend, f"SELECT {column} AS v FROM transactions") if r["v"] is not None]
        assert values and all(v.startswith("b1:") for v in values)
    assert all(r["pattern_blind"].startswith("b1:") for r in raw(friend, "SELECT pattern_blind FROM merchant_rules WHERE source = 'learned'"))
    assert all(r["text_hash"].startswith("b1:") for r in raw(friend, "SELECT text_hash FROM sms_inbox UNION ALL SELECT text_hash FROM sms_seen"))


def test_the_database_refuses_plain_text_when_a_write_forgot_to_seal(monkeypatch):
    """The trigger is the real guard: if vault.seal were skipped, the write fails instead of leaking."""
    friend, _ = person("friend")
    monkeypatch.setattr(vault, "seal", lambda text, ctx=None: text)
    with as_user(friend), pytest.raises(psycopg.errors.CheckViolation):
        tools.add_transaction("10", "debit", "2026-10-05", payee="Someone")


def test_laptop_mode_stores_exactly_what_it_always_did(monkeypatch):
    monkeypatch.delenv("RECTIFI_MULTIUSER")
    tools.add_account("Savings", "1234")
    tools.add_transaction("120", "debit", "2026-10-05", payee="Chaayos Cafe", note="n", upi_ref="123456789012", raw_text="text")
    [r] = raw(1, "SELECT payee, note, raw_text, upi_ref, upi_ref_sealed, unlocked FROM transactions")
    assert (r["payee"], r["note"], r["raw_text"], r["upi_ref"], r["unlocked"]) == ("Chaayos Cafe", "n", "text", "123456789012", True)


# ───────────────────────── merchant_key ─────────────────────────

def test_merchant_key_is_set_for_brand_payees_and_null_for_the_rest():
    friend, _ = person("friend")
    with as_user(friend):
        tools.add_transaction("250", "debit", "2026-10-05", payee="swiggy@icici")
        tools.add_transaction("90", "debit", "2026-10-05", payee="Chaayos Cafe", description="UPI to amazon pay gift")
        tools.add_transaction("70", "debit", "2026-10-05", payee="Ramesh Kumar")
        tools.add_transaction("60", "debit", "2026-10-05", payee="COLA HOUSE")
        importer.import_file(CSV, "s.csv", tools.list_accounts()[0]["id"])
    keys = {r["payee_hint"]: r["merchant_key"] for r in raw(friend, "SELECT merchant_key, amount::text AS payee_hint FROM transactions")}
    assert keys["250.00"] == "swiggy"
    assert keys["90.00"] == "amazon"                     # the narration counts too
    assert keys["70.00"] is None and keys["60.00"] is None
    imported = {r["amount"]: r["merchant_key"] for r in raw(friend, "SELECT merchant_key, amount FROM transactions WHERE source = 'statement'")}
    assert imported[Decimal("1240.50")] == "uber" and imported[Decimal("199.00")] == "netflix" and imported[Decimal("2105.00")] is None
    # never from the learned-rule helper: a taught pattern is not an allow-listed brand
    with as_user(friend):
        txn = [t for t in tools.list_transactions() if t["payee"] == "Ramesh Kumar"][0]
        tools.set_category(txn["id"], "Gifts & Donations", remember=True)
        assert raw(friend, "SELECT merchant_key FROM transactions WHERE id = %s", (txn["id"],))[0]["merchant_key"] is None


# ───────────────────────── dedupe ─────────────────────────

def test_a_statement_imported_twice_is_deduped_by_keyed_hashes_and_the_sms_reference_matches_it(client):
    friend, device = person("friend")
    with as_user(friend):
        account = tools.list_accounts()[0]["id"]
        first = importer.import_file(CSV, "s.csv", account)
        second = importer.import_file(CSV, "s.csv", account)
        assert (first.added, second.added, second.duplicates) == (5, 0, 5)
    assert count("transactions", friend) == 5
    # the same payment arriving by SMS (same UPI reference as the Swiggy row) is a duplicate: blind_ingest on both sides
    sms = "Rs.250.00 debited from A/c XX4417 on 01-10-26 to VPA swiggy@icici UPI Ref No 429183746521"
    assert post(client, device["token"], sms).json() == {"status": "duplicate"}
    assert count("transactions", friend) == 5
    # and the plain reference exists only sealed
    refs = [r["upi_ref_sealed"] for r in raw(friend, "SELECT upi_ref_sealed FROM transactions WHERE upi_ref_sealed IS NOT NULL")]
    assert refs and all(is_sealed(v) for v in refs)


def test_the_same_sms_for_two_people_makes_one_row_each(client):
    a, dev_a = person("a")
    b, dev_b = person("b")
    assert post(client, dev_a["token"], BANK_SMS).json() == {"status": "added"}
    assert post(client, dev_b["token"], BANK_SMS).json() == {"status": "added"}
    assert count("transactions", a) == 1 and count("transactions", b) == 1
    # the keyed hash differs per person, so one person's reference tells nothing about another's
    ref_a = raw(a, "SELECT upi_ref FROM transactions")[0]["upi_ref"]
    ref_b = raw(b, "SELECT upi_ref FROM transactions")[0]["upi_ref"]
    assert ref_a.startswith("b1:") and ref_b.startswith("b1:") and ref_a != ref_b


def test_a_retried_sms_stays_one_row(client):
    friend, device = person("friend")
    assert post(client, device["token"], OTHER_SMS).json() == {"status": "added"}
    assert post(client, device["token"], OTHER_SMS).json() == {"status": "duplicate"}
    assert post(client, device["token"], BANK_SMS, stamp=STAMP + 1).json() == {"status": "added"}
    assert post(client, device["token"], BANK_SMS, stamp=STAMP + 2).json() == {"status": "duplicate"}    # same UPI reference
    assert count("transactions", friend) == 2


def test_a_review_sms_is_kept_once_and_sealed_to_the_public_key(client):
    friend, device = person("friend")
    unreadable = "Rs 500.00 transferred from A/c XX1234 to your wallet"
    assert post(client, device["token"], unreadable).json() == {"status": "needs_review"}
    assert post(client, device["token"], unreadable).json() == {"status": "duplicate"}
    [row] = raw(friend, "SELECT sender, raw_text, reason FROM sms_inbox")
    assert row["raw_text"].startswith("rs1.") and row["sender"].startswith("rs1.") and row["reason"].startswith("rs1.")


# ───────────────────────── the sealed SMS flow ─────────────────────────

def teach(ctx, payee, category):
    with as_user(ctx):
        tools.add_transaction("5", "debit", "2026-10-01", payee=payee)
        txn = [t for t in tools.list_transactions() if t["payee"] == payee][0]
        tools.set_category(txn["id"], category, remember=True)


def test_an_sms_that_arrives_signed_out_is_finished_at_sign_in_and_matches_a_signed_in_one(client):
    signed_out, dev_a = person("late")
    signed_in, _ = person("online")
    for who in (signed_out, signed_in):
        teach(who, "chai point", "Entertainment")                        # a learned rule: sealed, so it waits for sign-in
    assert post(client, dev_a["token"], OTHER_SMS).json() == {"status": "added"}
    with as_user(signed_in):
        assert tools.ingest_sms(None, "AX-HDFCBK", OTHER_SMS, None)["status"] == "added"

    [early] = raw(signed_out, "SELECT * FROM transactions WHERE source = 'sms'")
    assert early["unlocked"] is False and early["payee"].startswith("rs1.") and early["raw_text"].startswith("rs1.")
    assert early["amount"] == Decimal("40.00") and early["txn_date"].isoformat() == "2026-10-20"      # plain: alerts need them
    with as_user(locked(signed_out)):                                                               # no data key here
        assert tools.list_transactions(search="chai") == []                                         # no key, no text, nothing found
        row = [t for t in tools.list_transactions() if t["source"] == "sms"][0]
        assert row["payee"] is None and row["category"] != "Entertainment"                          # the learned rule has not run

    hooks.after_sign_in(signed_out)                                                               # sign-in

    def view(who):
        with as_user(who):
            t = [t for t in tools.list_transactions() if t["source"] == "sms"][0]
        r = raw(who, "SELECT merchant_key, unlocked, payee, raw_text, upi_ref_sealed FROM transactions WHERE source = 'sms'")[0]
        assert r["unlocked"] is True and r["payee"].startswith("rx1.") and r["raw_text"].startswith("rx1.")
        return {k: t[k] for k in ("amount", "direction", "txn_date", "payee", "description", "category", "category_source",
                                  "category_confidence", "category_reason", "suggested_category", "source", "account")} | {"mk": r["merchant_key"]}

    assert view(signed_out) == view(signed_in)
    assert view(signed_out)["category"] == "Entertainment" and view(signed_out)["category_source"] == "learned"
    hooks.after_sign_in(signed_out)                                                               # a second run changes nothing
    assert view(signed_out) == view(signed_in)


def test_the_sign_in_step_never_touches_a_category_the_person_chose(client):
    friend, device = person("friend")
    teach(friend, "chai point", "Entertainment")
    assert post(client, device["token"], OTHER_SMS).json() == {"status": "added"}
    with as_user(friend), db.connect() as conn:                                  # the person corrected it in another session
        food = conn.execute("SELECT id FROM categories WHERE name = 'Groceries'").fetchone()["id"]
        conn.execute("UPDATE transactions SET category_id = %s, category_source = 'user', category_confidence = 100 "
                     "WHERE source = 'sms'", (food,))
    hooks.after_sign_in(friend)
    with as_user(friend):
        t = [t for t in tools.list_transactions() if t["source"] == "sms"][0]
    assert (t["category"], t["category_source"]) == ("Groceries", "user")
    assert raw(friend, "SELECT unlocked FROM transactions WHERE source = 'sms'")[0]["unlocked"] is True


def test_a_review_item_is_opened_at_sign_in(client):
    friend, device = person("friend")
    assert post(client, device["token"], "Rs 500.00 transferred from A/c XX1234 to your wallet").json() == {"status": "needs_review"}
    hooks.after_sign_in(friend)
    [row] = raw(friend, "SELECT sender, raw_text FROM sms_inbox")
    assert row["raw_text"].startswith("rx1.") and row["sender"].startswith("rx1.")
    with as_user(friend):
        assert tools.list_inbox()[0]["raw_text"] == "Rs 500.00 transferred from A/c XX1234 to your wallet"


def test_signed_out_brand_sms_carries_its_merchant_key_for_alerts(client):
    friend, device = person("friend")
    post(client, device["token"], BANK_SMS)
    [r] = raw(friend, "SELECT merchant_key, unlocked FROM transactions")
    assert (r["merchant_key"], r["unlocked"]) == ("swiggy", False)


def test_a_device_token_writes_only_as_its_own_owner(client):
    a, dev_a = person("a")
    b, dev_b = person("b")
    with as_user(a):
        found = tools.authenticate_device(dev_a["token"])
    assert found["uid"] == a.uid and found["ctx"].uid == a.uid and found["ctx"].key is None and found["ctx"].strict is False
    post(client, dev_a["token"], BANK_SMS)
    assert count("transactions", a) == 1 and count("transactions", b) == 0
    with as_user(b), pytest.raises(tools.RectifiError, match="not paired"):             # A's phone id used while bound as B
        tools.ingest_sms(dev_a["id"], "AX-HDFCBK", OTHER_SMS, None)
    assert count("transactions", a) == 1 and count("transactions", b) == 0
    assert tools.authenticate_device("rf_wrong") is None


def test_the_gateway_still_answers_with_the_same_status_codes(client):
    friend, device = person("friend")
    assert client.post("/sms", headers={"Authorization": "Bearer rf_nope"}, json={"text": "x"}).status_code == 401
    assert post(client, device["token"], "123456 is your OTP for login. Do not share it. -HDFC Bank").json() == {"status": "ignored"}
    assert post(client, device["token"], BANK_SMS).status_code == 200


# ───────────────────────── search, rules, caches ─────────────────────────

def test_ledger_search_runs_in_python_over_sealed_text_and_reads_the_newest_rows(monkeypatch):
    friend, _ = person("friend")
    with as_user(friend):
        tools.add_transaction("11", "debit", "2026-10-01", payee="Alpha Stores", note="birthday gift")
        tools.add_transaction("22", "debit", "2026-10-02", payee="Beta", description="UPI/ALPHA/9")
        tools.add_transaction("33", "debit", "2026-10-03", payee="Gamma")
        found = tools.list_transactions(search="alpha")
        assert sorted(t["amount"] for t in found) == [Decimal("11.00"), Decimal("22.00")]
        assert [t["amount"] for t in tools.list_transactions(search="BIRTHDAY")] == [Decimal("11.00")]
        assert tools.list_transactions(search="nothing like this") == []
        assert len(tools.list_transactions(search="a", limit=2)) == 2
        monkeypatch.setattr(tools, "SEARCH_WINDOW", 2)                      # only the newest 2 rows are read: Alpha (oldest) is out
        assert [t["amount"] for t in tools.list_transactions(search="alpha")] == [Decimal("22.00")]


def test_learned_rules_are_sealed_ordered_in_python_and_apply():
    friend, _ = person("friend")
    teach(friend, "chai", "Entertainment")
    teach(friend, "chai point", "Groceries")
    with as_user(friend), db.connect() as conn:
        ctx = categorize.load_context(conn, with_index=False)
        learned = [p for p, _, source in ctx.rules if source == "learned"]
        assert learned == ["chai point", "chai"]                              # longest first
        assert ctx.rules[0][2] == "learned" and ctx.rules[-1][2] == "default"
        assert categorize.decide_with(ctx, "chai point", None, "debit").category == "Groceries"
    assert all(is_sealed(r["pattern"]) for r in raw(friend, "SELECT pattern FROM merchant_rules WHERE source = 'learned'"))
    assert any(r["pattern"] == "swiggy" for r in raw(friend, "SELECT pattern FROM merchant_rules WHERE source = 'default'"))   # shipped rules stay plain


def test_a_signed_out_context_skips_learned_rules_instead_of_failing(client):
    friend, _ = person("friend")
    teach(friend, "chai point", "Entertainment")
    with as_user(locked(friend)), db.connect() as conn:
        ctx = categorize.load_context(conn, with_index=False)
    assert all(source != "learned" for _, _, source in ctx.rules) and ctx.rules


def test_the_lookalike_index_is_cached_per_person():
    a, _ = person("a")
    b, _ = person("b")
    for who, payee, category in ((a, "chaayos", "Entertainment"), (b, "chaayos", "Groceries")):
        with as_user(who):
            tools.add_transaction("5", "debit", "2026-10-01", payee=payee, category=category)
    for who in (a, b):
        with as_user(who), db.connect() as conn:
            categorize.get_index(conn)
    assert {a.uid, b.uid} <= {key[0] for key in categorize._index_cache}
    with as_user(a), db.connect() as conn:
        assert {r["category"] for r in categorize.get_index(conn).rows} == {"Entertainment"}
    with as_user(b), db.connect() as conn:
        assert {r["category"] for r in categorize.get_index(conn).rows} == {"Groceries"}


def test_insights_work_on_sealed_ledgers():
    from rectifi import insights
    friend, _ = person("friend")
    with as_user(friend):
        for month in ("2026-07", "2026-08", "2026-09", "2026-10"):
            tools.add_transaction("499", "debit", f"{month}-05", payee="Streamly Plus")
        found = insights.insights(today=tools.resolve_date("2026-10-20"))
    recurring = [i for i in found if i.kind == "recurring"]                  # found from payee names that were sealed in the table
    assert recurring and "499" in recurring[0].title


# ───────────────────────── uploads ─────────────────────────

def test_uploads_and_recent_imports_are_per_person():
    from rectifi.api import uploads
    from rectifi.api.errors import ApiError
    uploads.reset()
    a, _ = person("a")
    b, _ = person("b")
    with as_user(a):
        upload_id = uploads.pending.put({"table": "A's"})
        uploads.recent.put("digest", {"batch_id": 1})
        assert uploads.pending.get(upload_id) == {"table": "A's"}
    with as_user(b):
        with pytest.raises(ApiError) as caught:
            uploads.pending.get(upload_id)
        assert caught.value.status == 404
        uploads.pending.drop(upload_id)                                       # B cannot drop A's upload either
        assert uploads.recent.get("digest") is None
        for _ in range(uploads.PENDING_MAX + 2):                              # B filling their own quota does not push A out
            uploads.pending.put({})
        assert len(uploads.pending) == uploads.PENDING_MAX
    with as_user(a):
        assert uploads.pending.get(upload_id) == {"table": "A's"} and uploads.recent.get("digest") == {"batch_id": 1}
    uploads.reset()


def test_a_friends_import_starts_no_model_job_and_the_owners_does(monkeypatch):
    from rectifi.agent import jobs
    from rectifi.api.routes import imports as route

    queued = []
    monkeypatch.setattr(jobs, "queue_for_batch", lambda batch_id: queued.append(batch_id))
    owner = make_user("owner", uid=1, encrypted=True)
    friend = make_user("friend", encrypted=True)
    with as_user(friend):
        tools.add_account("HDFC", "4417")
        result = importer.import_file(CSV, "s.csv", "HDFC")
        route.start_sorting(result.batch_id)
    assert queued == []
    assert {r["classify_state"] for r in raw(friend, "SELECT classify_state FROM transactions")} == {None}     # never waiting for a model
    with as_user(owner):
        tools.add_account("HDFC", "4417")
        result = importer.import_file(CSV, "s.csv", "HDFC")
        route.start_sorting(result.batch_id)
    assert queued == [result.batch_id]
