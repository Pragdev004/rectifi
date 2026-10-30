"""Score the classifier on hand-written fake Indian narrations (evals/classify_cases.json, both directions).

    python -m rectifi.evals classify          the fixed layers only (rules, patterns); needs the database for the rules
    python -m rectifi.evals classify --live   also asks the model for what the fixed layers could not place

The report has: how much each layer covers and how often it is right, macro-F1 over the gold categories, a confusion
matrix, the share sent to Miscellaneous, and (with a model) a sweep of the confidence threshold from 0.5 to 0.9 that picks the
lowest threshold where the model's precision is at least 0.9. Self-reported confidence from small models is poorly
calibrated, so set RECTIFI_CLASSIFY_APPLY_MIN from this sweep, not from a guess.

The fixed layers are scored with built-in rules only and no look-alike index, so your own corrections never change the score.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from .. import categorize, config, tools
from ..db import connect
from ..parsing import narration_details

CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "classify_cases.json"
SWEEP = [x / 100 for x in range(50, 95, 5)]
PRECISION_TARGET = 0.9


def load_cases(path: Path | None = None) -> list[dict]:
    cases = json.loads((path or CASES_PATH).read_text(encoding="utf-8"))
    for c in cases:
        c.setdefault("payee", narration_details(c["narration"])[0])
    return cases


def fixed_context(conn) -> categorize.Context:
    """Built-in rules only, no own-account digits, no look-alike index: the same score on every laptop."""
    ctx = categorize.load_context(conn, with_index=False)
    ctx.rules = [r for r in ctx.rules if r[2] == "default"]
    ctx.own_last4 = []
    return ctx


def macro_f1(pairs: list[tuple[str, str]]) -> float:
    """Mean F1 over every category that appears as gold. pairs: (gold, predicted)."""
    f1s = []
    for label in sorted({g for g, _ in pairs}):
        tp = sum(1 for g, p in pairs if g == label and p == label)
        fp = sum(1 for g, p in pairs if g != label and p == label)
        fn = sum(1 for g, p in pairs if g == label and p != label)
        f1s.append(0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn))
    return round(sum(f1s) / len(f1s), 4) if f1s else 0.0


def ask_model(cases: list[dict], llm) -> dict[int, tuple[str, float]]:
    """Ask the model about the given cases (indexes into the list) the way the real job does. Returns {index: (category, 0-1)}."""
    from ..agent import classify as live

    answers: dict[int, tuple[str, float]] = {}
    empty = categorize.NgramIndex([])
    for direction in ("debit", "credit"):
        mine = [(i, c) for i, c in cases if c["direction"] == direction]
        names = tools.category_names_for(direction)
        for start in range(0, len(mine), config.CLASSIFY_BATCH_SIZE):
            chunk = mine[start:start + config.CLASSIFY_BATCH_SIZE]
            groups = [{"key": categorize.merchant_key(c["payee"], c["narration"]), "payee": c["payee"], "description": c["narration"],
                       "amount": "100.00"} for _, c in chunk]
            try:
                labels = live.ask_model(llm, direction, names, live._row_lines(groups, empty, direction))
            except (live.ModelUnavailable, ValueError):
                continue
            seen = Counter(label.i for label in labels)
            for label in labels:
                if seen[label.i] == 1 and 1 <= label.i <= len(chunk):
                    value = float(label.confidence)
                    answers[chunk[label.i - 1][0]] = (label.category, value / 100 if value > 1 else value)
    return answers


def evaluate(cases: list[dict], llm=None) -> dict:
    """Run the cases. `llm` (optional) is asked about what the fixed layers could not place."""
    with connect() as conn:
        ctx = fixed_context(conn)
    fixed = [categorize.decide_with(ctx, c["payee"], c["narration"], c["direction"]) for c in cases]
    final = [d.category for d in fixed]
    answers: dict[int, tuple[str, float]] = {}
    if llm is not None:
        todo = [(i, c) for i, c in enumerate(cases) if fixed[i].source == "fallback"]
        answers = ask_model(todo, llm)
        for i, (category, conf) in answers.items():
            if category not in categorize.FALLBACK_NAMES and round(conf * 100) >= config.CLASSIFY_APPLY_MIN:
                final[i] = category

    layers: dict[str, dict] = {}
    for source in categorize.SOURCES:
        idx = [i for i, d in enumerate(fixed) if d.source == source]
        if source == "llm":
            idx = [i for i in answers if final[i] == answers[i][0] and fixed[i].source == "fallback"]
        if idx:
            right = sum(1 for i in idx if (final[i] if source == "llm" else fixed[i].category) == cases[i]["gold"])
            layers[source] = {"rows": len(idx), "coverage": round(len(idx) / len(cases), 4), "accuracy": round(right / len(idx), 4)}
    pairs = [(c["gold"], final[i]) for i, c in enumerate(cases)]
    confusion: dict[str, dict[str, int]] = defaultdict(dict)
    for gold, pred in pairs:
        if gold != pred:
            confusion[gold][pred] = confusion[gold].get(pred, 0) + 1
    misc = sum(1 for p in final if p in categorize.FALLBACK_NAMES)
    report = {
        "cases": len(cases), "accuracy": round(sum(1 for g, p in pairs if g == p) / len(cases), 4), "macro_f1": macro_f1(pairs),
        "layers": layers, "to_miscellaneous": round(misc / len(cases), 4), "confusion": {k: dict(v) for k, v in confusion.items()},
        "model": None,
    }
    if llm is not None:
        report["model"] = sweep(cases, answers)
    return report


def sweep(cases: list[dict], answers: dict[int, tuple[str, float]]) -> dict:
    """Precision and coverage of the model at each threshold; picks the lowest threshold with precision >= 0.9."""
    rows, chosen = [], None
    picked = {i: a for i, a in answers.items() if a[0] not in categorize.FALLBACK_NAMES}
    for t in SWEEP:
        sure = [i for i, (_, conf) in picked.items() if conf >= t - 1e-9]
        right = sum(1 for i in sure if picked[i][0] == cases[i]["gold"])
        precision = round(right / len(sure), 4) if sure else None
        rows.append({"threshold": t, "answered": len(sure), "precision": precision})
        if chosen is None and precision is not None and precision >= PRECISION_TARGET:
            chosen = t
    return {"asked": len(answers), "sweep": rows, "suggested_apply_min": round(chosen * 100) if chosen is not None else None}


def format_report(report: dict) -> str:
    lines = [f"{report['cases']} cases · accuracy {report['accuracy']:.1%} · macro-F1 {report['macro_f1']:.3f} · "
             f"{report['to_miscellaneous']:.1%} sent to Miscellaneous", "", "layer       rows  coverage  accuracy"]
    for name, row in report["layers"].items():
        lines.append(f"{name:<10} {row['rows']:>5}  {row['coverage']:>7.1%}  {row['accuracy']:>8.1%}")
    if report["confusion"]:
        lines += ["", "wrong answers (gold -> what Rectifi said):"]
        for gold, wrong in sorted(report["confusion"].items()):
            lines.append(f"  {gold}: " + ", ".join(f"{p} x{n}" for p, n in sorted(wrong.items())))
    model = report.get("model")
    if model:
        lines += ["", f"model asked about {model['asked']} cases", "threshold  answered  precision"]
        lines += [f"{r['threshold']:>9.2f}  {r['answered']:>8}  " + (f"{r['precision']:.1%}" if r["precision"] is not None else "-")
                  for r in model["sweep"]]
        pick = model["suggested_apply_min"]
        lines.append(f"set RECTIFI_CLASSIFY_APPLY_MIN={pick}" if pick is not None else "no threshold reaches 90% precision: use a bigger model")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import sys

    args = sys.argv[1:] if argv is None else argv
    llm = None
    if "--live" in args:
        from ..agent.llm import get_llm, llm_status

        ok, why = llm_status()
        if not ok:
            print(f"Model is not ready: {why}")
            return 2
        llm = get_llm("classify")
    from ..db import init_db

    init_db()
    print(format_report(evaluate(load_cases(), llm)))
    return 0
