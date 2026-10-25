"""Statement upload over HTTP: auto-import, already-imported, manual preview and commit, undo, PDF passwords, size limits."""

from pathlib import Path

import pytest
from api_support import make_client

from rectifi import tools
from rectifi.api import uploads

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CSV = FIXTURES / "hdfc_style_statement.csv"
ENCRYPTED = FIXTURES / "statement_encrypted.pdf"
PASSWORD = "demo1234"


@pytest.fixture(autouse=True)
def clean_uploads():
    uploads.reset()
    yield
    uploads.reset()


@pytest.fixture
def client():
    return make_client()


def upload(client, path=CSV, data=None, name=None, **fields):
    content = data if data is not None else path.read_bytes()
    return client.post("/api/imports", files={"file": (name or path.name, content)}, data=fields)


def test_a_clear_csv_imports_at_once_with_the_balance_check(client):
    acct = tools.add_account("HDFC Savings", "4417")["id"]
    r = upload(client)
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"] == "imported" and body["how"] == "digits"
    batch = body["batch"]
    assert (batch["added"], batch["merged"], batch["duplicates"], batch["needs_review"]) == (5, 0, 0, 0)
    assert batch["account"] == "HDFC Savings" and batch["account_id"] == acct and batch["undone_at"] is None
    assert body["needs_review_lines"] == [] and body["reconcile_reason"] is None
    assert body["reconcile"]["balanced"] is True and body["reconcile"]["gap"] == "0.00"
    assert body["reconcile"]["closing"] == "16205.50" and body["reconcile"]["opening"] == "12000.00"
    assert len(tools.list_transactions()) == 5


def test_the_same_file_again_is_already_imported_and_adds_nothing(client):
    tools.add_account("HDFC Savings", "4417")
    first = upload(client).json()
    again = upload(client).json()
    assert again["outcome"] == "already_imported" and again["batch"]["id"] == first["batch"]["id"] and again["how"] == "digits"
    assert len(tools.list_transactions()) == 5
    with __import__("rectifi.db", fromlist=["connect"]).connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM import_batches").fetchone()["n"] == 1


def test_after_an_undo_the_same_file_can_be_imported_again(client):
    tools.add_account("HDFC Savings", "4417")
    first = upload(client).json()
    undo = client.post(f"/api/imports/{first['batch']['id']}/undo").json()
    assert undo["removed"] == 5 and undo["already_undone"] is False and undo["batch"]["undone_at"] is not None
    assert tools.list_transactions() == []
    assert client.post(f"/api/imports/{first['batch']['id']}/undo").json()["already_undone"] is True
    again = upload(client).json()
    assert again["outcome"] == "imported" and again["batch"]["added"] == 5 and again["batch"]["id"] != first["batch"]["id"]


def test_get_batch(client):
    tools.add_account("HDFC Savings", "4417")
    batch_id = upload(client).json()["batch"]["id"]
    got = client.get(f"/api/imports/{batch_id}").json()
    assert got["batch"]["id"] == batch_id and got["batch"]["rows_left"] == 5 and got["reconcile"]["balanced"] is True
    assert client.get("/api/imports/9999").status_code == 404
    assert client.post("/api/imports/9999/undo").status_code == 422


def test_statement_for_other_digits_goes_to_a_preview_not_a_guess(client):
    tools.add_account("SBI Savings", "9999")
    body = upload(client).json()
    assert body["outcome"] == "preview" and "ending 4417" in body["account_reason"]
    assert tools.list_transactions() == []


def test_no_account_yet_gives_a_preview_that_says_so(client):
    body = upload(client).json()
    assert body["outcome"] == "preview" and "no account yet" in body["account_reason"].lower()


def test_manual_preview_commit_then_undo(client):
    acct = tools.add_account("HDFC Savings", "4417")["id"]
    body = upload(client, manual="1").json()
    assert body["outcome"] == "preview" and body["upload_id"] and body["filename"] == "hdfc_style_statement.csv"
    assert "check the columns" in body["reasons"][0]
    assert body["mapping"]["date"] == "txn date" or body["mapping"]["date"] == "date"
    assert "narration" in body["columns"]
    pv = body["preview"]
    assert pv["totals"] == {"count": 5, "money_out": "3794.50", "money_in": "8000.00", "first": "2026-10-01", "last": "2026-10-12"}
    assert len(pv["rows"]) == 5 and pv["rows"][0]["amount"] == "250.00" and pv["rows"][0]["direction"] == "debit"
    assert pv["rows"][0]["balance"] == "11750.00" and pv["review_rows"] == [] and pv["suggested_account_id"] == acct
    assert tools.list_transactions() == []                                      # a preview imports nothing

    again = client.post("/api/imports/preview", json={"upload_id": body["upload_id"], "mapping": body["mapping"]})
    assert again.status_code == 200 and again.json()["preview"]["totals"]["count"] == 5
    bad = client.post("/api/imports/preview", json={"upload_id": body["upload_id"], "mapping": {**body["mapping"], "date": None}})
    assert bad.status_code == 422 and "Date and Narration" in bad.json()["error"]["message"]
    nope = client.post("/api/imports/preview", json={"upload_id": body["upload_id"], "mapping": {**body["mapping"], "date": "no such column"}})
    assert nope.status_code == 422 and nope.json()["error"]["field"] == "mapping.date"

    done = client.post("/api/imports/commit", json={"upload_id": body["upload_id"], "mapping": body["mapping"], "account_id": acct})
    assert done.status_code == 200
    out = done.json()
    assert out["outcome"] == "imported" and out["how"] == "chosen" and out["batch"]["added"] == 5 and out["reconcile"]["balanced"] is True
    assert client.post("/api/imports/commit", json={"upload_id": body["upload_id"], "mapping": body["mapping"], "account_id": acct}).status_code == 409
    undo = client.post(f"/api/imports/{out['batch']['id']}/undo").json()
    assert undo["removed"] == 5 and tools.list_transactions() == []


def test_commit_validates_the_account(client):
    tools.add_account("HDFC Savings", "4417")
    body = upload(client, manual="1").json()
    r = client.post("/api/imports/commit", json={"upload_id": body["upload_id"], "mapping": body["mapping"], "account_id": 4242})
    assert r.status_code == 422 and r.json()["error"]["field"] == "account_id"


def test_unclear_columns_show_why(client):
    tools.add_account("Savings")
    csv = b"Date,Narration,Amount\n01/10/26,chai,20.00\n02/10/26,pay,500.00\n"
    body = upload(client, data=csv, name="odd.csv").json()
    assert body["outcome"] == "preview" and any("can't tell money in from money out" in x for x in body["reasons"])


def test_pdf_password_flow_and_the_password_is_never_kept_or_logged(client, caplog):
    tools.add_account("Savings", "4417")
    with caplog.at_level("DEBUG"):
        r = upload(client, ENCRYPTED)
        assert r.status_code == 422 and r.json()["error"]["code"] == "pdf_password_needed"
        r = upload(client, ENCRYPTED, password="not-the-password")
        assert r.status_code == 422 and r.json()["error"]["code"] == "pdf_password_wrong"
        assert "not-the-password" not in r.text
        r = upload(client, ENCRYPTED, password=PASSWORD)
        assert r.status_code == 200 and r.json()["outcome"] == "imported" and r.json()["batch"]["added"] == 6
        assert r.json()["reconcile"]["balanced"] is True
    assert PASSWORD not in caplog.text and "not-the-password" not in caplog.text
    assert PASSWORD not in repr(uploads.pending._items) and PASSWORD not in repr(uploads.recent._items)
    assert PASSWORD not in r.text


def test_a_file_over_10_mb_is_413(client):
    tools.add_account("Savings", "4417")
    limit = 10 * 1024 * 1024
    just_over = upload(client, data=b"x" * (limit + 1), name="big.csv")
    assert just_over.status_code == 413
    r = just_over
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large" and "10 MB" in r.json()["error"]["message"]


def test_wrong_kinds_of_requests(client):
    assert client.post("/api/imports", json={"a": 1}).status_code == 415
    r = client.post("/api/imports", files={"file": ("notes.txt", b"hello")})
    assert r.status_code == 415 and r.json()["error"]["field"] == "file"
    r = client.post("/api/imports", files={"other": ("a.csv", b"x")})
    assert r.status_code == 422 and r.json()["error"]["field"] == "file"


def test_an_unreadable_file_is_a_plain_422(client):
    r = upload(client, data=b"just some words\nno table here\n", name="junk.csv")
    assert r.status_code == 422 and "header row" in r.json()["error"]["message"]


def test_pending_upload_expires_after_15_minutes(client):
    tools.add_account("HDFC Savings", "4417")
    now = [1000.0]
    uploads.pending.clock = lambda: now[0]
    body = upload(client, manual="1").json()
    payload = {"upload_id": body["upload_id"], "mapping": body["mapping"]}
    now[0] += 14 * 60
    assert client.post("/api/imports/preview", json=payload).status_code == 200
    now[0] += 2 * 60
    r = client.post("/api/imports/preview", json=payload)
    assert r.status_code == 409 and r.json()["error"]["code"] == "upload_expired"


def test_at_most_five_pending_uploads_are_kept(client):
    tools.add_account("HDFC Savings", "4417")
    ids = [upload(client, manual="1").json()["upload_id"] for _ in range(7)]
    assert len(uploads.pending) == 5
    mapping = {"date": "txn date"}
    first = client.post("/api/imports/preview", json={"upload_id": ids[0], "mapping": mapping})
    assert first.status_code == 409
    assert client.post("/api/imports/preview", json={"upload_id": ids[-1], "mapping": mapping}).status_code != 409


def test_pending_holds_only_the_table_not_the_file_bytes(client):
    tools.add_account("HDFC Savings", "4417")
    upload(client, manual="1")
    (_, payload), = uploads.pending._items.values()
    assert set(payload) == {"table", "check", "filename", "hash"}
    assert not any(isinstance(v, (bytes, bytearray)) for v in payload.values())


def test_the_limit_sits_exactly_at_10_mb(client, monkeypatch):
    from rectifi.api.routes import imports as route
    from rectifi.api import security

    assert route.UPLOAD_LIMIT == security.UPLOAD_LIMIT == 10 * 1024 * 1024
    monkeypatch.setattr(route, "UPLOAD_LIMIT", 600)
    monkeypatch.setattr(security, "UPLOAD_LIMIT", 600)
    tools.add_account("Savings", "4417")
    assert upload(client, data=b"Date,Narration\n" + b"x" * 585, name="a.csv").status_code != 413        # exactly 600 bytes
    assert upload(client, data=b"Date,Narration\n" + b"x" * 586, name="a.csv").status_code == 413         # 601
