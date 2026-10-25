"""The deterministic half of the chart harness: validate, run in the sandbox, check the figure. No model, no langchain.

`graph.py` calls these steps inside its nodes; `rerun_saved` calls them directly to redraw a pinned chart from its saved,
already-validated code on today's data. That path never touches a model (AI_PLAN B2.7).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import date

from .. import tools
from ..money import D0
from . import checks as checks_module
from . import sandbox
from .validate import validate


@dataclass
class Outcome:
    stage: str                                   # "validate" | "run" | "check" | "passed"
    ok: bool
    feedback: str = ""                           # plain words for the repair prompt
    verdict: str = ""                            # VALIDATION FAILED | ERROR | TIMEOUT | CHECK FAILED | PASSED
    detail: str = ""                             # one line for the person
    traceback: str = ""
    sandbox: sandbox.SandboxResult | None = None
    checks: dict = field(default_factory=dict)
    entries: int = 0


def b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def check_code(code: str, request: str) -> Outcome:
    result = validate(code, request=request)
    if result.ok:
        return Outcome("validate", True, verdict="PASSED", detail="The code passed the safety check.")
    return Outcome("validate", False, feedback="The code was refused before it ran:\n" + result.feedback(),
                   verdict="VALIDATION FAILED", detail=result.violations[0].message)


def run_code(code: str, start: date, end: date, *, period: str, request: str = "", timeout: float | None = None) -> Outcome:
    """Run already-validated code on the data for [start, end], then check the figure. Returns the first stage that failed, or passed."""
    frame = tools.chart_frame(start, end)
    ran = sandbox.run_chart(code, frame, period=period, timeout=timeout)
    entries = len(frame["transactions"])
    if not ran.ok:
        error = ran.error or {}
        where = f"\nWhere: {error['traceback']}" if error.get("traceback") else ""
        verdict = "TIMEOUT" if ran.status == "timeout" else "ERROR"
        text = f"{error.get('type', 'Error')}: {error.get('message', '')}".strip()
        hint = ""
        if ran.status == "memory":
            hint = " The chart used too much memory: draw fewer points or aggregate first."
        elif ran.status == "timeout":
            hint = " The chart took too long: aggregate with groupby before plotting."
        return Outcome("run", False, feedback=f"The code failed when it ran. {text}{where}{hint}", verdict=verdict, detail=text,
                       traceback=error.get("traceback", ""), sandbox=ran, entries=entries)
    reference = tools.chart_reference_totals(start, end)
    verdicts = checks_module.run_checks(ran.info, png=ran.png, reference=reference, request=request, warnings=ran.warnings, entries=entries)
    if not verdicts["ok"]:
        return Outcome("check", False, feedback="The chart ran, but the checks failed:\n- " + "\n- ".join(verdicts["failures"]),
                       verdict="CHECK FAILED", detail=verdicts["failures"][0], sandbox=ran, checks=verdicts, entries=entries)
    return Outcome("passed", True, verdict="PASSED", detail="Has data, labelled, figures match Rectifi's totals.", sandbox=ran, checks=verdicts, entries=entries)


def title_of(info: dict | None) -> str:
    info = info or {}
    if info.get("suptitle"):
        return info["suptitle"]
    for ax in info.get("axes", []):
        for key in ("title", "title_left", "title_right"):
            if ax.get(key):
                return ax[key]
    return "Chart"


def rerun_saved(chart_id: int) -> dict:
    """Redraw a saved chart from its stored code on the latest data. Validate, sandbox and checks run again; no model is called.
    Raises tools.RectifiError with a plain message when the saved code no longer passes (the saved image is left as it was)."""
    saved = tools.get_chart(chart_id, with_code=True, with_images=False)
    spec = (saved["checks"] or {}).get("period") or "last-3-months"
    start, end, label = tools.chart_period(spec)
    check = check_code(saved["code"], saved["prompt"])
    if not check.ok:
        raise tools.RectifiError("The saved code no longer passes the safety check: " + check.detail)
    outcome = run_code(saved["code"], start, end, period=label, request=saved["prompt"])
    if not outcome.ok:
        raise tools.RectifiError(f"Drawing it again failed ({outcome.verdict.lower()}): {outcome.detail}")
    checks = {**outcome.checks, "period": spec, "entries": outcome.entries}
    trace = list(saved["trace"] or []) + [{"attempt": 0, "step": "rerun", "verdict": "PASSED", "detail": f"Redrawn from saved code on {outcome.entries} entries."}]
    return tools.update_chart_run(chart_id, image_light=outcome.sandbox.png["light"], image_dark=outcome.sandbox.png["dark"],
                                  period_start=start, period_end=end, trace=trace[-30:], checks=checks)
