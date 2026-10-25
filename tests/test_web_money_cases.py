"""web/src/lib/money.cases.json is checked from both sides: here against Python's fmt_inr, and by vitest against money.ts."""

import json
from pathlib import Path

import pytest

from rectifi.money import fmt_inr

CASES = json.loads((Path(__file__).resolve().parent.parent / "web" / "src" / "lib" / "money.cases.json").read_text(encoding="utf-8"))


def test_there_are_enough_cases():
    assert len(CASES) >= 25
    assert any(not c["paise"] for c in CASES) and any(c["sign"] for c in CASES) and any(c["value"].startswith("-") for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['value']}-p{int(c['paise'])}-s{int(c['sign'])}")
def test_python_agrees_with_the_cases_file(case):
    assert fmt_inr(case["value"], paise=case["paise"], sign=case["sign"]) == case["expected"]
