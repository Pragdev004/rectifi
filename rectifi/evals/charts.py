"""Score the chart harness on about 20 requests (evals/chart_cases.json).

    python -m rectifi.evals.charts            scripted check that the runner works (no model, uses canned code)
    python -m rectifi.evals.charts --live     asks the real model (RECTIFI_MODEL_CODE, else the chat model) and prints the pass rate

Run it only on a database whose name ends in _test or _demo: it fills the database with fake spending first.
Each case says what a correct chart has: the kind of mark (bar, line, wedge), how many bars or points, which ₹ figure must be
in the text (a total Rectifi worked out), attempts at most 3, time at most 120 s. An adversarial case passes when the harness
refused or the chart came out clean: the forbidden text must not appear anywhere in it.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .. import config, db, tools
from ..charts import checks as checks_module

CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "chart_cases.json"


def load_cases(path: Path | None = None) -> list[dict]:
    return json.loads((path or CASES_PATH).read_text(encoding="utf-8"))


def seed_standard() -> None:
    """Three months of fake, repeatable spending plus a salary and two budgets. Refuses a database that isn't a test or demo one."""
    name = db.describe_target(config.database_url())
    if not name.endswith(("_test", "_demo")):
        raise SystemExit(f"Refusing to fill {name} with fake data: use a database whose name ends in _test or _demo.")
    today = date.today()
    first = today.replace(day=1)
    months = [first]
    for _ in range(2):
        months.append((months[-1] - timedelta(days=1)).replace(day=1))
    plan = [("Food & Dining", ("Swiggy", "Zomato", "Cafe Coffee"), (310, 540, 220)), ("Transport", ("Uber", "Metro"), (180, 60)),
            ("Shopping", ("Amazon", "Myntra"), (1499, 899)), ("Entertainment", ("Netflix",), (649,)), ("Bills & Utilities", ("Airtel",), (399,))]
    for month in months:
        day = 1
        for category, payees, amounts in plan:
            for payee, amount in zip(payees, amounts):
                for repeat in range(2):
                    day = day % 27 + 1
                    d = min(month.replace(day=day), today)
                    tools.add_transaction(str(amount + repeat * 17), "debit", d.isoformat(), payee=payee, category=category, source="manual")
        tools.add_transaction("65000", "credit", min(month, today).isoformat(), payee="Employer", category="Salary & Allowance", source="manual")
    tools.set_budget("Food & Dining", "6000", first)
    tools.set_budget("Transport", "2000", first)


def _texts(info: dict) -> list[str]:
    return [t for _, t in checks_module.claimed_texts(info)] + [x for ax in info.get("axes", []) for x in ax.get("xticks", []) + ax.get("yticks", [])]


def _reference_value(name: str, start: date, end: date) -> Decimal | None:
    ref = tools.chart_reference_totals(start, end)
    if name == "spending.overall":
        return ref["spending"]["overall"]
    if name == "budget.food":
        return sum((v for (cat, _m), v in ref["budget_limits"]["by_category_month"].items() if cat == "Food & Dining"), Decimal(0))
    return None


def judge(case: dict, state: dict, seconds: float) -> dict:
    """Compare one finished run with what the case expects. Returns {"id", "ok", "attempts", "seconds", "failures": [...]}."""
    expect = case["expect"]
    failures: list[str] = []
    info = state.get("info") or {}
    ok = bool(state.get("ok"))
    attempts = state.get("attempts", 0)
    if expect.get("outcome") == "ok_or_refused":
        text = "\n".join(_texts(info)) if ok else ""
        failures += [f"forbidden text {bad!r} appears in the chart" for bad in expect.get("forbidden_text", []) if bad in text]
    else:
        if not ok:
            failures.append(f"no chart: {state.get('error')}")
        else:
            artists = [a for ax in info.get("axes", []) for a in ax.get("artists", [])]
            kinds = {a["type"] for a in artists}
            if expect.get("kind") and expect["kind"] not in kinds:
                failures.append(f"expected a {expect['kind']} chart, drew {sorted(kinds) or 'nothing'}")
            n = sum(a["n"] for a in artists if a["type"] == expect.get("kind"))
            if "points" in expect and n != expect["points"]:
                failures.append(f"expected {expect['points']} marks, found {n}")
            if "min_points" in expect and n < expect["min_points"]:
                failures.append(f"expected at least {expect['min_points']} marks, found {n}")
            for word in expect.get("categories", []):
                if not any(word.lower() in t.lower() for t in _texts(info)):
                    failures.append(f"{word!r} is not on the chart")
            if expect.get("reference"):
                start, end = date.fromisoformat(state["start"]), date.fromisoformat(state["end"])
                wanted = _reference_value(expect["reference"], start, end)
                shown = [v for t in _texts(info) for _, v in checks_module.figures_in(t) if v is not None]
                if wanted is None or not any(checks_module._matches(v, wanted) for v in shown):
                    failures.append(f"the figure for {expect['reference']} is not in the chart's text")
    if attempts > expect.get("max_attempts", 3):
        failures.append(f"{attempts} attempts")
    if seconds > expect.get("max_seconds", 120):
        failures.append(f"{seconds:.0f} s")
    return {"id": case["id"], "ok": not failures, "attempts": attempts, "seconds": round(seconds, 1), "failures": failures}


def run_case(case: dict, llm) -> dict:
    from langgraph.checkpoint.memory import InMemorySaver

    from ..agent import contracts

    graph = contracts.build_chart_subgraph(llm=llm, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": f"chart-{uuid.uuid4()}"}}
    started = time.monotonic()
    try:
        graph.invoke({"request": case["request"], "period": case["period"], "review": False}, cfg)
    except Exception as exc:                          # noqa: BLE001 - a crash is a failed case, not a stopped eval
        return {"id": case["id"], "ok": False, "attempts": 0, "seconds": round(time.monotonic() - started, 1), "failures": [f"{type(exc).__name__}: {exc}"[:200]]}
    return judge(case, graph.get_state(cfg).values, time.monotonic() - started)


def run_all(cases: list[dict], llm_for_case) -> dict:
    """`llm_for_case(case)` returns the model for that case (the live model, or a scripted one in tests)."""
    results = [run_case(c, llm_for_case(c)) for c in cases]
    passed = sum(r["ok"] for r in results)
    return {"passed": passed, "total": len(results), "rate": round(passed / len(results), 2) if results else 0.0, "cases": results}


def main(argv: list[str]) -> int:
    live = "--live" in argv
    only = next((a.split("=", 1)[1].split(",") for a in argv if a.startswith("--cases=")), None)
    cases = [c for c in load_cases() if not only or c["id"] in only]
    db.init_db()
    seed_standard()
    if not live:
        print("Dry run: add --live to ask the model. (The scripted checks are in tests/test_chart_graph.py.)")
        return 0
    from ..agent.llm import get_llm

    llm = get_llm("code")
    report = run_all(cases, lambda _c: llm)
    for r in report["cases"]:
        print(("PASS " if r["ok"] else "FAIL ") + f"{r['id']:<24} attempts={r['attempts']} {r['seconds']}s " + "; ".join(r["failures"]))
    print(f"\nPass rate: {report['passed']}/{report['total']} = {report['rate']:.0%} with model {config.role_model('code')}")
    return 0 if report["rate"] >= 0.5 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
