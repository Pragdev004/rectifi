"""Statement upload imports at once, shows a result card, and can be undone.

Covers: the auto-import path, undo removing only that batch, an unclear file falling back to the preview, a Streamlit
rerun not importing twice, review rows staying out, and which account the rows go into.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from streamlit.util import calc_hash
from test_app import APP, page_text

from rectifi import importer, tools
from rectifi.db import connect
from rectifi.importer import StatementRow, assess_statement, import_rows, read_table
from rectifi.pdf_statement import table_from_text
from rectifi.tools import RectifiError

D = Decimal
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hdfc_style_statement.csv"
CSV = FIXTURE.read_text(encoding="utf-8")


def table_of(text: str, name: str = "s.csv"):
    return read_table(text.encode(), name)


def batches() -> list[dict]:
    with connect() as conn:
        return conn.execute("SELECT * FROM import_batches ORDER BY id").fetchall()


def count(sql: str = "SELECT COUNT(*) AS n FROM transactions") -> int:
    with connect() as conn:
        return conn.execute(sql).fetchone()["n"]


def simple_csv(rows: str, header: str = "Date,Narration,Amount", letterhead: str = "") -> str:
    return f"{letterhead}{header}\n{rows}\n"


# ───────────────────────── how sure is Rectifi about the columns? ─────────────────────────

def test_a_clear_statement_is_confident_and_the_letterhead_names_the_account():
    check = assess_statement(table_of(CSV))
    assert check.confident and check.reasons == []
    assert check.account_last4 == "4417"
    assert len(check.rows) == 5


@pytest.mark.parametrize("text, why", [
    ("Date,Narration,Debit\n01/10/26,chai,20.00\n", "only a Debit or only a Credit"),
    ("Date,Narration,Amount\n01/10/26,chai,20.00\n02/10/26,pay,500.00\n", "can't tell money in from money out"),
    ("Date,Narration,Amount,Type\n01/10/26,chai,20.00,UPI\n02/10/26,pay,500.00,NEFT\n", "Type column"),
    ("Date,Narration,Amount\n03/25/26,chai,-20.00\n", "month-first"),
    ("Date,Narration,Amount\nsoon,chai,-20.00\nlater,pay,-5.00\n", "No transactions"),
], ids=["only debit", "unsigned amount", "type is not dr/cr", "month first", "no readable dates"])
def test_an_unclear_statement_is_not_confident_and_says_why(text, why):
    check = assess_statement(table_of(text))
    assert not check.confident
    assert any(why in reason for reason in check.reasons), check.reasons


@pytest.mark.parametrize("text", [
    "Date,Narration,Amount\n01/10/26,chai,-20.00\n02/10/26,stipend,8000.00\n",                       # minus signs say which way
    "Date,Narration,Amount,Dr/Cr\n01/10/26,chai,20.00,DR\n02/10/26,stipend,8000.00,CR\n",             # a Dr/Cr column says it
    "Date,Narration,Debit,Credit\n01/10/26,chai,20.00,\n02/10/26,stipend,,8000.00\n",
], ids=["signed", "dr/cr column", "debit and credit"])
def test_these_ways_of_showing_money_in_and_out_are_clear(text):
    assert assess_statement(table_of(text)).confident


@pytest.mark.parametrize("columns, why", [(["date", "notes", "amount"], "description"), (["when", "narration", "amount"], "date")])
def test_missing_date_or_description_column_is_not_confident(columns, why):
    check = assess_statement(pd.DataFrame([["01/10/26", "chai", "-20.00"]], columns=columns))
    assert not check.confident and any(why in r for r in check.reasons)


def test_a_few_footer_lines_are_fine_but_a_mostly_unreadable_file_is_not():
    body = "\n".join(f"{d:02d}/10/26,pay {d},-{d}.00" for d in range(1, 16))
    ok = assess_statement(table_of(simple_csv(body + "\nTotal,,\nThis is a computer generated statement,,")))
    assert ok.confident and len(ok.problems) == 2
    garbage = "\n".join(f"x{d},pay {d},-{d}.00" for d in range(1, 10)) + "\n01/10/26,real,-1.00"
    assert not assess_statement(table_of(simple_csv(garbage))).confident


def test_a_pdf_where_every_row_needs_review_is_not_confident():
    table, _ = table_from_text([["01/09/2026 MYSTERY 250.00 24,750.00", "02/09/2026 OTHER 99.00"]])
    check = assess_statement(table)
    assert not check.confident and any("Every row needs review" in r for r in check.reasons)


@pytest.mark.parametrize("lines, last4", [
    (["HDFC BANK", "Account No : XXXXXXXX4417"], "4417"),
    (["Statement for A/c No. 50100123456789"], "6789"),
    (["Account Number: 1234 5678 9012"], "9012"),
    (["Customer ID 12345678"], None),
    (["A/C XX1111", "A/C XX2222"], None),                       # two different accounts: do not pick one
    ([], None),
])
def test_account_digits_are_read_from_the_letterhead(lines, last4):
    assert importer._last4_from_lines(lines) == last4


# ───────────────────────── which account? ─────────────────────────

def test_the_statement_digits_choose_the_account_even_when_another_was_used_last():
    tools.add_account("Other bank", "9999")
    tools.add_account("HDFC Savings", "4417")
    tools.add_transaction("10", "debit", date.today(), payee="x", account="Other bank")        # most recently used
    pick = tools.account_for_statement("4417")
    assert pick["account"]["name"] == "HDFC Savings" and pick["by_digits"]


def test_digits_that_match_nobody_are_refused_when_that_could_be_the_wrong_bank():
    tools.add_account("Other bank", "9999")
    pick = tools.account_for_statement("4417")
    assert pick["account"] is None and "4417" in pick["reason"]


def test_digits_that_match_nobody_are_accepted_when_the_only_account_has_no_digits_saved():
    tools.add_account("Savings")
    assert tools.account_for_statement("4417")["account"]["name"] == "Savings"


def test_no_digits_means_the_default_account_and_no_accounts_means_none():
    assert tools.account_for_statement(None)["account"] is None
    tools.add_account("A")
    tools.add_account("B")
    tools.add_transaction("10", "debit", date.today(), payee="x", account="B")
    assert tools.account_for_statement(None)["account"]["name"] == "B"


# ───────────────────────── batches and undo ─────────────────────────

def rows_of(text: str):
    return assess_statement(table_of(text)).rows


def test_every_import_is_a_batch_and_only_new_rows_carry_its_id():
    tools.add_account("Savings", "4417")
    tools.add_transaction("250", "debit", date(2026, 10, 1), payee="chai", account="Savings", source="manual")   # will merge
    result = import_rows(rows_of(CSV), "Savings", "oct.csv")
    assert (result.added, result.merged, result.duplicates) == (4, 1, 0)
    (batch,) = batches()
    assert batch["id"] == result.batch_id and batch["filename"] == "oct.csv" and batch["account_id"] == result.account_id
    assert (batch["added"], batch["merged"], batch["duplicates"], batch["needs_review"]) == (4, 1, 0, 0)
    assert count("SELECT COUNT(*) AS n FROM transactions WHERE import_batch_id IS NOT NULL") == 4
    assert count("SELECT COUNT(*) AS n FROM transactions WHERE import_batch_id IS NULL") == 1       # the merged hand entry


def test_undo_removes_exactly_the_rows_that_import_added():
    tools.add_account("Savings", "4417")
    mine = tools.add_transaction("250", "debit", date(2026, 10, 1), payee="chai", account="Savings", source="manual")["transaction"]
    sms = tools.add_transaction("77", "debit", date(2026, 10, 3), payee="bus", account="Savings", source="sms")["transaction"]
    first = import_rows(rows_of(CSV), "Savings", "oct.csv")                       # added 4, merged the 250
    later = import_rows([StatementRow(date(2026, 10, 20), D("500.00"), "debit", "NEW RENT", None, None, 1)], "Savings", "late.csv")
    again = import_rows(rows_of(CSV), "Savings", "oct-again.csv")                 # everything already recorded
    assert (again.added, again.merged, again.duplicates) == (0, 0, 5)

    undone = tools.undo_import(first.batch_id)
    assert undone["removed"] == 4 and not undone["already_undone"]
    left = {t["id"]: t for t in tools.list_transactions(limit=50)}
    assert set(left) == {mine["id"], sms["id"], later_id(later)}                  # hand entry (merged), SMS, other batch survive
    assert left[mine["id"]]["description"] and left[mine["id"]]["payee"] == "chai"   # merged row keeps what the merge gave it
    assert tools.get_import_batch(first.batch_id)["undone_at"] is not None
    assert tools.get_import_batch(later.batch_id)["rows_left"] == 1
    assert tools.get_import_batch(again.batch_id)["added"] == 0


def later_id(result) -> int:
    with connect() as conn:
        return conn.execute("SELECT id FROM transactions WHERE import_batch_id = %s", (result.batch_id,)).fetchone()["id"]


def test_undo_twice_does_nothing_the_second_time():
    tools.add_account("Savings", "4417")
    batch = import_rows(rows_of(CSV), "Savings", "oct.csv").batch_id
    assert tools.undo_import(batch)["removed"] == 5
    assert tools.undo_import(batch) == {"removed": 0, "snapshots_restored": 0, "already_undone": True}
    with pytest.raises(RectifiError):
        tools.undo_import(9999)


def test_undo_takes_back_the_balance_snapshots_the_import_wrote():
    tools.add_account("Savings", "4417")
    tools.record_balance("Savings", date(2026, 10, 5), "18509.50")                # typed by hand; the import will replace it
    batch = import_rows(rows_of(CSV), "Savings", "oct.csv").batch_id
    before = {str(s["as_of"]): (s["balance"], s["source"]) for s in tools.list_snapshots("Savings")}
    assert before["2026-10-12"] == (D("16205.50"), "statement") and before["2026-09-30"] == (D("12000.00"), "statement")
    out = tools.undo_import(batch)
    assert out["snapshots_restored"] == 2
    assert [(str(s["as_of"]), s["balance"], s["source"]) for s in tools.list_snapshots("Savings")] == [
        ("2026-10-05", D("18509.50"), "manual")]


def test_undo_leaves_a_snapshot_alone_if_it_changed_afterwards():
    tools.add_account("Savings", "4417")
    batch = import_rows(rows_of(CSV), "Savings", "oct.csv").batch_id
    tools.record_balance("Savings", date(2026, 10, 12), "16000")                  # you corrected it by hand after the import
    out = tools.undo_import(batch)
    assert out["snapshots_restored"] == 1
    assert [(str(s["as_of"]), s["balance"]) for s in tools.list_snapshots("Savings")] == [("2026-10-12", D("16000.00"))]


def test_review_rows_never_import_and_are_counted_on_the_batch():
    tools.add_account("Savings", "4417")
    table, _ = table_from_text([[
        "01/09/2026 MYSTERY 250.00 24,750.00",
        "02/09/2026 UPI-SWIGGY-swiggy@icici-ICIC0000103-429183746521-PAYMENT 100.00 24,650.00",
    ]])
    check = assess_statement(table)
    assert check.confident                      # one row is provable, so the file is worth importing; the other waits
    result = import_rows(check.rows, "Savings", "pdf.pdf")
    assert result.added == 1 and len(result.needs_review) == 1
    assert batches()[0]["needs_review"] == 1 and count() == 1


def test_importing_nothing_but_review_rows_makes_no_batch():
    tools.add_account("Savings", "4417")
    table, _ = table_from_text([["01/09/2026 MYSTERY 250.00 24,750.00"]])
    result = import_rows(assess_statement(table).rows, "Savings")
    assert result.batch_id is None and batches() == [] and count() == 0


# ───────────────────────── the Add page ─────────────────────────

class FakeUpload:
    def __init__(self, name: str, data: bytes):
        self.name, self._data = name, data

    def getvalue(self) -> bytes:
        return self._data


def add_page(monkeypatch, text: str | None = CSV, name: str = "oct.csv", manual: bool = False) -> AppTest:
    import streamlit as st

    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: FakeUpload(name, text.encode()) if text is not None else None)
    at = AppTest.from_file(str(APP), default_timeout=30)
    at._page_hash = calc_hash("add")
    if manual:
        at.session_state["stmt_manual"] = True
    at.run()
    assert not at.exception
    return at


def button(at, label):
    return next(b for b in at.button if b.label.startswith(label))


def test_a_clear_statement_is_imported_at_once_with_a_result_card(monkeypatch):
    tools.add_account("HDFC Savings", "4417")
    at = add_page(monkeypatch)
    assert not any(b.label.startswith("Import ") for b in at.button)             # no separate Import click
    text = page_text(at)
    assert "Added as new entries" in text and "<td>5</td>" in text
    assert "Merged with entries you made by hand" in text and "Already recorded" in text and "Needs review" in text
    assert "HDFC Savings" in text and "matched by the account digits" in text
    assert "Balanced" in text                                                    # the reconciliation result
    assert button(at, "Undo this import")
    assert count() == 5 and count("SELECT COUNT(*) AS n FROM transactions WHERE import_batch_id IS NOT NULL") == 5
    assert tools.reconcile("HDFC Savings")["balanced"]


def test_a_statement_without_digits_goes_into_the_default_account(monkeypatch):
    tools.add_account("First")
    tools.add_account("Second")
    tools.add_transaction("10", "debit", date.today(), payee="x", account="Second")
    no_letterhead = CSV.replace("Account No : XXXXXXXX4417", "")
    at = add_page(monkeypatch, no_letterhead)
    assert tools.list_transactions(search="swiggy")[0]["account"] == "Second"
    assert "most recently used account" in page_text(at)


def test_the_statement_digits_beat_the_default_account(monkeypatch):
    tools.add_account("Other bank", "9999")
    tools.add_account("HDFC Savings", "4417")
    tools.add_transaction("10", "debit", date.today(), payee="x", account="Other bank")
    add_page(monkeypatch)
    assert {t["account"] for t in tools.list_transactions(search="swiggy")} == {"HDFC Savings"}


def test_undo_button_removes_the_batch_and_offers_to_import_again(monkeypatch):
    tools.add_account("HDFC Savings", "4417")
    tools.add_transaction("250", "debit", date(2026, 10, 1), payee="chai", account="HDFC Savings")    # merges, must survive
    at = add_page(monkeypatch)
    assert count() == 5
    button(at, "Undo this import").click().run()
    assert not at.exception
    assert "Import undone" in page_text(at)
    assert [t["payee"] for t in tools.list_transactions(limit=10)] == ["chai"]
    assert tools.list_snapshots("HDFC Savings") == []
    button(at, "Import this file again").click().run()                          # forgets the file, so the next run imports it again
    assert count() == 5 and len(batches()) == 2


def test_a_streamlit_rerun_does_not_import_the_same_file_twice(monkeypatch):
    tools.add_account("HDFC Savings", "4417")
    at = add_page(monkeypatch)
    for _ in range(3):
        at.run()
        assert not at.exception
    assert count() == 5 and len(batches()) == 1
    assert batches()[0]["duplicates"] == 0
    assert "Added as new entries" in page_text(at)                              # the card is still shown on every rerun


def test_an_unclear_statement_shows_the_preview_and_imports_nothing(monkeypatch):
    tools.add_account("Savings", "4417")
    unsigned = simple_csv("01/10/26,chai,20.00\n02/10/26,stipend,8000.00")
    at = add_page(monkeypatch, unsigned)
    assert count() == 0 and batches() == []
    assert "can't tell money in from money out" in page_text(at)
    assert "wasn't sure" in page_text(at) or "not sure" in page_text(at)
    assert [m.value for m in at.metric][0] == "2"                               # the manual preview is there
    assert button(at, "Import 2 rows")


def test_manual_mode_never_imports_by_itself(monkeypatch):
    tools.add_account("HDFC Savings", "4417")
    at = add_page(monkeypatch, manual=True)
    assert count() == 0
    button(at, "Import 5 rows").click().run()
    assert count() == 5 and "Added as new entries" in page_text(at)
    assert len(batches()) == 1


def test_a_statement_for_another_bank_is_not_imported_into_the_wrong_account(monkeypatch):
    tools.add_account("Other bank", "9999")
    at = add_page(monkeypatch)
    assert count() == 0
    assert "ending 4417" in page_text(at)
    assert button(at, "Import 5 rows")                                           # the person can still choose


def test_with_no_account_nothing_is_imported(monkeypatch):
    at = add_page(monkeypatch)
    assert count() == 0 and batches() == []
    assert "Add the bank account first" in page_text(at)


def test_review_rows_are_listed_on_the_card_and_not_imported(monkeypatch):
    tools.add_account("Savings", "4417")
    table, _ = table_from_text([[
        "01/09/2026 MYSTERY 250.00 24,750.00",
        "02/09/2026 UPI-SWIGGY-swiggy@icici-ICIC0000103-429183746521-PAYMENT 100.00 24,650.00",
        "03/09/2026 SALARY 1,000.00 25,650.00",
    ]])
    monkeypatch.setattr("rectifi.importer.read_table", lambda data, filename, password=None: table)
    at = add_page(monkeypatch)
    text = page_text(at)
    assert "1 row(s) were NOT imported" in text and "MYSTERY" in text
    assert count() == 2 and not any("MYSTERY" in (t["description"] or "") for t in tools.list_transactions(limit=10))


def test_importer_does_not_import_any_model_code():
    source = (Path(importer.__file__)).read_text(encoding="utf-8")
    assert "rectifi.agent" not in source and "langchain" not in source


def test_init_db_upgrades_a_database_made_before_import_batches_existed():
    """A user's real database already has transactions. Starting the new version must add the column and keep every row."""
    from rectifi import db

    tools.add_transaction(amount=D("120"), direction="debit", txn_date=date(2026, 9, 1), payee="chai", source="manual")
    with connect() as conn:                                   # put the database back to how the old version left it
        conn.execute("DROP INDEX IF EXISTS idx_txn_batch")
        conn.execute("ALTER TABLE transactions DROP COLUMN import_batch_id")
        conn.execute("DROP TABLE classify_jobs")                  # added later; it points at import_batches
        conn.execute("DROP TABLE import_batches")
    db.init_db()
    db.init_db()                                              # and running it again changes nothing
    with connect() as conn:
        cols = {r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'transactions'")}
        row = conn.execute("SELECT amount, import_batch_id FROM transactions").fetchone()
    assert "import_batch_id" in cols
    assert row["amount"] == D("120.00") and row["import_batch_id"] is None           # old row kept, not tied to any import
