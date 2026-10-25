"""PDF bank statements: ruled tables over two pages, the text-line fallback, passwords, scans.

The PDFs in tests/fixtures are fake (made by tests/fixtures/make_pdf_fixtures.py). Amounts are checked as exact Decimals.
Every fixture has the same 24 transactions: opening balance 25,000.00, debits 17,334.90, credits 14,111.35,
closing balance 21,776.45 (the ruled one is 2 pages, with a header repeated on page 2, narrations wrapped inside
a cell and onto their own row, blank separator rows and a summary box that must be ignored).
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from rectifi import tools
from rectifi.importer import (StatementRow, detect_columns, import_file, import_rows, normalise, pdf_to_table,
                              read_table)
from rectifi.pdf_statement import NO_ROWS_MESSAGE, PdfContent, PdfPage, join_wrapped, table_from_text
from rectifi.tools import RectifiError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULED, TEXT_ONLY = FIXTURES / "statement_ruled.pdf", FIXTURES / "statement_text_only.pdf"
ENCRYPTED, IMAGE_ONLY = FIXTURES / "statement_encrypted.pdf", FIXTURES / "statement_image_only.pdf"
PASSWORD = "demo1234"

D = Decimal
DEBITS, CREDITS = D("17334.90"), D("14111.35")


def rows_of(path: Path, password=None):
    table = read_table(path.read_bytes(), path.name, password)
    rows, problems = normalise(table, detect_columns(table))
    assert problems == []
    return rows


def totals(rows):
    return (sum((r.amount for r in rows if r.direction == "debit"), D(0)),
            sum((r.amount for r in rows if r.direction == "credit"), D(0)))


@pytest.mark.parametrize("path", [RULED, TEXT_ONLY], ids=["ruled tables", "text lines"])
def test_both_readers_find_the_same_24_rows_with_exact_amounts_and_directions(path):
    rows = rows_of(path)
    assert len(rows) == 24
    assert totals(rows) == (DEBITS, CREDITS)
    by_date = {r.txn_date: r for r in rows}
    swiggy = rows[0]
    assert (swiggy.txn_date, swiggy.amount, swiggy.direction, swiggy.balance) == (date(2026, 9, 1), D("250.00"), "debit", D("24750.00"))
    stipend = by_date[date(2026, 9, 2)]
    assert (stipend.amount, stipend.direction, stipend.balance) == (D("8000.00"), "credit", D("32750.00"))
    refund = by_date[date(2026, 9, 10)]
    assert (refund.amount, refund.direction) == (D("649.00"), "credit")
    # the first row of page 2 (the header is printed again above it)
    assert (by_date[date(2026, 9, 16)].amount, by_date[date(2026, 9, 16)].direction) == (D("318.40"), "debit")
    last = rows[-1]
    assert (last.txn_date, last.amount, last.direction, last.balance) == (date(2026, 9, 30), D("112.35"), "credit", D("21776.45"))
    assert all(r.review is None for r in rows)
    assert [r.balance for r in rows][-1] == D("21776.45")


@pytest.mark.parametrize("path", [RULED, TEXT_ONLY], ids=["ruled tables", "text lines"])
def test_wrapped_narrations_are_put_back_together(path):
    rows = rows_of(path)
    narrations = {r.txn_date: r.narration for r in rows}
    # a reference cut in the middle of the number (two pieces) is glued back into one 12-digit number
    assert "UTIB0000100-429900112233-RIDE" in narrations[date(2026, 9, 3)]
    # words keep their space
    assert narrations[date(2026, 9, 4)] == "POS 416021XXXXXX4417 DMART AVENUE CHENNAI SOUTH BRANCH TAMIL NADU IN"
    # the second line of a narration, printed as its own row/line, is joined to the row above, not read as a payment
    assert narrations[date(2026, 9, 9)] == "ATM WDL KOTTAYAM VALAVOOR CARD XX4417 TXN ID 99812"


def test_ruled_pdf_ignores_blank_rows_repeated_header_and_summary_box():
    table = read_table(RULED.read_bytes(), RULED.name)
    assert len(table) == 24
    assert list(table["date"]).count("Date") == 0
    assert not table["narration"].str.contains("Opening Balance|Total Debits", regex=True).any()
    assert "" not in set(table["date"])


def test_join_wrapped_rules():
    assert join_wrapped("UPI-A-4299", "00112233-RIDE") == "UPI-A-429900112233-RIDE"       # number cut in two
    assert join_wrapped("UPI-A-4299001122", "-PAYMENT") == "UPI-A-4299001122-PAYMENT"     # starts with a hyphen
    assert join_wrapped("DMART AVENUE", "CHENNAI") == "DMART AVENUE CHENNAI"              # two words
    assert join_wrapped("", "ONLY") == "ONLY"


@pytest.mark.parametrize("path", [RULED, TEXT_ONLY], ids=["ruled tables", "text lines"])
def test_pdf_import_dedupes_and_reconciles_from_the_balance_column(path):
    tools.add_account("Savings", "4417")
    first = import_file(path.read_bytes(), path.name, "Savings")
    assert (first.added, first.merged, first.duplicates, first.needs_review) == (24, 0, 0, [])
    assert first.opening == (date(2026, 8, 31), D("25000.00"))
    assert first.closing == (date(2026, 9, 30), D("21776.45"))

    rec = tools.reconcile("Savings")
    assert rec["balanced"] and rec["gap"] == D("0.00")
    assert (rec["credits"], rec["debits"], rec["count"]) == (CREDITS, DEBITS, 24)
    assert rec["expected_closing"] == rec["closing"] == D("21776.45")

    # the same file again: nothing new (including the two identical-looking Swiggy rows on different days)
    again = import_file(path.read_bytes(), path.name, "Savings")
    assert (again.added, again.merged, again.duplicates) == (0, 0, 24)
    assert len(tools.list_transactions(limit=100)) == 24

    # UPI references are taken from the narration, including the one that was cut in two by the line wrap
    swiggy = tools.list_transactions(search="swiggy", limit=10)
    assert {t["upi_ref"] for t in swiggy} == {"429183746521", "430144455566", "430155566677"}
    assert tools.list_transactions(search="uber")[0]["upi_ref"] == "429900112233"
    assert all(t["source"] == "statement" for t in tools.list_transactions(limit=100))

    # and the arithmetic still catches a missing row
    netflix = tools.list_transactions(search="netflix")[0]
    tools.delete_transaction(netflix["id"])
    rec = tools.reconcile("Savings")
    assert not rec["balanced"] and rec["gap"] == -D("199.00")


@pytest.mark.skipif(date.today() < date(2026, 9, 30), reason="the fixture is dated September 2026")
def test_pdf_import_merges_with_an_entry_typed_by_hand():
    tools.add_account("Savings", "4417")
    by_hand = tools.add_transaction("1240.50", "debit", "2026-09-04", payee="uber", source="chat")["transaction"]
    result = import_file(RULED.read_bytes(), RULED.name, "Savings")
    assert (result.added, result.merged) == (23, 1)
    merged = tools.get_transaction(by_hand["id"])
    assert merged["upi_ref"] == "429900112233" and merged["txn_date"] == date(2026, 9, 3)
    assert tools.reconcile("Savings")["balanced"]


# ───────────────────────── passwords ─────────────────────────

def test_encrypted_pdf_without_a_password_asks_for_one():
    with pytest.raises(RectifiError) as err:
        read_table(ENCRYPTED.read_bytes(), ENCRYPTED.name)
    message = str(err.value)
    assert "password-protected" in message
    assert "If your bank's email gave a password for this PDF, enter it" in message


def test_encrypted_pdf_with_a_wrong_password_says_so_without_echoing_it():
    with pytest.raises(RectifiError) as err:
        read_table(ENCRYPTED.read_bytes(), ENCRYPTED.name, "not-the-password")
    assert "did not open the PDF" in str(err.value)
    assert "not-the-password" not in str(err.value)


def test_encrypted_pdf_with_the_right_password_imports_exact_amounts():
    tools.add_account("Savings", "4417")
    result = import_file(ENCRYPTED.read_bytes(), ENCRYPTED.name, "Savings", password=PASSWORD)
    assert (result.added, result.needs_review) == (6, [])
    expected = [  # first six rows of the statement
        (date(2026, 9, 1), D("250.00"), "debit"), (date(2026, 9, 2), D("8000.00"), "credit"),
        (date(2026, 9, 3), D("1240.50"), "debit"), (date(2026, 9, 4), D("2105.00"), "debit"),
        (date(2026, 9, 5), D("199.00"), "debit"), (date(2026, 9, 7), D("412.75"), "debit"),
    ]
    stored = sorted(tools.list_transactions(limit=20), key=lambda t: t["txn_date"])
    assert [(t["txn_date"], t["amount"], t["direction"]) for t in stored] == expected
    assert tools.reconcile("Savings")["balanced"]
    assert tools.reconcile("Savings")["closing"] == D("28792.75")


def test_the_password_is_never_stored_or_logged(caplog):
    tools.add_account("Savings", "4417")
    with caplog.at_level("DEBUG"):
        import_file(ENCRYPTED.read_bytes(), ENCRYPTED.name, "Savings", password=PASSWORD)
        with pytest.raises(RectifiError):
            read_table(ENCRYPTED.read_bytes(), ENCRYPTED.name, "wrong-pass-123")
    assert PASSWORD not in caplog.text and "wrong-pass-123" not in caplog.text
    from rectifi.db import connect

    with connect() as conn:
        for (table,) in [(r["tablename"],) for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()]:
            dump = " ".join(r["j"] for r in conn.execute(f'SELECT row_to_json(t)::text AS j FROM "{table}" t').fetchall())
            assert PASSWORD not in dump


# ───────────────────────── scans, junk, nothing found ─────────────────────────

def test_image_only_pdf_says_it_cannot_be_read_and_suggests_csv():
    with pytest.raises(RectifiError) as err:
        read_table(IMAGE_ONLY.read_bytes(), IMAGE_ONLY.name)
    message = str(err.value)
    assert "scan or a photo" in message and "can't read it" in message
    assert "CSV or Excel" in message


@pytest.mark.parametrize("data", [b"%PDF-1.4 this is not really a pdf", b"hello"], ids=["truncated pdf", "named .pdf only"])
def test_a_damaged_pdf_gives_a_friendly_error(data):
    with pytest.raises(RectifiError) as err:
        read_table(data, "statement.pdf")
    assert "couldn't read this file as a PDF" in str(err.value)


def test_a_pdf_with_text_but_no_transactions_says_so():
    content = PdfContent(pages=[PdfPage(tables=[], lines=["Dear customer", "Your account is safe with us", "Page 1 of 1"])])
    with pytest.raises(RectifiError) as err:
        pdf_to_table(content)
    assert str(err.value) == NO_ROWS_MESSAGE
    assert "CSV or Excel" in NO_ROWS_MESSAGE


# ───────────────────────── the text-line reader, rule by rule ─────────────────────────

def read_lines(*lines):
    table, problems = table_from_text([list(lines)])
    rows, more = normalise(table, detect_columns(table)) if len(table) else ([], [])
    return rows, problems + more


def test_direction_comes_from_how_the_balance_moved_not_from_the_narration():
    rows, _ = read_lines(
        "Opening Balance 1,000.00",
        "01/09/2026 UPI-DR-LOOKS-LIKE-A-DEBIT 100.00 1,100.00",     # narration says DR, the balance went UP: money in
        "02/09/2026 NEFT CR-LOOKS-LIKE-A-CREDIT 300.00 800.00",     # narration says CR, the balance went DOWN: money out
    )
    assert [(r.amount, r.direction) for r in rows] == [(D("100.00"), "credit"), (D("300.00"), "debit")]


def test_dr_cr_marks_decide_when_there_is_no_balance():
    rows, _ = read_lines("01/09/2026 CHAI 40.00 Dr", "02/09/2026 REFUND 55.50Cr", "03/09/2026 RENT 5,000.00 DR.")
    assert [(r.amount, r.direction, r.balance) for r in rows] == [
        (D("40.00"), "debit", None), (D("55.50"), "credit", None), (D("5000.00"), "debit", None)]


def test_a_row_that_cannot_be_proven_is_marked_for_review_not_guessed():
    rows, _ = read_lines(
        "01/09/2026 FIRST ROW NO OPENING BALANCE 250.00 24,750.00",     # nothing to compare with, no mark
        "02/09/2026 SECOND ROW 8,000.00 32,750.00",                      # now provable: money in
        "03/09/2026 NO BALANCE NO MARK 99.00",                           # nothing proves it
        "04/09/2026 FOURTH 100.00 32,000.00",                            # the chain was broken by the row above
        "05/09/2026 FIFTH 10.00 31,000.00",                              # 32,000 - 10 is not 31,000: a row is missing
    )
    assert [r.review is None for r in rows] == [False, True, False, False, False]
    assert rows[0].direction == "" and rows[0].amount == D("250.00")
    assert "no earlier balance" in rows[0].review
    assert rows[1].direction == "credit"
    assert "no balance on this row" in rows[2].review
    assert "no earlier balance" in rows[3].review
    assert "did not move" in rows[4].review
    # review rows keep their amount for display but carry no direction
    assert [r.amount for r in rows if r.review] == [D("250.00"), D("99.00"), D("100.00"), D("10.00")]


def test_the_words_opening_balance_inside_a_narration_are_not_an_opening_balance():
    rows, _ = read_lines("01/09/2026 REVERSAL OF OPENING BALANCE FEE 250.00 24,750.00")
    assert len(rows) == 1 and rows[0].amount == D("250.00")


def test_review_rows_are_not_imported_and_the_balance_check_shows_the_gap():
    tools.add_account("Savings", "4417")
    table, _ = table_from_text([[
        "01/09/2026 MYSTERY 250.00 24,750.00",
        "02/09/2026 UPI-SWIGGY-swiggy@icici-ICIC0000103-429183746521-PAYMENT 100.00 24,650.00",
        "03/09/2026 SALARY 1,000.00 25,650.00",
    ]])
    rows, _ = normalise(table, detect_columns(table))
    result = import_rows(rows, "Savings")
    assert result.added == 2 and len(result.needs_review) == 1
    assert "₹250.00" in result.needs_review[0] and "MYSTERY" in result.needs_review[0]
    assert [t["amount"] for t in tools.list_transactions(limit=10)] == [D("1000.00"), D("100.00")]
    # opening is worked out from the first row that could be imported (24,750 before the Swiggy debit of 100 = 24,750)
    rec = tools.reconcile("Savings")
    assert rec["balanced"] and rec["opening"] == D("24750.00")


def test_a_review_row_in_the_middle_of_the_window_shows_up_as_a_gap():
    tools.add_account("Savings", "4417")
    rows = [
        StatementRow(date(2026, 9, 1), D("100.00"), "debit", "A", D("900.00"), None, 1),
        StatementRow(date(2026, 9, 2), D("50.00"), "", "UNPROVEN", None, None, 2, review="x"),
        StatementRow(date(2026, 9, 3), D("20.00"), "debit", "C", D("830.00"), None, 3),     # the 50 left the account too
    ]
    import_rows(rows, "Savings")
    rec = tools.reconcile("Savings")
    assert not rec["balanced"] and rec["gap"] == -D("50.00")      # the bank shows 50 less than Rectifi expects


def test_newest_first_statements_are_turned_around():
    rows, _ = read_lines(
        "03/09/2026 C 20.00 830.00",
        "02/09/2026 B 50.00 850.00",
        "01/09/2026 A 100.00 900.00",
        "Opening Balance 1,000.00",
    )
    assert [(r.txn_date.day, r.amount, r.direction) for r in rows] == [(1, D("100.00"), "debit"), (2, D("50.00"), "debit"), (3, D("20.00"), "debit")]


def test_three_numbers_withdrawal_deposit_balance_with_a_zero_on_the_empty_side():
    rows, _ = read_lines("Opening Balance 100.00", "01/09/2026 X 0.00 50.00 150.00", "02/09/2026 Y 30.00 0.00 120.00")
    assert [(r.amount, r.direction) for r in rows] == [(D("50.00"), "credit"), (D("30.00"), "debit")]


def test_reference_numbers_and_dates_are_not_mistaken_for_money():
    rows, _ = read_lines("Opening Balance 500.00", "01/09/2026 CHQ 000123 CLEARING 01/09/2026 200.00 300.00")
    assert [(r.amount, r.narration, r.direction) for r in rows] == [(D("200.00"), "CHQ 000123 CLEARING", "debit")]


def test_footer_and_header_lines_are_not_glued_onto_a_narration():
    table, _ = table_from_text([
        ["DEMO BANK Statement of Account", "01/09/2026 CHAI 40.00 Dr", "Page 1 of 2", "This is a computer generated statement."],
        ["Date Narration Amount Balance", "02/09/2026 TEA 20.00 Dr"],
    ])
    assert list(table["narration"]) == ["CHAI", "TEA"]


# ───────────────────────── PDF content never reaches a model ─────────────────────────

def test_pdf_import_never_calls_a_language_model(monkeypatch):
    import rectifi.agent.graph as graph
    import rectifi.agent.llm as llm

    def boom(*a, **k):
        raise AssertionError("a PDF statement must never reach an LLM")

    monkeypatch.setattr(llm, "get_llm", boom)
    monkeypatch.setattr(graph, "get_llm", boom)
    tools.add_account("Savings", "4417")
    assert import_file(RULED.read_bytes(), RULED.name, "Savings").added == 24


def test_pdf_modules_do_not_import_any_model_code():
    for name in ("pdf_statement.py", "importer.py"):
        source = (Path(__file__).resolve().parent.parent / "rectifi" / name).read_text(encoding="utf-8")
        assert "rectifi.agent" not in source and "from .agent" not in source and "langchain" not in source


# ───────────────────────── the Add > Import statement tab ─────────────────────────

class FakeUpload:
    """What st.file_uploader returns (AppTest can't upload a file, so the test hands the app one)."""

    def __init__(self, path: Path):
        self.name, self._data = path.name, path.read_bytes()

    def getvalue(self) -> bytes:
        return self._data


def add_page_with_upload(monkeypatch, path: Path, manual: bool = False):
    """Open Add with this file 'uploaded'. A clear statement is now imported at once; manual=True ticks the
    "choose the columns and account myself" box first, which is the step-by-step preview with an Import button."""
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    from streamlit.util import calc_hash
    from test_app import APP

    monkeypatch.setattr(st, "file_uploader", lambda *a, **k: FakeUpload(path))
    at = AppTest.from_file(str(APP), default_timeout=30)
    at._page_hash = calc_hash("add")
    if manual:
        at.session_state["stmt_manual"] = True
    at.run()
    return at


def holds(value, needle: str) -> bool:
    """Is `needle` anywhere inside this value (strings, dicts, lists, tables)?"""
    import pandas as pd

    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(holds(k, needle) or holds(v, needle) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(holds(v, needle) for v in value)
    if isinstance(value, pd.DataFrame):
        return needle in value.to_csv()
    return False


def label_button(at, label):
    return next(b for b in at.button if b.label.startswith(label))


def test_add_page_accepts_pdf_and_imports_a_ruled_statement(monkeypatch):
    from test_app import page_text

    tools.add_account("Savings", "4417")
    at = add_page_with_upload(monkeypatch, RULED, manual=True)
    assert not at.exception
    assert [m.value for m in at.metric][:4] == ["24", "01 Sep – 30 Sep", "₹17,334", "₹14,111"]
    assert any(e.label == "PDF password" for e in at.expander)
    label_button(at, "Import 24 rows").click().run()
    assert not at.exception
    assert "Added as new entries" in page_text(at) and "Balanced" in page_text(at)
    assert tools.reconcile("Savings")["balanced"]


def test_add_page_imports_a_clear_pdf_at_once_without_an_import_click(monkeypatch):
    from test_app import page_text

    tools.add_account("Savings", "4417")
    at = add_page_with_upload(monkeypatch, RULED)
    assert not at.exception
    assert not any(b.label.startswith("Import ") for b in at.button)
    assert any(b.label == "Undo this import" for b in at.button)
    assert "Balanced" in page_text(at)
    assert tools.reconcile("Savings")["balanced"]


def test_add_page_asks_for_the_password_and_never_keeps_it(monkeypatch):
    tools.add_account("Savings", "4417")
    at = add_page_with_upload(monkeypatch, ENCRYPTED)
    assert any("password-protected" in e.value for e in at.error)
    assert len(at.metric) == 0                                                 # nothing is shown until it opens
    field = next(t for t in at.text_input if t.label == "PDF password (optional)")
    assert field.proto.type == 1                                               # a password field (dots, not letters)
    assert "If your bank's email gave a password for this PDF, enter it" in field.help

    field.set_value("wrong-one")
    label_button(at, "Read the PDF").click().run()
    assert any("did not open the PDF" in e.value for e in at.error)

    next(t for t in at.text_input if t.label == "PDF password (optional)").set_value(PASSWORD)
    label_button(at, "Read the PDF").click().run()
    assert not at.exception and not at.error
    # a clear statement is imported at once: the password form is done, the table is imported, the password is gone
    assert len(tools.list_transactions(limit=50)) == 6 and tools.reconcile("Savings")["balanced"]
    # what the session keeps is the table, never the password
    kept = at.session_state["pdf_read"]
    assert set(kept) == {"key", "table", "error"} and kept["error"] is None
    assert PASSWORD not in repr(kept["key"]) and PASSWORD not in kept["table"].to_csv()
    assert not holds(at.session_state.to_dict(), PASSWORD)


def test_add_page_shows_the_scan_message(monkeypatch):
    at = add_page_with_upload(monkeypatch, IMAGE_ONLY)
    assert not at.exception
    assert any("scan or a photo" in e.value for e in at.error)


def test_the_password_variable_is_not_written_to_session_state_in_the_app_source():
    source = (Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    block = source[source.index("def statement_table"):source.index("def account_choices")]
    assert "st.session_state[" in block and "typed" in block
    assert not any("st.session_state" in line and "typed" in line for line in block.splitlines())
    assert "clear_on_submit=True" in block
