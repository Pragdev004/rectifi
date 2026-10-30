"""The classifier eval runner: loads the cases, scores the fixed layers, and scores a scripted model run."""

from support.fakes import ScriptedStructured

from rectifi import categorize
from rectifi.evals import classify as ev


def test_the_case_file_is_large_and_balanced():
    cases = ev.load_cases()
    assert len(cases) >= 150
    assert {c["direction"] for c in cases} == {"debit", "credit"}
    assert sum(1 for c in cases if c["gold"] == categorize.FALLBACK_EXPENSE) >= 10       # person-to-person rows
    assert all(set(c) >= {"narration", "direction", "gold", "payee"} for c in cases)


def test_the_fixed_layers_report():
    report = ev.evaluate(ev.load_cases())
    assert report["model"] is None
    assert {"rule", "heuristic", "fallback"} <= set(report["layers"])
    assert report["layers"]["rule"]["accuracy"] >= 0.95 and report["layers"]["heuristic"]["accuracy"] >= 0.9
    assert report["macro_f1"] > 0.5 and 0 < report["to_miscellaneous"] < 0.6
    assert "layer" in ev.format_report(report)


def test_a_scripted_model_run_is_scored_and_the_threshold_is_picked():
    cases = ev.load_cases()
    fixed = ev.evaluate(cases)
    waiting = sum(1 for _ in range(fixed["layers"]["fallback"]["rows"]))
    # an oracle that is sure when right and unsure when wrong
    from rectifi.db import connect
    with connect() as conn:
        ctx = ev.fixed_context(conn)
    todo = [c for c in cases if categorize.decide_with(ctx, c["payee"], c["narration"], c["direction"]).source == "fallback"]
    scripted = []
    for direction in ("debit", "credit"):
        mine = [c for c in todo if c["direction"] == direction]
        for start in range(0, len(mine), 12):
            chunk = mine[start:start + 12]
            scripted.append({"labels": [{"i": n, "category": c["gold"], "confidence": 0.95 if n % 2 else 0.55, "why": "oracle"}
                                        for n, c in enumerate(chunk, 1)]})
    report = ev.evaluate(cases, ScriptedStructured(structured=scripted))
    assert report["model"]["asked"] == len(todo) == waiting
    sweep = {r["threshold"]: r for r in report["model"]["sweep"]}
    assert sweep[0.5]["answered"] >= sweep[0.9]["answered"] > 0
    assert sweep[0.9]["precision"] == 1.0
    assert report["model"]["suggested_apply_min"] == 50
    assert report["accuracy"] > fixed["accuracy"]
    assert "set RECTIFI_CLASSIFY_APPLY_MIN=50" in ev.format_report(report)
