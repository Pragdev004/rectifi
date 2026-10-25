"""Masking. Anything that goes to an outside model (DeepSeek, Claude Desktop) passes through here first.

Rule: account, card, UPI-reference and phone numbers keep only their last 4 digits.
Amounts, dates and short codes like 'XX1234' are left alone, because the model needs them.
The database keeps the real text. Only the copy that leaves for a model is masked.
"""

from __future__ import annotations

import re

# 4111 1111 1111 1111  or  4111-1111-1111-1111
_CARD = re.compile(r"(?<![\w])(\d{4})([ -])(\d{4})\2(\d{4})\2(\d{3,7})(?![\w])")
# one long run of digits (or X / * masking characters) ending in 4 digits
_LONG = re.compile(r"(?<![\w])([\dXx*]{5,}\d{4})(?![\w])")


def mask_sensitive(text: str) -> str:
    """Account/card/UPI-ref/phone numbers -> keep last 4. Amounts, dates and 'XX1234' untouched."""
    if not text:
        return text
    text = _CARD.sub(lambda m: f"XXXX{m[2]}XXXX{m[2]}XXXX{m[2]}{m[5]}", text)

    def repl(m: re.Match) -> str:
        s = m[1]
        # Fewer than 9 digits and no mask characters: that is an amount like 12500000, not an account.
        if sum(c.isdigit() for c in s) < 9 and not re.search(r"[Xx*]", s):
            return s
        return "X" * (len(s) - 4) + s[-4:]

    return _LONG.sub(repl, text)
