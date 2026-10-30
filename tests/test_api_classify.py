"""The classifier over HTTP: what an import answers, the rows endpoint, the job, apply-similar, and that importing stays fast
and works with the model off."""

from pathlib import Path

import pytest
from api_support import make_client

from rectifi import tools
from rectifi.api import uploads

CSV = Path(__file__).resolve().parent / "fixtures" / "hdfc_style_statement.csv"


@pytest.fixture(autouse=True)
def clean_uploads():
    uploads.reset()
    yield
    uploads.reset()


@pytest.fixture
def model_off(monkeypatch):
    from rectifi.agent import llm

    monkeypatch.setattr(llm, "llm_status", lambda: (False, "Ollama isn't running."))


@pytest.fixture
def client():
    return make_client()


def statement_bytes() -> bytes:
    """The fixture statement, with the Swiggy payment turned into a payment to a person (nothing can place that)."""
    return CSV.read_bytes().replace(b"UPI-SWIGGY-swiggy@icici", b"UPI-RAHUL SHARMA-rahul@okaxis")


def upload(client):
    return client.post("/api/imports", files={"file": (CSV.name, statement_bytes())}).json()


def test_an_import_with_the_model_off_still_lands_and_names_the_sources(client, model_off):
    tools.add_account("HDFC Savings", "4417")
    body = upload(client)
    assert body["outcome"] == "imported" and body["batch"]["added"] == 5
    summary = body["categories"]
    assert summary["total"] == 5 and sum(summary["by_source"].values()) == 5
    assert summary["by_source"]["rule"] >= 1                       # uber
    assert summary["miscellaneous"] == 1 and summary["by_source"]["fallback"] == 1
    job = client.get(f"/api/classify/jobs/{body['classify_job_id']}").json()["job"]
    assert job["status"] == "no_model" and job["error"] == "Ollama isn't running." and job["total"] == 1


def test_the_rows_endpoint_shows_each_rows_source_and_filters(client, model_off):
    tools.add_account("HDFC Savings", "4417")
    body = upload(client)
    batch = body["batch"]["id"]
    rows = client.get(f"/api/imports/{batch}/rows").json()
    assert len(rows["items"]) == 5 and rows["show"] == "all"
    first = rows["items"][0]
    for field in ("category_source", "category_confidence", "category_reason", "suggested_category", "suggested_confidence"):
        assert field in first
    assert isinstance(first["category_confidence"], int)          # a whole percent, never a money string
    misc = client.get(f"/api/imports/{batch}/rows?show=misc").json()["items"]
    assert all(r["category"] == "Miscellaneous" for r in misc)
    assert client.get(f"/api/imports/{batch}/rows?show=model").json()["items"] == []
    assert client.get(f"/api/imports/{batch}/rows?show=zzz").status_code == 422
    assert client.get("/api/imports/9999/rows").status_code == 404


def test_the_ledger_rows_carry_the_source_too(client, model_off):
    tools.add_account("HDFC Savings", "4417")
    upload(client)
    items = client.get("/api/transactions?period=all").json()["items"]
    assert {r["category_source"] for r in items} <= {"rule", "heuristic", "retrieval", "fallback", "learned", "user"}


def test_a_correction_is_yours_and_apply_similar_spreads_it(client, model_off):
    acct = tools.add_account("HDFC Savings", "4417")["id"]
    from datetime import date
    from decimal import Decimal

    from rectifi.importer import StatementRow, import_rows

    rows = [StatementRow(date(2026, 10, 1), Decimal(str(10 + i)), "debit", f"UPI/DR/55555555555{i}/MURUGAN AGENCIES/SBIN/Pay", None, None, i + 1)
            for i in range(3)]
    batch = import_rows(rows, acct, "m.csv").batch_id
    ids = [r["id"] for r in client.get(f"/api/imports/{batch}/rows").json()["items"]]
    patched = client.patch(f"/api/transactions/{ids[0]}", json={"category": "Shopping"}).json()
    assert patched["transaction"]["category_source"] == "user" and patched["transaction"]["category_confidence"] == 100
    assert patched["learned_rule"] == "murugan agencies"
    out = client.post(f"/api/transactions/{ids[0]}/apply-similar")
    assert out.status_code == 200 and out.json()["changed"] == 2      # the two look-alikes the machine had left in Miscellaneous
    assert client.post(f"/api/transactions/{ids[0]}/apply-similar").json()["changed"] == 0
    after = {r["id"]: r for r in client.get(f"/api/imports/{batch}/rows").json()["items"]}
    assert all(r["category"] == "Shopping" for r in after.values())
    assert client.post("/api/transactions/99999/apply-similar").status_code == 404


def test_start_a_job_when_the_model_is_off_says_so(client, model_off):
    tools.add_account("HDFC Savings", "4417")
    body = upload(client)
    batch = body["batch"]["id"]
    r = client.post("/api/classify/jobs", json={"batch_id": batch})
    assert r.status_code == 200
    data = r.json()
    assert data["job"]["status"] == "no_model" and data["waiting"] == 1
    assert client.post("/api/classify/jobs", json={"batch_id": 9999}).status_code == 404
    assert client.get("/api/classify/jobs/9999").status_code == 404


def test_undo_cancels_the_job_the_page_is_watching(client, model_off):
    tools.add_account("HDFC Savings", "4417")
    body = upload(client)
    assert client.post(f"/api/imports/{body['batch']['id']}/undo").status_code == 200
    assert client.get(f"/api/classify/jobs/{body['classify_job_id']}").json()["job"]["status"] == "cancelled"
