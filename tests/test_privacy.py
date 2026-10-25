import pytest

from rectifi.privacy import mask_sensitive


@pytest.mark.parametrize("raw, masked", [
    ("A/c 0012345678901 debited", "A/c XXXXXXXXX8901 debited"),
    ("POS 416021XXXXXX4417 DMART", "POS XXXXXXXXXXXX4417 DMART"),
    ("card 4111 1111 1111 1111", "card XXXX XXXX XXXX 1111"),
    ("UPI Ref No 429183746521.", "UPI Ref No XXXXXXXX6521."),
    ("phone 9876543210", "phone XXXXXX3210"),
])
def test_mask_sensitive(raw, masked):
    assert mask_sensitive(raw) == masked


@pytest.mark.parametrize("text", [
    "spent 1,25,000.50 on 21-10-2026",
    "Rs.12500000",
    "A/c XX1234",
    "Avl Bal INR 19,750.00",
])
def test_masking_keeps_amounts(text):
    assert mask_sensitive(text) == text


def test_mask_handles_empty():
    assert mask_sensitive("") == ""
    assert mask_sensitive(None) is None
