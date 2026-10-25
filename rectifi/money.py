"""Money helpers. Every rupee in Rectifi is a Decimal — never a float.

Why: 0.1 + 0.2 == 0.30000000000000004 in floating point. Over a year of
transactions those crumbs add up and reconciliation never hits exactly ₹0.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

D0 = Decimal("0.00")
PAISE = Decimal("0.01")

_CLEAN = re.compile(r"(?i)(₹|rs\.?|inr|\s|,)")


def parse_amount(value) -> Decimal:
    """Turn '₹1,25,000.50', 'Rs. 250', '250', 250 or Decimal('250') into Decimal('…') rounded to paise.

    Raises ValueError for anything that isn't a plain positive-or-zero number.
    """
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, float):
        # Floats only arrive from libraries like pandas; go through str() so 0.1 stays 0.1.
        amount = Decimal(str(value))
    elif isinstance(value, str):
        cleaned = _CLEAN.sub("", value.strip())
        if cleaned.endswith("/-"):
            cleaned = cleaned[:-2]
        if not cleaned:
            raise ValueError(f"Empty amount: {value!r}")
        try:
            amount = Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValueError(f"Not an amount: {value!r}") from exc
    else:
        raise ValueError(f"Unsupported amount type: {type(value).__name__}")
    if not amount.is_finite():
        raise ValueError(f"Not a finite amount: {value!r}")
    return amount.quantize(PAISE, rounding=ROUND_HALF_UP)


def group_indian(integer_digits: str) -> str:
    """'12500000' -> '1,25,00,000' (Indian lakh/crore grouping)."""
    if len(integer_digits) <= 3:
        return integer_digits
    head, tail = integer_digits[:-3], integer_digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def fmt_inr(amount, *, paise: bool = True, sign: bool = False) -> str:
    """Decimal('125000.5') -> '₹1,25,000.50'. sign=True prefixes + for positive values."""
    value = parse_amount(amount) if not isinstance(amount, Decimal) else amount.quantize(PAISE)
    negative = value < 0
    value = abs(value)
    integer, _, fraction = f"{value:.2f}".partition(".")
    text = "₹" + group_indian(integer)
    if paise:
        text += "." + fraction
    if negative:
        return "−" + text
    if sign and value > 0:
        return "+" + text
    return text


_NUMBER_IN_TEXT = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d{1,2})?)\s*(k|K|lakh|lakhs|L)?(?![\w])")


def amounts_in_text(text: str) -> set[Decimal]:
    """Every number a human typed, as Decimals. '2.5k' -> 2500, '1 lakh' -> 100000, '₹1,250' -> 1250.

    Used by the agent guard: an amount the LLM wants to save must be one of these.
    """
    found: set[Decimal] = set()
    text = re.sub(r"(?i)(rs\.?|inr|₹)", " ", text or "")  # "Rs.250" -> " 250"
    for raw, suffix in _NUMBER_IN_TEXT.findall(text):
        try:
            base = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        if suffix in ("k", "K"):
            base *= 1000
        elif suffix in ("lakh", "lakhs", "L"):
            base *= 100000
        found.add(base.quantize(PAISE))
    return found
