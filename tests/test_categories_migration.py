"""Other -> Miscellaneous, the new categories and rules, and Fuel. The migration keeps ids, data, budgets and rules."""

from datetime import date
from decimal import Decimal

from support.olddb import rebuild_old_database

from rectifi import db, tools
from rectifi.categorize import FALLBACK_EXPENSE, FALLBACK_INCOME


def q(sql, params=()):
    with db.connect() as conn:
        return conn.execute(sql, params).fetchall()


def seed_old_database():
    """An old database: a payment and a budget in "Other", a rule the person taught, and the old petrol rule in Transport."""
    rebuild_old_database()
    with db.connect() as conn:
        other = conn.execute("SELECT id FROM categories WHERE name = 'Other'").fetchone()["id"]
        health = conn.execute("SELECT id FROM categories WHERE name = 'Health'").fetchone()["id"]
        conn.execute("INSERT INTO merchant_rules (pattern, category_id) VALUES ('cult fit', %s)", (health,))
        conn.execute("INSERT INTO budgets (category_id, month, limit_amount) VALUES (%s, '2026-10-01', 1500)", (other,))
        conn.execute(
            """INSERT INTO transactions (amount, direction, txn_date, payee, category_id, source)
               VALUES (300, 'debit', '2026-10-05', 'Corner shop', %s, 'manual')""", (other,))
    return other


def test_other_becomes_miscellaneous_with_the_same_id():
    other_id = seed_old_database()
    db.init_db()
    row = q("SELECT id, name, kind FROM categories WHERE id = %s", (other_id,))[0]
    assert row["name"] == FALLBACK_EXPENSE == "Miscellaneous" and row["kind"] == "expense"
    assert q("SELECT COUNT(*) AS n FROM categories WHERE name = 'Other'")[0]["n"] == 0
    assert q("SELECT COUNT(*) AS n FROM categories WHERE name = 'Miscellaneous'")[0]["n"] == 1
    # the payment and the budget stayed on the same row
    txn = tools.list_transactions()[0]
    assert txn["category"] == "Miscellaneous" and txn["category_id"] == other_id and txn["amount"] == Decimal("300.00")
    budgets = tools.budget_status("2026-10")["categories"]
    assert [(b["category"], b["limit"]) for b in budgets] == [("Miscellaneous", Decimal("1500.00"))]


def test_new_categories_exist_with_the_right_kinds():
    seed_old_database()
    db.init_db()
    kinds = {r["name"]: r["kind"] for r in q("SELECT name, kind FROM categories")}
    for name in ("Fuel", "Personal Care", "EMI & Loans", "Fees & Charges", "Gifts & Donations", "Cash Withdrawal", "Miscellaneous"):
        assert kinds[name] == "expense", name
    assert kinds["Interest & Cashback"] == "income" and kinds[FALLBACK_INCOME] == "income"
    assert kinds["Investments"] == "transfer" and kinds["Transfers"] == "transfer"
    assert len(kinds) == len(db.DEFAULT_CATEGORIES)


def test_petrol_and_fuel_rules_move_to_fuel_and_others_stay():
    seed_old_database()
    db.init_db()
    rules = {r["pattern"]: r["category"] for r in q(
        "SELECT r.pattern, c.name AS category FROM merchant_rules r JOIN categories c ON c.id = r.category_id")}
    assert rules["petrol"] == "Fuel" and rules["fuel"] == "Fuel"
    assert rules["uber"] == "Transport" and rules["swiggy"] == "Food & Dining"
    assert rules["zerodha"] == "Investments" and rules["cashback"] == "Interest & Cashback"


def test_taught_rules_are_marked_learned_and_defaults_stay_default():
    seed_old_database()
    db.init_db()
    source = {r["pattern"]: r["source"] for r in q("SELECT pattern, source FROM merchant_rules")}
    assert source["cult fit"] == "learned"
    assert source["swiggy"] == source["petrol"] == source["zerodha"] == "default"


def test_a_fresh_database_has_no_other_and_petrol_is_fuel():
    # conftest's fresh_db already ran init_db on an empty database
    assert q("SELECT COUNT(*) AS n FROM categories WHERE name = 'Other'")[0]["n"] == 0
    assert tools.add_transaction("500", "debit", date(2026, 10, 1), payee="HP petrol pump")["transaction"]["category"] == "Fuel"
    assert tools.add_transaction("90", "debit", date(2026, 10, 2), payee="Mystery shop")["transaction"]["category"] == "Miscellaneous"
    assert tools.add_transaction("20", "credit", date(2026, 10, 3), payee="Mystery")["transaction"]["category"] == "Other income"


def snapshot():
    return (q("SELECT id, name, kind FROM categories ORDER BY id"),
            q("SELECT id, pattern, category_id, source FROM merchant_rules ORDER BY id"),
            q("SELECT key FROM app_settings ORDER BY key"))


def test_running_init_twice_changes_nothing():
    seed_old_database()
    db.init_db()
    first = snapshot()
    db.init_db()
    db.init_db()
    assert snapshot() == first


def test_a_later_petrol_to_transport_choice_is_not_undone_at_next_start():
    seed_old_database()
    db.init_db()
    with db.connect() as conn:
        conn.execute("UPDATE merchant_rules SET category_id = (SELECT id FROM categories WHERE name = 'Transport') WHERE pattern = 'petrol'")
    db.init_db()
    assert q("SELECT c.name FROM merchant_rules r JOIN categories c ON c.id = r.category_id WHERE r.pattern = 'petrol'")[0]["name"] == "Transport"


def test_if_the_person_has_both_other_and_miscellaneous_both_are_left_alone():
    rebuild_old_database()
    with db.connect() as conn:
        conn.execute("INSERT INTO categories (name, kind) VALUES ('Miscellaneous', 'expense')")
    db.init_db()
    names = [r["name"] for r in q("SELECT name FROM categories WHERE name IN ('Other', 'Miscellaneous') ORDER BY name")]
    assert names == ["Miscellaneous", "Other"]
    # the app uses Miscellaneous as the catch-all
    assert tools.add_transaction("90", "debit", date(2026, 10, 2), payee="Mystery shop")["transaction"]["category"] == "Miscellaneous"
