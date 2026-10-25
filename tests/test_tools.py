from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from rectifi import tools
from rectifi.importer import import_file
from rectifi.tools import RectifiError

FIXTURE = Path(__file__).parent / "fixtures" / "hdfc_style_statement.csv"


def test_add_and_auto_categorise():
    result = tools.add_transaction("250", "debit", "today", payee="swiggy@icici", source="chat")
    assert result["status"] == "added"
    assert result["transaction"]["category"] == "Food & Dining"
    assert result["transaction"]["amount"] == Decimal("250.00")


def test_database_rejects_duplicate_upi_ref():
    first = tools.add_transaction("250", "debit", payee="swiggy", upi_ref="429183746521", source="sms")
    second = tools.add_transaction("250", "debit", payee="swiggy", upi_ref="429183746521", source="sms")
    assert first["status"] == "added" and second["status"] == "duplicate"
    assert len(tools.list_transactions()) == 1


def test_rejects_future_and_bad_input():
    with pytest.raises(RectifiError):
        tools.add_transaction("250", "debit", (date.today() + timedelta(days=3)).isoformat())
    with pytest.raises(RectifiError):
        tools.add_transaction("0", "debit")
    with pytest.raises(RectifiError):
        tools.add_transaction("100", "sideways")
    with pytest.raises(RectifiError):
        tools.add_transaction("100", "debit", category="Nonsense")


def test_summary_excludes_transfers():
    tools.add_transaction("10000", "credit", category="Salary & Allowance")
    tools.add_transaction("2000", "debit", category="Food & Dining")
    tools.add_transaction("5000", "debit", category="Transfers")
    s = tools.spending_summary()
    assert s["income"] == Decimal("10000.00")
    assert s["spending"] == Decimal("2000.00")
    assert s["savings_rate"] == Decimal("80.0")


def test_budget_states():
    tools.set_budget("Food & Dining", "1000")
    tools.add_transaction("850", "debit", category="Food & Dining")
    status = tools.budget_status()
    food = status["categories"][0]
    assert food["state"] == "near" and food["remaining"] == Decimal("150.00")
    tools.add_transaction("200", "debit", category="Food & Dining")
    assert tools.budget_status()["categories"][0]["state"] == "over"


def test_recategorise_learns_rule():
    t = tools.add_transaction("120", "debit", payee="Chai Point", source="chat")["transaction"]
    assert t["category"] == "Other"
    tools.set_category(t["id"], "Food & Dining", remember=True)
    again = tools.add_transaction("90", "debit", payee="chai point", source="chat")["transaction"]
    assert again["category"] == "Food & Dining"


def _october_rows_exist():
    # the fixture is dated October 2026; skip the import tests if the test machine's clock is earlier
    return date.today() >= date(2026, 10, 12)


@pytest.mark.skipif(not _october_rows_exist(), reason="fixture dates are in the future on this machine")
def test_statement_import_dedupes_merges_and_reconciles():
    tools.add_account("HDFC Savings", "4417")
    # logged by chat before the statement arrived: the same Uber ride, no reference
    chat = tools.add_transaction("1240.50", "debit", "2026-10-04", payee="uber", source="chat")["transaction"]
    # logged from SMS with the UPI ref: the Swiggy payment
    tools.add_transaction("250", "debit", "2026-10-01", payee="swiggy@icici", upi_ref="429183746521", source="sms")

    result = import_file(FIXTURE.read_bytes(), FIXTURE.name, "HDFC Savings")
    assert result.duplicates == 1          # swiggy, by UPI ref
    assert result.merged == 1              # uber chat entry, confirmed by the bank
    assert result.added == 3               # stipend, dmart, netflix
    assert result.opening == (date(2026, 9, 30), Decimal("12000.00"))

    merged = tools.get_transaction(chat["id"])
    assert merged["upi_ref"] == "429900112233" and merged["txn_date"] == date(2026, 10, 5)

    rec = tools.reconcile("HDFC Savings")
    assert rec["balanced"], rec
    assert rec["gap"] == Decimal("0.00")

    # importing the same file again changes nothing
    again = import_file(FIXTURE.read_bytes(), FIXTURE.name, "HDFC Savings")
    assert again.added == 0 and again.merged == 0 and again.duplicates == 5

    # deleting one row makes the arithmetic catch it
    netflix = tools.list_transactions(search="netflix")[0]
    tools.delete_transaction(netflix["id"])
    rec = tools.reconcile("HDFC Savings")
    assert not rec["balanced"] and rec["gap"] == Decimal("-199.00")


@pytest.mark.skipif(not _october_rows_exist(), reason="fixture dates are in the future on this machine")
def test_statement_categories():
    tools.add_account("HDFC Savings", "4417")
    import_file(FIXTURE.read_bytes(), FIXTURE.name, "HDFC Savings")
    by_payee = {t["payee"]: t["category"] for t in tools.list_transactions()}
    assert by_payee["swiggy@icici"] == "Food & Dining"
    assert by_payee["netflix@hdfcbank"] == "Subscriptions"
    assert by_payee["uber@axisbank"] == "Transport"


def test_category_kind_must_match_direction():
    with pytest.raises(RectifiError, match="income category"):
        tools.add_transaction("250", "debit", category="Salary & Allowance")        # money out, income category
    with pytest.raises(RectifiError, match="expense category"):
        tools.add_transaction("250", "credit", category="Food & Dining")            # money in, expense category
    assert tools.list_transactions() == []
    # transfers work both ways
    assert tools.add_transaction("250", "debit", category="Transfers")["status"] == "added"
    assert tools.add_transaction("250", "credit", category="Transfers")["status"] == "added"


def test_set_category_checks_kind_too():
    spent = tools.add_transaction("120", "debit", payee="Chai Point")["transaction"]
    with pytest.raises(RectifiError, match="income category"):
        tools.set_category(spent["id"], "Refunds")
    assert tools.get_transaction(spent["id"])["category"] != "Refunds"            # unchanged
    with pytest.raises(RectifiError, match="No transaction"):
        tools.set_category(999999, "Food & Dining")
    assert tools.set_category(spent["id"], "Groceries")["transaction"]["category"] == "Groceries"


def _statement_csv(*rows):
    head = "Date,Narration,Withdrawal Amt.,Deposit Amt.\n"
    return (head + "\n".join(rows) + "\n").encode()


def test_two_identical_rows_on_one_day_both_import_and_reimport_is_idempotent():
    tools.add_account("Wallet", "9999")
    data = _statement_csv("01/10/26,CHAI POINT CASH,20.00,", "01/10/26,CHAI POINT CASH,20.00,", "02/10/26,BOOKSTORE,300.00,")
    first = import_file(data, "s.csv", "Wallet")
    assert first.added == 3 and first.duplicates == 0
    assert len(tools.list_transactions()) == 3
    again = import_file(data, "s.csv", "Wallet")
    assert again.added == 0 and again.duplicates == 3
    assert len(tools.list_transactions()) == 3


def test_old_fingerprints_still_match():
    """A row imported before the occurrence counter existed must still be recognised as already imported."""
    from rectifi.importer import StatementRow, _fingerprint

    row = StatementRow(date(2026, 10, 1), Decimal("20.00"), "debit", "CHAI POINT CASH", None, None, 1)
    assert _fingerprint(1, row, 1) == _fingerprint(1, row)
    assert _fingerprint(1, row, 2) != _fingerprint(1, row, 1)


def test_xls_gives_a_friendly_error(monkeypatch):
    import pandas as pd

    def no_xlrd(*args, **kwargs):
        raise ImportError("Missing optional dependency 'xlrd'")

    monkeypatch.setattr(pd, "read_excel", no_xlrd)
    from rectifi.importer import read_table

    with pytest.raises(RectifiError, match="Save As"):
        read_table(b"whatever", "old_statement.xls")


def test_broken_excel_file_gives_a_friendly_error():
    from rectifi.importer import read_table

    with pytest.raises(RectifiError, match="Couldn't read"):
        read_table(b"this is not an excel file", "bad.xlsx")
