"""The chart harness as a LangGraph (AI_PLAN B2.2). The only module in rectifi/charts that loads langchain.

    prepare -> write_code -> validate -+-> review_gate (only if the person ticked "read the code first") -> execute
                  ^                    |                                                                     |
                  |                    +-fail-> repair                       check_figure <-----------------+
                  +-------- repair <--------fail------------------------------+       |  pass -> finish (saves the chart)
    repair adds 1 to `attempts`; at RECTIFI_CHART_ATTEMPTS the run ends in give_up.

The model writes code. It never sees a real row, and it never decides a number: the validator refuses typed amounts and the
figure check refuses a rupee figure that isn't in Rectifi's records (CLAUDE.md rule 1). Money crosses to the model nowhere.
Checkpointed (thread_id "chart-<uuid>") so the review gate can pause and "Try again from attempt N" can fork the history.
"""

from __future__ import annotations

import operator
import uuid
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.output_parsers import StrOutputParser
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, RetryPolicy, interrupt

from .. import config, tools
from ..agent.contracts import ChartInput, ChartOutput
from ..privacy import mask_sensitive
from . import prompts, run
from .validate import extract_code

RETRY_INTERVAL = 0.5          # tests set this to 0


class ChartState(TypedDict, total=False):
    request: str
    period: str
    review: bool
    reviewed: bool
    start: str
    end: str
    label: str
    categories: list
    code: str
    attempts: int
    feedback: str                 # extra words from the person ("Try again from here")
    problem: str                  # why the last attempt failed, in plain words for the repair prompt
    stage: str
    png_light: str
    png_dark: str
    info: dict
    checks: dict
    entries: int
    route: str
    trace: Annotated[list, operator.add]
    chart_id: int | None
    ok: bool
    error: str | None
    model: str


def _retry_on(exc: Exception) -> bool:
    return not isinstance(exc, (ValueError, TypeError, KeyError, AttributeError, NotImplementedError, tools.RectifiError))


def _retry() -> RetryPolicy:
    return RetryPolicy(initial_interval=RETRY_INTERVAL, backoff_factor=2.0, max_attempts=3, jitter=False, retry_on=_retry_on)


def _emit(step: str, verdict: str, detail: str = "", attempt: int = 0, **extra) -> dict:
    """Tell the page (custom stream) and return the trace row to store."""
    event = {"step": step, "verdict": verdict, "detail": detail[:300], "attempt": attempt, **extra}
    try:
        get_stream_writer()(event)
    except Exception:            # noqa: BLE001 - no streaming consumer is fine
        pass
    return event


def _model_name(llm) -> str:
    for attr in ("model", "model_name", "_llm_type"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def build_chart_subgraph(*, llm, checkpointer=None):
    """Input ChartInput {request, period, review}; output ChartOutput {chart_id, ok, trace, error}."""
    parser = StrOutputParser()
    model_name = _model_name(llm)

    def prepare(state: ChartState) -> dict:
        start, end, label = tools.chart_period(state.get("period") or "last-3-months")
        names = [c["name"] for c in tools.list_categories()]
        _emit("prepare", "ok", f"Period: {label}", 0)
        return {"start": start.isoformat(), "end": end.isoformat(), "label": label, "categories": names, "attempts": 0, "reviewed": False,
                "ok": False, "chart_id": None, "error": None, "model": model_name, "route": "write"}

    def _prompt_vars(state: ChartState) -> dict:
        extra = state.get("feedback") or ""
        return {"system": prompts.system_text(state["categories"], state["label"]), "request": mask_sensitive(state["request"]),
                "feedback": (f"The person added: {mask_sensitive(extra)}\n\n" if extra else "")}

    def write_code(state: ChartState) -> dict:
        _emit("write", "working", "Writing code…", 1)
        text = (prompts.WRITE_PROMPT | llm | parser).invoke(_prompt_vars(state))
        code = extract_code(text)
        row = _emit("write", "ok", f"Wrote {len(code.splitlines())} lines.", 1)
        return {"code": code, "attempts": 1, "feedback": "", "trace": [row]}

    def repair(state: ChartState) -> dict:
        attempt = state["attempts"] + 1
        _emit("repair", "working", "Fixing the code…", attempt)
        text = (prompts.REPAIR_PROMPT | llm | parser).invoke({
            **_prompt_vars(state), "code": state["code"], "problem": mask_sensitive(state.get("problem", ""))[:1500] + "\n"})
        code = extract_code(text)
        row = _emit("repair", "ok", "Wrote a new version.", attempt)
        return {"code": code, "attempts": attempt, "feedback": "", "trace": [row]}

    def validate_node(state: ChartState) -> dict:
        n = state["attempts"]
        _emit("validate", "working", "Checking the code…", n)
        outcome = run.check_code(state["code"], state["request"])
        row = _emit("validate", outcome.verdict, outcome.detail, n)
        if not outcome.ok:
            return {"problem": outcome.feedback, "trace": [row], "route": "fail", "stage": "validate"}
        return {"trace": [row], "route": "review" if state.get("review") and not state.get("reviewed") else "run", "stage": "validate"}

    def review_gate(state: ChartState) -> Command[Literal["validate", "execute", "give_up"]]:
        _emit("review", "waiting", "Waiting for you to read the code.", state["attempts"])
        answer = interrupt({"kind": "review_code", "code": state["code"], "attempt": state["attempts"]})
        if isinstance(answer, bool):                # the chat window answers yes / no; the Charts page sends {"run": true, "code": "..."}
            answer = {"run": answer}
        answer = answer if isinstance(answer, dict) else {}
        if not answer.get("run"):
            row = _emit("review", "CANCELLED", "You chose not to run the code.", state["attempts"])
            return Command(goto="give_up", update={"trace": [row], "error": "You cancelled before the code ran.", "stage": "cancelled"})
        edited = answer.get("code")
        if isinstance(edited, str) and edited.strip() and edited.strip() != state["code"].strip():
            row = _emit("review", "EDITED", "You edited the code; checking it again.", state["attempts"])
            return Command(goto="validate", update={"code": edited, "reviewed": True, "trace": [row]})
        row = _emit("review", "APPROVED", "You approved the code.", state["attempts"])
        return Command(goto="execute", update={"reviewed": True, "trace": [row]})

    def execute(state: ChartState) -> dict:
        n = state["attempts"]
        _emit("run", "working", "Running…", n)
        from datetime import date

        outcome = run.run_code(state["code"], date.fromisoformat(state["start"]), date.fromisoformat(state["end"]), period=state["label"],
                               request=state["request"])
        if outcome.stage == "run":
            row = _emit("run", outcome.verdict, outcome.detail, n, traceback=outcome.traceback)
            return {"problem": outcome.feedback, "trace": [row], "route": "fail", "stage": "run"}
        row = _emit("run", "RAN", "The code ran.", n)
        update: dict[str, Any] = {"trace": [row], "stage": "run", "entries": outcome.entries,
                                  "png_light": run.b64(outcome.sandbox.png["light"]), "png_dark": run.b64(outcome.sandbox.png["dark"]), "info": outcome.sandbox.info}
        _emit("check", "working", "Checking the figure…", n)
        if outcome.ok:
            update.update(checks=outcome.checks, route="done", trace=[row, _emit("check", "PASSED", outcome.detail, n)])
        else:
            update.update(checks=outcome.checks, problem=outcome.feedback, route="fail", stage="check",
                          trace=[row, _emit("check", "CHECK FAILED", outcome.detail, n)])
        return update

    def finish(state: ChartState) -> dict:
        import base64
        from datetime import date

        title = run.title_of(state.get("info"))
        checks = {**(state.get("checks") or {}), "period": state.get("period") or "last-3-months", "entries": state.get("entries", 0)}
        chart_id = tools.save_chart(
            title=title, prompt=state["request"], code=state["code"], image_light=base64.b64decode(state["png_light"]),
            image_dark=base64.b64decode(state["png_dark"]), period_start=date.fromisoformat(state["start"]), period_end=date.fromisoformat(state["end"]),
            model=state.get("model"), attempts=state["attempts"], trace=state.get("trace", []) + [], checks=checks, thread_id=None)
        row = _emit("done", "PASSED", f"Saved as chart {chart_id}.", state["attempts"], chart_id=chart_id)
        return {"chart_id": chart_id, "ok": True, "error": None, "trace": [row], "png_light": "", "png_dark": ""}

    def give_up(state: ChartState) -> dict:
        reason = state.get("error") or f"I couldn't draw it in {state.get('attempts', 0)} tries. Last problem: {(state.get('problem') or '').splitlines()[-1][:200] if state.get('problem') else 'unknown'}"
        row = _emit("give_up", "FAILED", reason, state.get("attempts", 0))
        return {"ok": False, "chart_id": None, "error": reason, "trace": [row], "png_light": "", "png_dark": ""}

    def after_fail(state: ChartState) -> str:
        return "give_up" if state["attempts"] >= config.CHART_MAX_ATTEMPTS else "repair"

    def route_validate(state: ChartState) -> str:
        return {"fail": after_fail(state), "review": "review_gate", "run": "execute"}[state["route"]]

    def route_execute(state: ChartState) -> str:
        return "finish" if state["route"] == "done" else after_fail(state)

    g = StateGraph(ChartState, input_schema=ChartInput, output_schema=ChartOutput)
    g.add_node("prepare", prepare)
    g.add_node("write_code", write_code, retry_policy=_retry())
    g.add_node("validate", validate_node)
    g.add_node("repair", repair, retry_policy=_retry())
    g.add_node("review_gate", review_gate)
    g.add_node("execute", execute)
    g.add_node("finish", finish)
    g.add_node("give_up", give_up)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "write_code")
    g.add_edge("write_code", "validate")
    g.add_edge("repair", "validate")
    g.add_conditional_edges("validate", route_validate, ["repair", "review_gate", "execute", "give_up"])
    g.add_conditional_edges("execute", route_execute, ["finish", "repair", "give_up"])
    g.add_edge("finish", END)
    g.add_edge("give_up", END)
    return g.compile(checkpointer=checkpointer)


def new_thread_id() -> str:
    return f"chart-{uuid.uuid4()}"


def retry_from_attempt(graph, thread_id: str, attempt: int, feedback: str = "") -> dict:
    """Time travel: fork the run at the checkpoint just before attempt `attempt` was written, add the person's feedback, and run on.
    Returns the new graph input config to pass to graph.stream(None, config). Raises ValueError if that attempt has no checkpoint."""
    base = {"configurable": {"thread_id": thread_id}}
    target = None
    for snap in graph.get_state_history(base):          # newest first
        values = snap.values or {}
        if snap.next and snap.next[0] in ("write_code", "repair") and values.get("attempts", 0) == attempt - 1:
            target = snap
            break
    if target is None:
        raise ValueError(f"There is no checkpoint before attempt {attempt}.")
    # Pretend the node that produced this checkpoint just ran, so the graph carries on from the same edge.
    as_node = "prepare" if target.next[0] == "write_code" else ("validate" if (target.values or {}).get("stage") == "validate" else "execute")
    update = {"feedback": feedback[:500], "error": None}
    return graph.update_state(target.config, update, as_node=as_node)
