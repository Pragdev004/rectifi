"""Evaluate the knowledge graph: the same questions through plain RAG, CRAG, Self-RAG and Full, one table.

    python -m rectifi.evals.rag --live                 # real Ollama models, the library indexed first (takes a while)
    python -m rectifi.evals.rag --live --modes rag,full --limit 10 --json /tmp/rag-eval.json

The question set is evals/rag_questions.json (30 questions: 12 answerable from a guide, 6 about your own numbers, 6 out of scope,
6 adversarial). pytest runs this same code with scripted fake models (tests/test_rag_eval.py) to prove the metrics are computed
correctly; only `--live` says anything about real model quality.

Metrics, per mode:
    hit@6        a guide question's gold guide is among the first 6 retrieved passages
    tool_hit     a data question used the gold read-only tool (only modes that route: self_rag, full)
    grounded     of the answers given: no untraced ₹ figure AND the grader said "fully" (when the mode has a grader) AND the
                 required words are there AND the forbidden words are not
    useful       of the questions that should be answered: answered, the grader did not say "not useful", required words present
    abstain      the app abstained exactly when it should (out-of-scope questions)
    routing      the route matched what was expected (only modes that route)
    p50 / p95    seconds per question
Small-model graders are noisy. These numbers measure how noisy; they do not prove an answer is right.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from pathlib import Path

QUESTIONS_PATH = Path(__file__).resolve().parents[2] / "evals" / "rag_questions.json"
MODES = ("rag", "crag", "self_rag", "full")
ROUTING_MODES = ("self_rag", "full", "auto")
REFLECT_MODES = ("self_rag", "full", "auto")
ANSWERABLE = ("guide", "data", "adversarial")


def load_questions(path: Path | str | None = None, kinds: tuple[str, ...] | None = None, limit: int | None = None) -> list[dict]:
    data = json.loads(Path(path or QUESTIONS_PATH).read_text(encoding="utf-8"))
    qs = [q for q in data["questions"] if not kinds or q["kind"] in kinds]
    return qs[:limit] if limit else qs


def _has_any(text: str, words: list[str]) -> bool:
    low = text.lower()
    return not words or any(w.lower() in low for w in words)


def _has_none(text: str, words: list[str]) -> bool:
    low = text.lower()
    return not any(w.lower() in low for w in words)


def run_question(graph, q: dict, mode: str) -> dict:
    """One question through one graph. Returns a flat record; never raises (an error becomes record['error'])."""
    cfg = {"configurable": {"thread_id": f"eval-{uuid.uuid4().hex}"}, "recursion_limit": 80}
    started = time.perf_counter()
    rec = {"id": q["id"], "kind": q["kind"], "mode": mode, "error": None}
    try:
        out = graph.invoke({"question": q["question"], "mode": mode, "history": [], "uid": "eval"}, cfg)
        state = graph.get_state(cfg).values
    except Exception as exc:
        rec.update(error=f"{type(exc).__name__}: {exc}"[:200], seconds=time.perf_counter() - started, answer="", abstained=False, route=None,
                   unverified=[], support=None, useful=None, hit=None, tool_hit=None)
        return rec
    rec["seconds"] = time.perf_counter() - started
    answer = out.get("answer", "")
    sources = [d.metadata.get("source") for d in state.get("documents", [])[:6]]
    used_tools = {c["source"].split(":", 1)[1] for c in out.get("citations", []) if str(c.get("source", "")).startswith("tool:")}
    rec.update(answer=answer, abstained=bool(out.get("abstained")), route=out.get("route"), unverified=list(out.get("unverified", [])),
               support=out.get("support"), useful=out.get("useful"),
               hit=(any(s in sources for s in q.get("gold_sources", [])) if q["kind"] == "guide" else None),
               tool_hit=(bool(used_tools & set(q.get("gold_tools", []))) if q["kind"] == "data" else None))
    return rec


def judge(rec: dict, q: dict, mode: str) -> dict:
    """Add the pass/fail columns to a record."""
    answered = not rec["abstained"] and not rec["error"] and bool(rec["answer"])
    words_ok = _has_any(rec["answer"], q.get("must_any", [])) and _has_none(rec["answer"], q.get("forbid", []))
    grader_ok = rec["support"] in ("full", None) if mode in REFLECT_MODES else True
    rec["grounded"] = (answered and not rec["unverified"] and grader_ok and words_ok) if q["kind"] in ANSWERABLE else None
    rec["answered"] = answered
    rec["useful_ok"] = (answered and rec["useful"] is not False and _has_any(rec["answer"], q.get("must_any", []))) if q["kind"] in ANSWERABLE else None
    rec["abstain_ok"] = rec["abstained"] == bool(q.get("expect_abstain"))
    rec["routing_ok"] = (rec["route"] == q["expect_route"]) if q.get("expect_route") else None
    return rec


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def _rate(flags: list) -> float | None:
    flags = [f for f in flags if f is not None]
    return round(sum(1 for f in flags if f) / len(flags), 3) if flags else None


def summarize(records: list[dict], mode: str) -> dict:
    answered_ans = [r for r in records if r["kind"] in ANSWERABLE and r["answered"]]
    times = [r["seconds"] for r in records]
    return {
        "n": len(records), "errors": sum(1 for r in records if r["error"]),
        "hit@6": _rate([r["hit"] for r in records]),
        "tool_hit": _rate([r["tool_hit"] for r in records]) if mode in ROUTING_MODES else None,
        "grounded": _rate([r["grounded"] for r in answered_ans]),
        "useful": _rate([r["useful_ok"] for r in records]),
        "abstain": _rate([r["abstain_ok"] for r in records]),
        "routing": _rate([r["routing_ok"] for r in records]) if mode in ROUTING_MODES else None,
        "p50_s": None if not times else round(percentile(times, 50), 2), "p95_s": None if not times else round(percentile(times, 95), 2),
    }


def run_eval(graph, questions: list[dict], modes: tuple[str, ...] = MODES, progress=None) -> dict:
    """Every question through every mode. `graph` is the compiled graph from build_rag_subgraph."""
    records: list[dict] = []
    summary: dict = {}
    for mode in modes:
        mode_records = []
        for q in questions:
            rec = judge(run_question(graph, q, mode), q, mode)
            mode_records.append(rec)
            if progress:
                progress(mode, q["id"])
        records.extend(mode_records)
        summary[mode] = summarize(mode_records, mode)
    return {"summary": summary, "records": records}


def format_report(result: dict) -> str:
    cols = ["hit@6", "tool_hit", "grounded", "useful", "abstain", "routing", "p50_s", "p95_s"]
    head = f"{'mode':10}" + "".join(f"{c:>10}" for c in cols) + f"{'errors':>8}"
    lines = [head, "-" * len(head)]
    for mode, s in result["summary"].items():
        cells = "".join(f"{('n/a' if s[c] is None else s[c]):>10}" for c in cols)
        lines.append(f"{mode:10}{cells}{s['errors']:>8}")
    lines.append("")
    lines.append("Self-RAG here is prompt-based: a normal small model answers short structured questions. It is not the fine-tuned reflection-token model of the paper.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate plain RAG, CRAG, Self-RAG and Full on the question set.")
    parser.add_argument("--live", action="store_true", help="use the real Ollama models (otherwise this does nothing: pytest has the scripted run)")
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--json", default=None, help="also write the full records to this file")
    args = parser.parse_args(argv)
    if not args.live:
        print("Nothing to run. Use --live for real models. The scripted check is: python -m pytest tests/test_rag_eval.py")
        return 2
    from .. import config
    from ..agent.llm import get_llm
    from ..db import init_db
    from ..rag import index
    from ..rag.embeddings import get_rag_embeddings
    from ..rag.graph import build_rag_subgraph

    init_db()
    embeddings = get_rag_embeddings()
    print(f"Indexing the library with {config.embed_model()} ...")
    index.reindex(embeddings)
    graph = build_rag_subgraph(llms={"grade": get_llm("grade"), "chat": get_llm("chat")}, embeddings=embeddings)
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
    result = run_eval(graph, load_questions(limit=args.limit), modes, progress=lambda m, i: print(f"  {m} {i}", file=sys.stderr))
    print(format_report(result))
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
