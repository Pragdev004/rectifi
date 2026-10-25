"""The web app in a real browser, part 2: capture (quick entry, SMS, statement import), the ledger, budgets and accounts.

Every test proves what the browser did by reading the database afterwards. Skipped when Playwright, Chromium or web/dist is missing.
"""

from datetime import date
from pathlib import Path

import pytest
from test_web_e2e import CHECK_TARGETS
from web_support import browser, live_server, new_page, pinned_today, seed_month, shot  # noqa: F401

from rectifi import tools

FIXTURES = Path(__file__).parent / "fixtures"
SMS = "Rs.250.00 debited from A/c XX4417 on 21-10-26 to VPA swiggy@icici UPI Ref No 429183746521"
WIDTHS = pytest.mark.parametrize("kind", ["phone", "desktop"])


def txns(**kw):
    return tools.list_transactions(None, None, **kw)


def no_sideways(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"


@WIDTHS
def test_quick_entry_saves_a_row_and_undo_removes_it(browser, live_server, kind):
    seed_month()
    before = len(txns())
    page, problems = new_page(browser, live_server, kind)
    page.goto("/add")
    page.get_by_label("Amount (₹)").fill("333")
    page.get_by_label("Paid to / received from").fill("Zomato")
    page.get_by_role("button", name="Save entry").click()
    page.wait_for_selector("[data-testid=receipt]")
    assert "Zomato" in page.inner_text("[data-testid=receipt]")
    assert len(txns()) == before + 1
    row = [t for t in txns() if t["payee"] == "Zomato"][0]
    assert str(row["amount"]) == "333.00" and row["direction"] == "debit"
    shot(page, f"add-quick-saved-{'390' if kind == 'phone' else '1280'}-light")
    no_sideways(page)
    page.get_by_role("button", name="Undo").click()
    page.wait_for_selector("text=Removed. That entry is gone.")
    assert len(txns()) == before
    assert not problems, problems


def test_quick_entry_shows_the_servers_message_beside_the_field(browser, live_server):
    seed_month()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/add")
    page.get_by_label("Amount (₹)").fill("abc")
    page.get_by_role("button", name="Save entry").click()
    page.wait_for_selector("input[aria-invalid=true]")
    assert page.get_by_label("Amount (₹)").get_attribute("aria-invalid") == "true"
    assert not [p for p in problems if "pageerror" in p], problems


@WIDTHS
def test_paste_sms_previews_then_saves_with_the_right_category(browser, live_server, kind):
    seed_month()
    page, problems = new_page(browser, live_server, kind)
    page.goto("/add?tab=sms")
    page.get_by_role("textbox", name="Bank SMS").fill(SMS)
    page.wait_for_selector("[data-testid=sms-slip]")
    assert "swiggy" in page.inner_text("[data-testid=sms-slip]").lower()
    shot(page, f"add-sms-{'390' if kind == 'phone' else '1280'}-light")
    page.get_by_role("button", name="Save from SMS").click()
    page.wait_for_selector("[data-testid=receipt]")
    saved = [t for t in txns() if t["upi_ref"] == "429183746521"]
    assert len(saved) == 1 and saved[0]["category"] == "Food & Dining"
    assert not problems, problems


@WIDTHS
def test_csv_import_shows_the_slip_and_undo_removes_exactly_those_rows(browser, live_server, kind):
    tools.add_account("HDFC Savings", "4417")
    page, problems = new_page(browser, live_server, kind)
    page.goto("/add?tab=import")
    page.set_input_files("input[type=file]", str(FIXTURES / "hdfc_style_statement.csv"))
    page.wait_for_selector("[data-testid=import-slip]")
    assert "imported" in page.inner_text("[data-testid=import-slip]").lower()
    added = int(page.inner_text("[data-testid=import-added]"))
    assert added > 0 and len(txns()) == added
    assert page.query_selector("[data-testid=recon-slip]")
    shot(page, f"add-import-{'390' if kind == 'phone' else '1280'}-light")
    no_sideways(page)
    page.get_by_role("button", name="Undo this import").click()
    page.wait_for_selector("[data-testid=import-undone]")
    assert "Import undone" in page.inner_text("[data-testid=import-undone]")
    assert len(txns()) == 0
    assert not problems, problems


def test_manual_import_shows_columns_first_then_commits(browser, live_server):
    tools.add_account("HDFC Savings", "4417")
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/add?tab=import")
    page.get_by_label("Let me check the columns", exact=False).check()
    page.set_input_files("input[type=file]", str(FIXTURES / "hdfc_style_statement.csv"))
    page.wait_for_selector("[data-testid=import-preview]")
    assert len(txns()) == 0, "nothing is imported before the button"
    shot(page, "add-import-preview-1280-light")
    page.locator("[data-testid=import-preview]").get_by_role("button", name="Import", exact=False).last.click()
    page.wait_for_selector("[data-testid=import-slip]")
    assert len(txns()) > 0
    assert not problems, problems


def test_password_pdf_asks_for_the_password(browser, live_server):
    tools.add_account("HDFC Savings", "4417")
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/add?tab=import")
    page.set_input_files("input[type=file]", str(FIXTURES / "statement_encrypted.pdf"))
    page.wait_for_selector("form[aria-label='PDF password']")
    page.get_by_role("textbox", name="PDF password").fill("definitely-wrong")
    page.get_by_role("button", name="Open the PDF").click()
    page.wait_for_selector("form[aria-label='PDF password'] [role=alert]")
    assert page.get_by_role("textbox", name="PDF password").input_value() == "", "the password leaves the field once sent"
    assert "definitely-wrong" not in page.content()
    assert not [p for p in problems if "pageerror" in p], problems


def test_ledger_category_change_on_desktop_is_saved(browser, live_server):
    seed_month()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/ledger")
    page.wait_for_selector("[data-testid=ledger-row]")
    row = page.locator("[data-testid=ledger-row]", has_text="Blinkit").first
    txn_id = int(row.get_attribute("data-txn-id"))
    row.locator("select[aria-label^='Category for']").select_option(label="Shopping")
    page.wait_for_timeout(600)
    assert tools.get_transaction(txn_id)["category"] == "Shopping"
    shot(page, "ledger-1280-light")
    no_sideways(page)
    assert not problems, problems


def test_ledger_on_phone_opens_a_sheet_and_links_an_unlinked_entry(browser, live_server):
    seed_month()
    tools.add_transaction("120", "debit", date(2026, 10, 19), payee="Tea stall", category="Food & Dining", source="manual")
    loose = [t for t in txns() if t["payee"] == "Tea stall"][0]
    assert loose["account_id"] is None
    page, problems = new_page(browser, live_server, "phone")
    page.goto("/ledger")
    page.get_by_role("button", name="Tea stall, open details").click()
    dialog = page.locator("dialog[open]")
    dialog.wait_for()
    shot(page, "ledger-sheet-390-light", tall=False)
    dialog.get_by_label("Account").select_option(label="SBI Savings")
    page.wait_for_timeout(600)
    assert tools.get_transaction(loose["id"])["account_id"] is not None
    page.keyboard.press("Escape")
    page.wait_for_selector("dialog[open]", state="detached")
    no_sideways(page)
    assert not problems, problems


def test_ledger_filter_by_search_and_clear(browser, live_server):
    seed_month()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/ledger?search=swiggy")
    page.wait_for_selector("[data-testid=ledger-row]")
    assert page.locator("[data-testid=ledger-row]").count() == 1
    page.goto("/ledger?search=nothing-like-this")
    page.wait_for_selector("text=No transactions match.")
    assert not problems, problems


@WIDTHS
def test_setting_a_budget_is_reflected_in_budget_status(browser, live_server, kind):
    seed_month()
    page, problems = new_page(browser, live_server, kind)
    page.goto("/budgets?month=2026-10")
    page.wait_for_selector("[data-testid='budget-Food & Dining']")
    shot(page, f"budgets-{'390' if kind == 'phone' else '1280'}-light")
    page.get_by_role("textbox", name="Limit for Groceries").fill("2500")
    page.get_by_role("button", name="Save the limit for Groceries").click()
    page.wait_for_selector("[data-testid='budget-Groceries']")
    status = tools.budget_status("2026-10")
    assert {c["category"]: str(c["limit"]) for c in status["categories"]}["Groceries"] == "2500.00"
    no_sideways(page)
    assert not problems, problems


def test_editing_and_removing_a_limit(browser, live_server):
    seed_month()
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/budgets?month=2026-10")
    page.wait_for_selector("[data-testid='budget-Transport']")
    box = page.get_by_role("textbox", name="Limit for Transport")
    box.fill("1000")
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_timeout(500)
    status = {c["category"]: str(c["limit"]) for c in tools.budget_status("2026-10")["categories"]}
    assert status["Transport"] == "1000.00"
    page.locator("[data-testid='budget-Transport']").get_by_role("button", name="Remove limit").click()
    page.wait_for_timeout(500)
    assert "Transport" not in {c["category"] for c in tools.budget_status("2026-10")["categories"]}
    assert not problems, problems


def test_empty_budget_month_can_copy_from_the_month_before(browser, live_server):
    seed_month()
    tools.set_budget("Food & Dining", "3000", "2026-09")
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/budgets?month=2026-09")
    page.wait_for_selector("[data-testid='budget-Food & Dining']")
    page.goto("/budgets?month=2026-08")
    page.wait_for_selector("text=No budgets for this month yet.")
    shot(page, "budgets-empty-1280-light")
    assert not problems, problems


@WIDTHS
def test_accounts_page_reconciliation_and_recording_a_second_balance(browser, live_server, kind):
    tools.add_account("SBI Savings", "4417")
    tools.record_balance("SBI Savings", date(2026, 9, 30), "5000")
    tools.add_transaction("500", "debit", date(2026, 10, 5), payee="Amazon", category="Shopping", account="SBI Savings", source="manual")
    page, problems = new_page(browser, live_server, kind)
    page.goto("/accounts")
    page.wait_for_selector("[data-testid^=account-]")
    assert page.inner_text("[data-testid^=account-] h2").startswith("SBI Savings")
    shot(page, f"accounts-gap-{'390' if kind == 'phone' else '1280'}-light")
    page.get_by_role("button", name="Record a balance").click()
    page.get_by_label("End of day").fill("2026-10-20")
    page.get_by_label("Balance (₹)").fill("4500")
    page.get_by_role("button", name="Save balance").click()
    page.wait_for_selector("[data-testid=recon-slip][data-balanced=true]")
    shot(page, f"accounts-balanced-{'390' if kind == 'phone' else '1280'}-light")
    no_sideways(page)
    assert not problems, problems


def test_accounts_empty_state_and_adding_one(browser, live_server):
    page, problems = new_page(browser, live_server, "desktop")
    page.goto("/accounts")
    page.wait_for_selector("text=No accounts yet.")
    shot(page, "accounts-empty-1280-light")
    page.get_by_label("Name").fill("Kotak 811")
    page.get_by_label("Last 4 digits").fill("9021")
    page.get_by_role("button", name="Add account").click()
    page.wait_for_selector("[data-testid^=account-]")
    assert [a["name"] for a in tools.list_accounts()] == ["Kotak 811"]
    assert not problems, problems


@WIDTHS
@pytest.mark.parametrize("path", ["/add", "/add?tab=sms", "/add?tab=import", "/ledger", "/budgets", "/accounts"])
def test_targets_are_44px_and_nothing_scrolls_sideways(browser, live_server, kind, path):
    seed_month()
    page, problems = new_page(browser, live_server, kind)
    page.goto(path)
    page.wait_for_selector("main h1, main h2")
    page.wait_for_timeout(500)
    no_sideways(page)
    assert page.evaluate(CHECK_TARGETS) == []
    assert not problems, problems
