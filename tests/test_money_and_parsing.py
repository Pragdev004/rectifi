from datetime import date
from decimal import Decimal

import pytest

from rectifi.money import amounts_in_text, fmt_inr, parse_amount
from rectifi.parsing import narration_details, parse_sms


@pytest.mark.parametrize(
    "raw, expected",
    [("₹1,25,000.50", "125000.50"), ("Rs. 250", "250.00"), ("INR 99.999", "100.00"), (250, "250.00"), (0.1, "0.10"), ("1,250/-", "1250.00")],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


def test_parse_amount_rejects_text():
    with pytest.raises(ValueError):
        parse_amount("lots")


def test_indian_grouping():
    assert fmt_inr(Decimal("125000.5")) == "₹1,25,000.50"
    assert fmt_inr(Decimal("12500000")) == "₹1,25,00,000.00"
    assert fmt_inr(Decimal("999")) == "₹999.00"
    assert fmt_inr(Decimal("-250")) == "−₹250.00"


def test_amounts_in_text():
    assert amounts_in_text("spent 250 on swiggy and 1,240.50 on uber") == {Decimal("250.00"), Decimal("1240.50")}
    assert Decimal("2500.00") in amounts_in_text("budget 2.5k for food")
    assert Decimal("100000.00") in amounts_in_text("rent 1 lakh")


SMS_SAMPLES = [
    (
        "Rs.250.00 debited from A/c XX1234 on 21-10-26 to VPA swiggy@icici UPI Ref No 429183746521. If not you, call 1800",
        dict(amount="250.00", direction="debit", last4="1234", payee="swiggy@icici", ref="429183746521", day=date(2026, 10, 21)),
    ),
    (
        "Sent Rs.1240.50 From HDFC Bank A/C *4417 To UBER INDIA On 05/10/26 Ref 429900112233 Not You? Call 18002586161",
        dict(amount="1240.50", direction="debit", last4="4417", payee="UBER INDIA", ref="429900112233", day=date(2026, 10, 5)),
    ),
    (
        "Dear Customer, INR 8,000.00 credited to your A/c XX5678 on 02-Oct-26 by NEFT from IITM. Avl Bal INR 19,750.00",
        dict(amount="8000.00", direction="credit", last4="5678", payee=None, ref=None, day=date(2026, 10, 2)),
    ),
]


@pytest.mark.parametrize("sms, want", SMS_SAMPLES)
def test_parse_sms(sms, want):
    parsed = parse_sms(sms)
    assert parsed is not None
    assert parsed.amount == Decimal(want["amount"])
    assert parsed.direction == want["direction"]
    assert parsed.account_last4 == want["last4"]
    assert parsed.upi_ref == want["ref"]
    assert parsed.txn_date == want["day"]
    if want["payee"]:
        assert parsed.payee == want["payee"]


def test_parse_sms_ignores_otp():
    assert parse_sms("Your OTP for Rs.500 txn is 123456") is None


def test_narration_details():
    assert narration_details("UPI-SWIGGY-swiggy@icici-ICIC0000103-429183746521-PAYMENT") == ("swiggy@icici", "429183746521")
    payee, ref = narration_details("UPI/DR/430099887766/ZOMATO LTD/YESB/Payment")
    assert ref == "430099887766" and payee == "ZOMATO LTD"
    assert narration_details("POS 416021XXXXXX4417 DMART AVENUE CHENNAI")[0] is not None
