"""Live: a real Ollama model writes the chart code. Skipped unless you run `pytest -m ollama`.
It measures; it does not demand a pass rate, because small models differ. `python -m rectifi.evals.charts --live` runs all cases."""

import pytest

from rectifi.evals import charts as evals

pytestmark = pytest.mark.ollama


@pytest.fixture(autouse=True)
def data():
    from rectifi import db

    db.init_db()
    evals.seed_standard()


@pytest.mark.parametrize("case_id", ["monthly-food-bar", "top5-payees", "adv-fake-total"])
def test_the_real_model_draws_or_is_refused(case_id):
    from rectifi.agent.llm import get_llm

    case = {c["id"]: c for c in evals.load_cases()}[case_id]
    result = evals.run_case(case, get_llm("code"))
    print(result)
    if case["expect"].get("adversarial"):
        assert result["ok"], result                    # the made-up figure must never reach the picture
    assert result["attempts"] <= 3
