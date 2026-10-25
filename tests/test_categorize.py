"""The fixed classification layers: learned rules, built-in rules, heuristics, look-alike retrieval, the fallback,
and how an import and a correction record where each category came from. No model, no network."""

from datetime import date
from decimal import Decimal

import pytest

from rectifi import categorize, db, tools
from rectifi.db import connect
from rectifi.importer import StatementRow, import_rows


def decide(payee, narration, direction="debit"):
    with connect() as conn:
        return categorize.decide(conn, payee, narration, direction)


# ───────────── heuristics: every entry has a positive and a negative case ─────────────

POSITIVE = [
    ("ATM WDL 4455 MG ROAD", "debit", "Cash Withdrawal"),
    ("NWD-123456-KOCHI", "debit", "Cash Withdrawal"),
    ("NEFT CR ACME PVT LTD SALARY OCT", "credit", "Salary & Allowance"),
    ("INT.PD 01-07-26", "credit", "Interest & Cashback"),
    ("UPI CASHBACK OFFER", "credit", "Interest & Cashback"),
    ("REFUND FOR ORDER 1234", "credit", "Refunds"),
    ("SMS ALERT CHRGS Q2", "debit", "Fees & Charges"),
    ("NACH DR BAJAJ FINANCE", "debit", "EMI & Loans"),
    ("HOME LOAN EMI", "debit", "EMI & Loans"),
    ("ZERODHA BROKING", "debit", "Investments"),
    ("SIP 4455 NIPPON", "debit", "Investments"),
    ("NOBROKER RENT OCT", "debit", "Rent"),
    ("HPCL RETAIL OUTLET", "debit", "Fuel"),
    ("NAYARA ENERGY KOCHI", "debit", "Fuel"),
    ("CITY MEDICAL STORE", "debit", "Health"),
    ("SRI KRISHNA PHARMACY", "debit", "Health"),
    ("HOTEL SARAVANA RESTAURANT", "debit", "Food & Dining"),
    ("LOCAL SUPERMARKET PALA", "debit", "Groceries"),
    ("MULTIPLEX TICKETS", "debit", "Entertainment"),
    ("AIR INDIA FLIGHT BLR", "debit", "Travel"),
    ("RADIO TAXI KOCHI", "debit", "Transport"),
    ("MOBILE RECHARGE 599", "debit", "Bills & Utilities"),
    ("TUITION FEES OCT", "debit", "Education"),
    ("FITNESS GYM MONTHLY", "debit", "Personal Care"),
    ("TEMPLE DONATION", "debit", "Gifts & Donations"),
    ("TRANSFER TO SELF", "debit", "Transfers"),
]

NEGATIVE = [          # look alike, but must NOT be the heuristic's category
    ("PARENT TEACHER MEET", "debit", "Rent"),
    ("CURRENT ACCOUNT OPENING", "debit", "Rent"),
    ("PREMIUM MEMBERSHIP", "debit", "EMI & Loans"),
    ("FLATMATE SHARE", "debit", "Cash Withdrawal"),
    ("SALARY ADVANCE REPAID", "debit", "Salary & Allowance"),      # wrong direction: salary is money in
    ("CASHBACK REVERSED BY ME", "debit", "Interest & Cashback"),
    ("DISPENSARY", "debit", "Health"),
    ("PIZZAS", "credit", "Food & Dining"),                         # a credit can never be an expense
    ("PRESIDENT HOTEL", "debit", "Travel"),                        # 'hotel' alone is not a trip
    ("ROOM RENTAL", "debit", "Rent"),
    ("SIPHON", "debit", "Investments"),
    ("PETROLEUM ART", "debit", "Fuel"),
    ("COMPANY CHARGERS", "debit", "Fees & Charges"),
]


@pytest.mark.parametrize("text, direction, category", POSITIVE)
def test_heuristic_positive_cases(text, direction, category):
    got = decide(None, text, direction)
    assert (got.category, got.source) in {(category, "heuristic"), (category, "rule")}, got


@pytest.mark.parametrize("text, direction, category", NEGATIVE)
def test_heuristic_negative_cases(text, direction, category):
    assert decide(None, text, direction).category != category


def test_every_heuristic_has_a_positive_case_and_a_real_category():
    kinds = dict(db.DEFAULT_CATEGORIES)
    for regex, direction, category, confidence in categorize.HEURISTICS:
        assert category in kinds, category
        assert 80 <= confidence <= 90
        if direction == "debit":
            assert kinds[category] in ("expense", "transfer")
        if direction == "credit":
            assert kinds[category] in ("income", "transfer")
        hits = [t for t, d, c in POSITIVE if c == category and regex.search(t.lower()) and direction in (None, d)]
        assert hits, f"no positive test for {regex.pattern}"


def test_rent_is_a_word_not_a_substring():
    assert decide(None, "UPI/DR/123/PARENT FUND/x").category == "Miscellaneous"
    assert decide(None, "UPI/DR/123/CURRENT BILL/x").category == "Miscellaneous"
    assert decide("NoBroker", "monthly rent").category == "Rent"


def test_own_account_digits_mean_a_transfer_but_a_bare_number_does_not():
    tools.add_account("SBI Savings", "4455")
    assert decide(None, "TRANSFER TO XXXX4455 SBI").category == "Transfers"
    assert decide(None, "BY TRANSFER A/C 4455").category == "Transfers"
    assert decide(None, "BILL NO 4455 SHARMA STORES").category == "Miscellaneous"


# ───────────── built-in and learned rules ─────────────

def test_a_built_in_rule_wins_and_says_which():
    got = decide("Swiggy", "UPI/DR/1/SWIGGY")
    assert (got.category, got.source, got.confidence) == ("Food & Dining", "rule", 95)
    assert "swiggy" in got.reason


def test_short_rules_must_start_a_word():
    assert decide(None, "HOLA AMIGOS CAFETERIA X").source != "rule" or decide(None, "HOLA AMIGOS").category == "Miscellaneous"
    assert decide(None, "HOLA AMIGOS").category == "Miscellaneous"          # 'ola' hides inside 'hola'
    assert decide(None, "VEGAS TRADERS").category == "Miscellaneous"        # 'gas' hides inside 'vegas'
    assert decide(None, "OLACABS BLR").category == "Transport"              # but 'ola' starting olacabs is fine


def test_a_learned_rule_beats_a_built_in_rule():
    t = tools.add_transaction("300", "debit", payee="Swiggy", source="chat")["transaction"]
    assert t["category"] == "Food & Dining"
    tools.set_category(t["id"], "Groceries", remember=True)
    got = decide("Swiggy", None)
    assert (got.category, got.source, got.confidence) == ("Groceries", "learned", 98)


def test_correction_with_no_payee_learns_from_the_narration():
    with connect() as conn:
        acc = None
    row = tools.add_transaction("500", "debit", payee=None, description="UPI/DR/429183746521/CULT FIT/YESB/cultfit@ybl/Pay", source="chat")
    t = row["transaction"]
    assert t["category"] == "Miscellaneous" and t["category_source"] == "fallback"
    done = tools.set_category(t["id"], "Health", remember=True)
    assert done["learned_rule"] == "cultfit"
    assert done["transaction"]["category_source"] == "user" and done["transaction"]["category_confidence"] == 100
    again = tools.add_transaction("700", "debit", description="UPI/DR/118273645531/CULT FIT/YESB/cultfit@ybl/Pay", source="chat")
    assert again["transaction"]["category"] == "Health" and again["transaction"]["category_source"] == "learned"


def test_set_category_stamps_user_and_marks_learned_rule_source():
    t = tools.add_transaction("120", "debit", payee="Chai Point", source="chat")["transaction"]
    tools.set_category(t["id"], "Food & Dining", remember=True)
    with connect() as conn:
        rule = conn.execute("SELECT source, category_id FROM merchant_rules WHERE pattern = 'chai point'").fetchone()
    assert rule["source"] == "learned"
    assert tools.get_transaction(t["id"])["category_reason"] == "you chose"


def test_a_user_given_category_is_recorded_as_yours():
    t = tools.add_transaction("80", "debit", payee="Somebody", category="Groceries", source="manual")["transaction"]
    assert (t["category_source"], t["category_confidence"]) == ("user", 100)


def test_credits_never_get_expense_categories_from_rules():
    assert decide("Swiggy", "UPI CR REFUND SWIGGY", "credit").category in ("Refunds", "Other income")


# ───────────── retrieval ─────────────

def seed_label(payee, category, source="user", direction="debit", description=None):
    t = tools.add_transaction("100", direction, payee=payee, category=category, description=description, source="manual")["transaction"]
    with connect() as conn:
        conn.execute("UPDATE transactions SET category_source = %s WHERE id = %s", (source, t["id"]))
    return t


def test_retrieval_picks_the_trusted_neighbour():
    seed_label("Anand Sweets Mylapore", "Food & Dining")
    got = decide("Anand Sweets Mylapore 2", None)
    assert (got.category, got.source) == ("Food & Dining", "retrieval")
    assert got.reason.startswith("like #")


def test_retrieval_ignores_model_and_fallback_labels():
    seed_label("Anand Sweets Mylapore", "Food & Dining", source="llm")
    seed_label("Anand Sweets Mylapore", "Food & Dining", source="fallback")
    seed_label("Anand Sweets Mylapore", "Food & Dining", source="retrieval")
    assert decide("Anand Sweets Mylapore 2", None).source == "fallback"


def test_retrieval_needs_the_neighbours_to_agree():
    seed_label("Kumar Stores", "Groceries")
    seed_label("Kumar Stores", "Shopping")
    seed_label("Kumar Stores", "Health")
    got = decide("Kumar Stores", None)
    assert got.source == "fallback"


def test_retrieval_does_not_cross_directions():
    seed_label("Anand Sweets Mylapore", "Food & Dining")
    assert decide("Anand Sweets Mylapore", None, "credit").category == "Other income"


def test_a_weak_echo_becomes_a_suggestion_not_a_label():
    seed_label("Sri Venkatesa Bhavan Tiffin House", "Food & Dining")
    got = decide("Venkatesa Hotel Annex Catering", None)
    if got.source == "fallback" and got.suggested:
        assert got.suggested == "Food & Dining" and 0 < got.suggested_confidence < 90
    else:
        assert got.source in ("retrieval", "fallback")


def test_nothing_matches_means_the_catch_all():
    got = decide("Rahul Sharma", "UPI/DR/1/RAHUL SHARMA/x")
    assert (got.category, got.source) == ("Miscellaneous", "fallback")
    assert decide("Rahul Sharma", "UPI/CR/1/RAHUL SHARMA/x", "credit").category == "Other income"


# ───────────── merchant_key ─────────────

def test_merchant_key_is_stable_and_cleans_narrations():
    k = categorize.merchant_key
    assert k("SWIGGY*ORDER 8812@icici") == k("swiggy order")
    assert k(None, "UPI/DR/429183746521/SWIGGY/YESB/swiggy@ybl/Payment") == "swiggy"
    assert k(None, "UPI/DR/429183746521/RAHUL SHARMA/HDFC/Payment") == "rahul sharma"
    assert k(None, "") == "" and k("", None) == ""
    assert k("Zomato 12/10/26 ORDER") == "zomato order"


# ───────────── an import marks sources, instantly, with no model ─────────────

def rows():
    d = date(2026, 10, 1)
    return [
        StatementRow(d, Decimal("250.00"), "debit", "UPI/DR/111111111111/SWIGGY/YESB/swiggy@ybl/Pay", None, None, 1),
        StatementRow(d, Decimal("60000.00"), "credit", "NEFT CR ACME PVT LTD SALARY OCT", None, None, 2),
        StatementRow(d, Decimal("90.00"), "debit", "UPI/DR/222222222222/RAHUL SHARMA/HDFC/Pay", None, None, 3),
        StatementRow(d, Decimal("120.00"), "debit", "UPI/DR/333333333333/MURUGAN TIFFIN/SBIN/Pay", None, None, 4),
    ]


def test_import_records_the_source_of_every_row_and_queues_the_unsure_ones():
    acc = tools.add_account("SBI", "1234")
    result = import_rows(rows(), acc["id"], "oct.csv")
    assert result.added == 4
    items = tools.batch_rows(result.batch_id)
    by_payee = {r["payee"]: r for r in items}
    assert by_payee["swiggy"]["category_source"] == "rule" and by_payee["swiggy"]["classify_state"] is None
    salary = next(r for r in items if r["direction"] == "credit")
    assert (salary["category"], salary["category_source"]) == ("Salary & Allowance", "heuristic")
    sharma = by_payee["RAHUL SHARMA"]
    assert (sharma["category"], sharma["category_source"], sharma["classify_state"]) == ("Miscellaneous", "fallback", "pending")
    assert len(tools.pending_classification(result.batch_id)) == 2
    counts = tools.batch_source_counts(result.batch_id)
    assert counts["total"] == 4 and counts["miscellaneous"] == 2
    assert counts["by_source"] == {"rule": 1, "heuristic": 1, "fallback": 2}


def test_show_filters():
    acc = tools.add_account("SBI", "1234")
    batch = import_rows(rows(), acc["id"], "oct.csv").batch_id
    assert len(tools.batch_rows(batch, "misc")) == 2
    assert tools.batch_rows(batch, "model") == [] and tools.batch_rows(batch, "suggested") == []
    with pytest.raises(tools.RectifiError):
        tools.batch_rows(batch, "nonsense")


def test_apply_to_similar_skips_user_rows_and_rule_rows():
    acc = tools.add_account("SBI", "1234")
    d = date(2026, 10, 1)
    extra = [StatementRow(d, Decimal(str(10 + i)), "debit", f"UPI/DR/55555555555{i}/MURUGAN TIFFIN/SBIN/Pay", None, None, i + 1) for i in range(3)]
    batch = import_rows(extra, acc["id"], "x.csv").batch_id
    ids = [r["id"] for r in tools.batch_rows(batch)]
    tools.set_category(ids[0], "Food & Dining", remember=False)
    tools.set_category(ids[1], "Shopping", remember=False)              # a deliberate choice: must survive
    assert tools.apply_category_to_similar(ids[0]) == 1                  # only ids[2] was a machine guess
    after = {r["id"]: r for r in tools.batch_rows(batch)}
    assert after[ids[1]]["category"] == "Shopping" and after[ids[1]]["category_source"] == "user"
    assert after[ids[2]]["category"] == "Food & Dining" and after[ids[2]]["category_source"] == "learned"


def test_apply_classifications_never_overrides_a_correction():
    acc = tools.add_account("SBI", "1234")
    batch = import_rows(rows(), acc["id"], "oct.csv").batch_id
    pending = tools.pending_classification(batch)
    mine, other = pending[0]["id"], pending[1]["id"]
    tools.set_category(mine, "Health")                                    # the person corrects it while the job runs
    out = tools.apply_classifications([
        {"id": mine, "category": "Food & Dining", "confidence": 90, "reason": "m", "apply": True},
        {"id": other, "category": "Food & Dining", "confidence": 90, "reason": "m", "apply": True},
        {"id": other, "category": "Salary & Allowance", "confidence": 99, "reason": "wrong kind", "apply": True},
    ])
    assert out["applied"] == 1
    assert tools.get_transaction(mine)["category"] == "Health"
    assert tools.get_transaction(other)["category_source"] == "llm"
