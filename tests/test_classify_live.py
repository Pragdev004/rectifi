"""Live check against a real Ollama (run with `pytest -m ollama`). Skipped in the normal run.

It asks the configured 'classify' model about a few unmistakable payments and checks the SHAPE of what comes back (the
answer is one of the allowed categories, the confidence is a percent, nothing but the pending rows changed). It does not
demand that a small model is always right: use `python -m rectifi.evals classify --live` to measure that."""

from datetime import date
from decimal import Decimal

import pytest

from rectifi import tools
from rectifi.agent import classify
from rectifi.agent.llm import get_llm, llm_status
from rectifi.importer import StatementRow, import_rows

pytestmark = pytest.mark.ollama


def test_the_real_model_answers_inside_the_enum():
    ok, why = llm_status()
    if not ok:
        pytest.skip(why)
    acc = tools.add_account("SBI", "1234")
    d = date(2026, 10, 1)
    rows = [StatementRow(d, Decimal("320.00"), "debit", "UPI/DR/111111111111/HOTEL SARAVANA BHAVAN/SBIN/saravana@ybl/Pay", None, None, 1),
            StatementRow(d, Decimal("900.00"), "debit", "UPI/DR/222222222222/RAHUL SHARMA/HDFC/rahul@okhdfc/Pay", None, None, 2)]
    batch = import_rows(rows, acc["id"], "live.csv").batch_id
    out = classify.classify_pending(batch, llm=get_llm("classify"))
    assert out["status"] in ("done", "failed")
    allowed = set(tools.category_names_for("debit"))
    for r in tools.batch_rows(batch):
        assert r["category"] in allowed
        if r["category_source"] == "llm":
            assert 0 <= r["category_confidence"] <= 100
