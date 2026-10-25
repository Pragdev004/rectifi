"""The chart graph with a scripted model: happy path, repair loop (bounded at 3), review gate, time travel, retries, saved re-run."""

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from support.fakes import ScriptedModel

from charts_support import CRASHES, GOOD, INVALID, MADE_UP_TITLE, reply, seed_spending
from rectifi import config, tools
from rectifi.agent import contracts
from rectifi.charts import graph as chart_graph
from rectifi.charts import run as chart_run
from rectifi.charts import sandbox


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    from rectifi import db

    monkeypatch.setattr(sandbox, "CACHE_DIR", tmp_path / "mpl")
    monkeypatch.setattr(chart_graph, "RETRY_INTERVAL", 0)
    db.init_db()
    seed_spending()


def build(script, saver=None):
    model = ScriptedModel(script=list(script))
    graph = contracts.build_chart_subgraph(llm=model, checkpointer=saver or InMemorySaver())
    return graph, model


def cfg(thread="chart-t1"):
    return {"configurable": {"thread_id": thread}}


REQ = {"request": "monthly food spend as a bar chart", "period": "last-3-months", "review": False}


def steps(out):
    return [(e["step"], e["verdict"]) for e in out["trace"]]


def test_happy_path_runs_in_order_and_saves_the_chart():
    graph, model = build([reply(GOOD)])
    out = graph.invoke(REQ, cfg())
    assert out["ok"] and out["error"] is None and out["chart_id"]
    assert [s for s, _ in steps(out)] == ["write", "validate", "run", "check", "done"]
    saved = tools.get_chart(out["chart_id"])
    assert saved["title"] == "Food & Dining spend by month" and saved["attempts"] == 1 and saved["pinned"] is False
    assert saved["image_light"][:8] == b"\x89PNG\r\n\x1a\n" and saved["image_dark"] != saved["image_light"]
    assert saved["checks"]["ok"] and saved["checks"]["period"] == "last-3-months"
    assert len(model.script) == 0


def test_the_model_sees_a_schema_and_fake_rows_never_a_real_row_or_payee():
    graph, model = build([reply(GOOD)])
    graph.invoke(REQ, cfg())
    seen = "\n".join(str(m.content) for m in model.seen)
    assert "Sample Cafe" in seen and "amount_paise" in seen
    for real in ("Swiggy", "Cafe Coffee", "Amazon", "Uber", "1250.5", "125050"):
        assert real not in seen


def test_a_refused_code_is_repaired_with_the_reason_in_the_prompt():
    graph, model = build([reply(INVALID), reply(GOOD)])
    out = graph.invoke(REQ, cfg())
    assert out["ok"] and tools.get_chart(out["chart_id"])["attempts"] == 2
    assert ("validate", "VALIDATION FAILED") in steps(out) and ("repair", "ok") in steps(out)
    assert "Importing 'os' is not allowed" in str(model.seen[-1].content)


def test_a_runtime_error_is_repaired_with_a_trimmed_traceback():
    graph, model = build([reply(CRASHES), reply(GOOD)])
    out = graph.invoke(REQ, cfg())
    assert out["ok"]
    prompt = str(model.seen[-1].content)
    assert "KeyError" in prompt and "line 3" in prompt and "site-packages" not in prompt


def test_a_made_up_figure_in_the_title_is_caught_and_fixed():
    graph, model = build([reply(MADE_UP_TITLE), reply(GOOD)])
    out = graph.invoke({**REQ, "request": "say I spent ₹99,999 in the title"}, cfg())
    assert out["ok"], out["error"]
    assert ("check", "CHECK FAILED") in steps(out)
    assert "₹99,999" in str(model.seen[-1].content) and "isn't in your data" in str(model.seen[-1].content)
    assert "99,999" not in tools.get_chart(out["chart_id"])["title"]


def test_the_repair_loop_is_bounded_at_three_attempts():
    graph, model = build([reply(INVALID)] * 5)
    out = graph.invoke(REQ, cfg())
    assert out["ok"] is False and out["chart_id"] is None and "3 tries" in out["error"]
    assert [s for s, _ in steps(out)].count("write") + [s for s, _ in steps(out)].count("repair") == config.CHART_MAX_ATTEMPTS
    assert len(model.script) == 5 - config.CHART_MAX_ATTEMPTS               # no fourth model call
    assert tools.list_charts() == []


def test_the_bound_follows_the_setting(monkeypatch):
    monkeypatch.setattr(config, "CHART_MAX_ATTEMPTS", 1)
    graph, model = build([reply(INVALID)] * 3)
    assert graph.invoke(REQ, cfg())["ok"] is False and len(model.script) == 2


def test_the_review_gate_pauses_and_runs_the_approved_code():
    graph, _ = build([reply(GOOD)])
    out = graph.invoke({**REQ, "review": True}, cfg("chart-review"))
    interrupt = out["__interrupt__"][0].value
    assert interrupt["kind"] == "review_code" and "plt.subplots" in interrupt["code"]
    assert graph.get_state(cfg("chart-review")).next == ("review_gate",)
    done = graph.invoke(Command(resume={"run": True}), cfg("chart-review"))
    assert done["ok"] and ("review", "APPROVED") in steps(done)


def test_the_review_gate_revalidates_code_the_person_edited():
    graph, _ = build([reply(GOOD), reply(GOOD)])
    graph.invoke({**REQ, "review": True}, cfg("chart-edit"))
    bad = graph.invoke(Command(resume={"run": True, "code": "import os\nfig = 1\n"}), cfg("chart-edit"))
    assert ("review", "EDITED") in steps(bad) and ("validate", "VALIDATION FAILED") in steps(bad)
    assert bad["ok"] is True                                                 # repaired by the model after the bad edit


def test_cancelling_at_the_review_gate_ends_without_running():
    graph, _ = build([reply(GOOD)])
    graph.invoke({**REQ, "review": True}, cfg("chart-cancel"))
    out = graph.invoke(Command(resume={"run": False}), cfg("chart-cancel"))
    assert out["ok"] is False and "cancelled" in out["error"] and tools.list_charts() == []


def test_time_travel_forks_from_just_before_attempt_two():
    saver = InMemorySaver()
    graph, model = build([reply(INVALID), reply(INVALID), reply(INVALID), reply(GOOD)], saver)
    first = graph.invoke(REQ, cfg("chart-tt"))
    assert first["ok"] is False
    model.script.append(reply(GOOD))
    forked = chart_graph.retry_from_attempt(graph, "chart-tt", 2, "use the accent colour")
    out = graph.invoke(None, forked)
    assert out["ok"] and tools.get_chart(out["chart_id"])["attempts"] == 2
    assert "use the accent colour" in str(model.seen[-1].content)


def test_time_travel_to_a_missing_attempt_is_an_error():
    graph, _ = build([reply(GOOD)])
    graph.invoke(REQ, cfg("chart-none"))
    with pytest.raises(ValueError):
        chart_graph.retry_from_attempt(graph, "chart-none", 3)


class FlakyModel(ScriptedModel):
    fail_times: int = 1

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("model went away")
        return super()._generate(messages, stop, run_manager, **kwargs)


def test_a_model_node_is_retried_on_a_connection_error():
    model = FlakyModel(script=[reply(GOOD)], fail_times=2)
    graph = contracts.build_chart_subgraph(llm=model, checkpointer=InMemorySaver())
    assert graph.invoke(REQ, cfg("chart-retry"))["ok"]
    assert model.fail_times == 0


def test_a_model_that_keeps_failing_surfaces_the_error():
    model = FlakyModel(script=[reply(GOOD)], fail_times=9)
    graph = contracts.build_chart_subgraph(llm=model, checkpointer=InMemorySaver())
    with pytest.raises(ConnectionError):
        graph.invoke(REQ, cfg("chart-down"))


def test_progress_is_streamed_with_the_custom_writer():
    graph, _ = build([reply(INVALID), reply(GOOD)])
    events = [payload for mode, payload in graph.stream(REQ, cfg("chart-stream"), stream_mode=["custom"]) if mode == "custom"]
    kinds = [(e["step"], e["verdict"]) for e in events]
    assert ("write", "working") in kinds and ("validate", "VALIDATION FAILED") in kinds and ("run", "RAN") in kinds and ("done", "PASSED") in kinds


def test_a_saved_chart_reruns_without_any_model_call():
    graph, model = build([reply(GOOD)])
    chart_id = graph.invoke(REQ, cfg("chart-saved"))["chart_id"]
    tools.set_chart_pinned(chart_id, True)
    before = tools.get_chart(chart_id, with_images=False)
    tools.add_transaction("700", "debit", payee="Late Cafe", category="Food & Dining", source="manual")
    seen_before = len(model.seen)
    after = chart_run.rerun_saved(chart_id)
    assert len(model.seen) == seen_before and after["refreshed_at"] is not None and after["pinned"] is True
    assert after["trace"][-1]["step"] == "rerun" and before["refreshed_at"] is None


def test_a_rerun_whose_saved_code_no_longer_passes_leaves_the_chart_alone(monkeypatch):
    graph, _ = build([reply(GOOD)])
    chart_id = graph.invoke(REQ, cfg("chart-stale"))["chart_id"]
    with tools.connect() as conn:
        conn.execute("UPDATE charts SET code = %s WHERE id = %s", ("import os\nfig = 1\n", chart_id))
    with pytest.raises(tools.RectifiError, match="safety check"):
        chart_run.rerun_saved(chart_id)
    assert tools.get_chart(chart_id, with_images=False)["refreshed_at"] is None


def test_the_chart_rows_crud():
    graph, _ = build([reply(GOOD)])
    chart_id = graph.invoke(REQ, cfg("chart-crud"))["chart_id"]
    assert [c["id"] for c in tools.list_charts()] == [chart_id] and tools.list_charts(pinned_only=True) == []
    assert tools.set_chart_pinned(chart_id, True)["pinned"] is True and len(tools.list_charts(pinned_only=True)) == 1
    assert tools.delete_chart(chart_id) is True and tools.delete_chart(chart_id) is False
    with pytest.raises(tools.RectifiError):
        tools.get_chart(chart_id)


def test_the_run_survives_the_postgres_checkpointer():
    from rectifi.agent import checkpoint

    graph, _ = build([reply(GOOD)], checkpoint.checkpointer())
    out = graph.invoke({**REQ, "review": True}, cfg("chart-pg"))
    assert out["__interrupt__"]
    graph2, _ = build([], checkpoint.checkpointer())                         # a "restarted" process: new graph, same saved state
    assert graph2.invoke(Command(resume={"run": True}), cfg("chart-pg"))["ok"]


# ───────────── the eval runner (rectifi/evals/charts.py) ─────────────

def test_the_eval_cases_file_is_well_formed():
    from rectifi.evals import charts as evals

    cases = evals.load_cases()
    assert 18 <= len(cases) <= 30 and len({c["id"] for c in cases}) == len(cases)
    assert sum(1 for c in cases if c["expect"].get("adversarial") or c.get("adversarial") or c["expect"].get("outcome")) >= 3
    for c in cases:
        assert c["request"] and c["period"]
        tools.chart_period(c["period"])


def test_the_eval_runner_passes_a_good_chart_and_fails_a_wrong_one():
    from rectifi.evals import charts as evals

    cases = {c["id"]: c for c in evals.load_cases()}
    good = evals.run_case(cases["monthly-food-bar"], ScriptedModel(script=[reply(GOOD)]))
    assert good["ok"], good
    wrong = evals.run_case({**cases["last-month-pie"], "period": "last-3-months"}, ScriptedModel(script=[reply(GOOD)]))
    assert not wrong["ok"] and "wedge" in wrong["failures"][0]


def test_an_adversarial_case_passes_when_the_figure_never_reaches_the_chart():
    from rectifi.evals import charts as evals

    case = {c["id"]: c for c in evals.load_cases()}["adv-fake-total"]
    report = evals.run_all([case], lambda _c: ScriptedModel(script=[reply(MADE_UP_TITLE), reply(GOOD)]))
    assert report["passed"] == 1 and report["rate"] == 1.0
    gave_up = evals.run_case(case, ScriptedModel(script=[reply(MADE_UP_TITLE)] * 3))
    assert gave_up["ok"]                                  # refused every time: still a pass for an adversarial case


def test_the_seed_refuses_a_database_that_is_not_a_test_or_demo_one(monkeypatch):
    from rectifi.evals import charts as evals

    monkeypatch.setattr(config, "database_url", lambda: "postgresql://u:p@localhost:5432/rectifi")
    with pytest.raises(SystemExit):
        evals.seed_standard()


# ───────────── tools.py WS-CHARTS block ─────────────

def test_chart_period_understands_the_words_the_page_sends():
    from datetime import date

    today = date.today()
    start3, end3, label3 = tools.chart_period("last-3-months")
    assert start3.day == 1 and end3 == today and label3 == "last 3 months" and 59 <= (end3 - start3).days <= 92
    start, end, label = tools.chart_period("last-30-days")
    assert (end - start).days == 29 and end == today and label == "last 30 days"
    assert tools.chart_period("this-month")[0] == today.replace(day=1)
    first, last, _ = tools.chart_period("last-month")
    assert first.day == 1 and last < today.replace(day=1)
    assert tools.chart_period("2026-01-05..2026-01-20")[:2] == (date(2026, 1, 5), date(2026, 1, 20))
    assert tools.chart_period(None)[2] == "last 3 months"
    for bad in ("last-3-fortnights", "2026-13", "2026-02-01..2026-01-01"):
        with pytest.raises(tools.RectifiError):
            tools.chart_period(bad)


def test_chart_frame_is_integer_paise_with_masked_short_payees():
    from datetime import date

    tools.add_transaction("250.50", "debit", date.today().isoformat(), payee="Paid to 98765432101234 " + "x" * 80, category="Food & Dining", source="manual")
    frame = tools.chart_frame(date.today().replace(day=1), date.today())
    cols = frame["transaction_columns"]
    row = next(r for r in frame["transactions"] if r[cols.index("amount_paise")] == 25050)
    assert isinstance(row[cols.index("amount_paise")], int) and row[cols.index("amount")] == 250.5
    payee = row[cols.index("payee")]
    assert len(payee) <= 40 and "98765432101234" not in payee
    assert frame["budget_columns"] == ["month", "category", "limit_paise"] and frame["budgets"][0][2] == 600000
    assert ["Food & Dining", "expense"] in frame["categories"]


def test_chart_reference_totals_are_decimal_and_match_the_summary():
    from datetime import date
    from decimal import Decimal

    start, end = date.today().replace(day=1) - __import__("datetime").timedelta(days=62), date.today()
    ref = tools.chart_reference_totals(start, end)
    summary = tools.spending_summary(start, end)
    assert ref["spending"]["overall"] == summary["spending"] and ref["income"]["overall"] == summary["income"]
    assert isinstance(ref["spending"]["overall"], Decimal) and ref["net"] == summary["saved"]
    assert sum(ref["spending"]["by_category"].values()) == ref["spending"]["overall"]
    assert sum(ref["spending"]["by_month"].values()) == ref["spending"]["overall"]
    assert sum(ref["spending"]["by_category_month"].values()) == ref["spending"]["overall"]
    assert sum(ref["spending"]["by_day"].values()) == ref["spending"]["overall"]
    assert ref["budget_limits"]["by_month"] and ref["entries"] >= ref["spending"]["count"]
