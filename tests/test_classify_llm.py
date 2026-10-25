"""The model pass (layer 5): enum-constrained answers, thresholds, masking, races, cancel and restart. A scripted model, no Ollama."""

import re
import typing
from datetime import date
from decimal import Decimal

import httpx
import pytest
from support.fakes import ScriptedStructured

from rectifi import categorize, config, tools
from rectifi.agent import classify, jobs
from rectifi.db import connect
from rectifi.importer import StatementRow, import_rows

D = date(2026, 10, 1)


def labels(*items):
    """items: (number, category, confidence) -> a scripted structured answer."""
    return {"labels": [{"i": i, "category": c, "confidence": conf, "why": "test"} for i, c, conf in items]}


def statement(*narrations, direction="debit"):
    return [StatementRow(D, Decimal(str(100 + n)), direction, text, None, None, n + 1) for n, text in enumerate(narrations)]


@pytest.fixture
def batch():
    acc = tools.add_account("SBI", "1234")
    result = import_rows(
        statement("UPI/DR/111111111111/RAHUL SHARMA/HDFC/Pay", "UPI/DR/222222222222/MURUGAN AGENCIES/SBIN/Pay",
                  "UPI/DR/333333333333/BLUE LOTUS/SBIN/Pay"), acc["id"], "x.csv")
    return result.batch_id


def snapshot(batch_id):
    with connect() as conn:
        return conn.execute(
            "SELECT id, amount, direction, txn_date, account_id, upi_ref, fingerprint, payee, description FROM transactions "
            "WHERE import_batch_id = %s ORDER BY id", (batch_id,)).fetchall()


def run(batch_id, model, **kw):
    return classify.classify_pending(batch_id, llm=model, **kw)


def test_the_model_applies_sure_answers_and_changes_nothing_else(batch):
    before = snapshot(batch)
    model = ScriptedStructured(structured=[labels((1, "Miscellaneous", 0.9), (2, "Shopping", 0.91), (3, "Food & Dining", 0.85))])
    out = run(batch, model)
    assert out["status"] == "done" and out["total"] == 3 and out["done"] == 3
    assert out["by_source"] == {"llm": 2, "fallback": 1}
    assert snapshot(batch) == before                               # amount, direction, date, account, upi_ref, fingerprint
    rows = {r["payee"]: r for r in tools.batch_rows(batch)}
    murugan = next(r for p, r in rows.items() if "murugan" in p.lower() or "MURUGAN" in p)
    assert (murugan["category"], murugan["category_source"], murugan["category_confidence"]) == ("Shopping", "llm", 91)
    assert murugan["classify_state"] == "done"
    assert tools.pending_classification(batch) == []


def test_only_pending_rows_are_touched(batch):
    pending = tools.pending_classification(batch)
    keep = pending[0]["id"]
    tools.set_category(keep, "Health")                             # not pending any more
    model = ScriptedStructured(structured=[labels((1, "Shopping", 0.9), (2, "Shopping", 0.9))])
    run(batch, model)
    assert tools.get_transaction(keep)["category"] == "Health"
    asked = "\n".join(str(m.content) for m in model.seen)
    assert "1. debit" in asked and "3. debit" not in asked         # only the two rows still open were asked about


def test_the_enum_blocks_a_wrong_kind_category(batch):
    model = ScriptedStructured(structured=[labels((1, "Salary & Allowance", 0.99)), ])
    out = run(batch, model)                                        # a debit can never be filed as salary: the answer is rejected
    assert out["status"] == "failed"
    debit_schema = model.schemas[0]
    row = typing.get_args(debit_schema.model_fields["labels"].annotation)[0]
    allowed = set(typing.get_args(row.model_fields["category"].annotation))
    assert "Food & Dining" in allowed and "Transfers" in allowed and "Miscellaneous" in allowed
    assert not allowed & {"Salary & Allowance", "Refunds", "Interest & Cashback", "Other income"}
    assert all(tools.get_transaction(r["id"])["category"] == "Miscellaneous" for r in tools.pending_classification(batch))


def test_credits_get_an_income_only_enum():
    acc = tools.add_account("SBI", "1234")
    b = import_rows(statement("UPI/CR/444444444444/RAHUL SHARMA/HDFC/Pay", direction="credit"), acc["id"], "c.csv").batch_id
    model = ScriptedStructured(structured=[labels((1, "Refunds", 0.9))])
    out = run(b, model)
    assert out["by_source"] == {"llm": 1}
    row = typing.get_args(model.schemas[0].model_fields["labels"].annotation)[0]
    allowed = set(typing.get_args(row.model_fields["category"].annotation))
    assert "Refunds" in allowed and "Food & Dining" not in allowed


def test_the_suggestion_band(batch, monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_APPLY_MIN", 80)
    monkeypatch.setattr(config, "CLASSIFY_SUGGEST_MIN", 50)
    model = ScriptedStructured(structured=[labels((1, "Food & Dining", 0.62), (2, "Shopping", 0.30), (3, "Health", 0.5))])
    out = run(batch, model)
    assert out["by_source"] == {"suggested": 2, "fallback": 1}
    rows = tools.batch_rows(batch, "suggested")
    assert {(r["suggested_category"], r["suggested_confidence"]) for r in rows} == {("Food & Dining", 62), ("Health", 50)}
    assert all(r["category"] == "Miscellaneous" and r["category_source"] == "fallback" for r in rows)


def test_a_model_that_answers_in_percent_is_understood(batch):
    out = run(batch, ScriptedStructured(structured=[labels((1, "Shopping", 85), (2, "Shopping", 85), (3, "Shopping", 85))]))
    assert out["by_source"] == {"llm": 3}


def test_a_missing_or_double_number_is_unsure_not_a_guess(batch):
    model = ScriptedStructured(structured=[labels((1, "Shopping", 0.9), (1, "Health", 0.9), (3, "Health", 0.9))])
    out = run(batch, model)
    assert out["by_source"] == {"llm": 1, "fallback": 2}
    assert tools.pending_classification(batch) == []


def test_the_model_never_sees_a_long_digit_run_or_the_account(batch):
    acc = tools.add_account("HDFC Salary", "9876")
    b = import_rows(statement("UPI/DR/987654321098/RAHUL 98765432101 SHARMA/HDFC/card 4111 1111 1111 1111"), acc["id"], "m.csv").batch_id
    model = ScriptedStructured(structured=[labels((1, "Miscellaneous", 0.9))])
    run(b, model)
    text = "\n".join(str(m.content) for m in model.seen)
    assert "RAHUL" in text.upper()                                  # it did see the (masked) payee
    assert not re.search(r"\d{9,}", text)
    assert "4111 1111 1111 1111" not in text and "HDFC Salary" not in text and "987654321098" not in text


def test_a_correction_during_the_job_wins(batch):
    target = tools.pending_classification(batch)[1]["id"]

    class Racing(ScriptedStructured):
        def with_structured_output(self, schema, **kw):
            inner = super().with_structured_output(schema, **kw)

            def go(value):
                tools.set_category(target, "Health")                # the person fixes a row while the model is thinking
                return inner.invoke(value)

            from langchain_core.runnables import RunnableLambda
            return RunnableLambda(go)

    out = run(batch, Racing(structured=[labels((1, "Shopping", 0.9), (2, "Shopping", 0.9), (3, "Shopping", 0.9))]))
    assert out["status"] == "done"
    mine = tools.get_transaction(target)
    assert (mine["category"], mine["category_source"]) == ("Health", "user")
    assert out["by_source"]["llm"] == 3                           # counted as asked ...
    applied = [r for r in tools.batch_rows(batch) if r["category_source"] == "llm"]
    assert len(applied) == 2                                       # ... but only two rows were still open


def test_no_model_leaves_rows_waiting(batch, monkeypatch):
    from rectifi.agent import llm as llm_module
    monkeypatch.setattr(llm_module, "llm_status", lambda: (False, "Ollama isn't running."))
    out = classify.classify_pending(batch)
    assert out["status"] == "no_model" and "Ollama" in out["error"]
    assert len(tools.pending_classification(batch)) == 3


def test_an_unreachable_model_fails_the_job_but_not_the_rows(batch):
    class Down(ScriptedStructured):
        def with_structured_output(self, schema, **kw):
            from langchain_core.runnables import RunnableLambda

            def boom(_):
                raise httpx.ConnectError("refused")
            return RunnableLambda(boom)

    out = run(batch, Down())
    assert out["status"] == "failed" and out["error"]
    assert len(tools.pending_classification(batch)) == 3


def test_rows_with_the_same_merchant_are_asked_once():
    acc = tools.add_account("SBI", "1234")
    b = import_rows(statement(*[f"UPI/DR/55555555555{i}/RAHUL SHARMA/HDFC/Pay" for i in range(4)]), acc["id"], "d.csv").batch_id
    model = ScriptedStructured(structured=[labels((1, "Gifts & Donations", 0.9))])
    out = run(b, model)
    assert out["by_source"] == {"llm": 4} and len(model.schemas) == 1


def test_batches_follow_the_configured_size(monkeypatch):
    monkeypatch.setattr(config, "CLASSIFY_BATCH_SIZE", 2)
    acc = tools.add_account("SBI", "1234")
    b = import_rows(statement(*[f"UPI/DR/66666666666{i}/PERSON NUMBER{'X' * i}/HDFC/Pay" for i in range(5)]), acc["id"], "e.csv").batch_id
    model = ScriptedStructured(structured=[labels((1, "Shopping", 0.9), (2, "Shopping", 0.9)),
                                           labels((1, "Shopping", 0.9), (2, "Shopping", 0.9)), labels((1, "Shopping", 0.9))])
    out = run(b, model)
    assert len(model.schemas) == 3 and out["done"] == 5


def test_progress_is_reported(batch):
    seen = []
    run(batch, ScriptedStructured(structured=[labels((1, "Shopping", 0.9), (2, "Shopping", 0.9), (3, "Shopping", 0.9))]),
        progress=lambda done, total: seen.append((done, total)))
    assert seen[-1] == (3, 3)


# ───────────── the job row: cancel on undo, restart ─────────────

def test_undo_cancels_the_job(batch):
    job_id = tools.create_classify_job(batch)
    assert tools.get_classify_job(job_id)["status"] == "queued"
    tools.undo_import(batch)
    assert tools.get_classify_job(job_id)["status"] == "cancelled"
    out = jobs.run_job(job_id, llm=ScriptedStructured(structured=[]))
    assert out is None


def test_a_running_job_stops_when_the_import_is_undone(batch):
    job_id = tools.create_classify_job(batch)
    tools.undo_import(batch)
    out = classify.classify_pending(batch, llm=ScriptedStructured(structured=[]), job_id=job_id)
    assert out["status"] in ("cancelled", "done")                  # nothing pending is left after undo, and nothing was asked


def test_run_job_updates_the_row_and_a_second_run_does_nothing(batch):
    job_id = tools.create_classify_job(batch)
    model = ScriptedStructured(structured=[labels((1, "Shopping", 0.9), (2, "Shopping", 0.9), (3, "Miscellaneous", 0.9))])
    out = jobs.run_job(job_id, llm=model)
    job = tools.get_classify_job(job_id)
    assert (job["status"], job["total"], job["done"], job["by_source"]) == ("done", 3, 3, {"llm": 2, "fallback": 1})
    assert job["model"] and job["finished_at"] is not None and out["status"] == "done"
    assert jobs.run_job(job_id, llm=ScriptedStructured(structured=[])) is None


def test_restart_requeue_is_idempotent(batch, monkeypatch):
    queued = tools.create_classify_job(batch)
    running = tools.create_classify_job(batch, "running")
    waiting = tools.create_classify_job(batch, "no_model")
    monkeypatch.setattr(jobs, "_ensure_worker", lambda: None)
    jobs.reset_for_tests()
    assert jobs.requeue_unfinished() == 2
    assert jobs.requeue_unfinished() == 0                          # once per process
    ids = []
    while not jobs._queue.empty():
        ids.append(jobs._queue.get_nowait())
    assert ids == [queued, running] and waiting not in ids
    # running the same job twice (the restart ran it halfway) changes nothing the second time
    model = ScriptedStructured(structured=[labels((1, "Shopping", 0.9), (2, "Shopping", 0.9), (3, "Shopping", 0.9))])
    jobs.run_job(running, llm=model)
    again = snapshot(batch)
    jobs.run_job(queued, llm=ScriptedStructured(structured=[]))
    assert snapshot(batch) == again


def test_queue_for_batch_records_no_model_when_the_model_is_off(batch, monkeypatch):
    from rectifi.agent import llm as llm_module
    monkeypatch.setattr(llm_module, "llm_status", lambda: (False, "Ollama isn't running."))
    started = jobs.queue_for_batch(batch)
    assert started["status"] == "no_model"
    assert tools.get_classify_job(started["job_id"])["error"] == "Ollama isn't running."
    monkeypatch.setenv("RECTIFI_AUTO_CLASSIFY", "0")
    assert jobs.queue_for_batch(batch)["status"] == "no_model"


def test_nothing_pending_means_no_job():
    acc = tools.add_account("SBI", "1234")
    b = import_rows(statement("UPI/DR/777777777777/SWIGGY/YESB/swiggy@ybl/Pay"), acc["id"], "s.csv").batch_id
    assert jobs.queue_for_batch(b) == {"job_id": None, "status": None}


def test_resort_marks_old_miscellaneous_rows():
    t = tools.add_transaction("100", "debit", payee="Nobody Known", source="chat")["transaction"]
    with connect() as conn:
        conn.execute("UPDATE transactions SET category_source = NULL, classify_state = NULL WHERE id = %s", (t["id"],))
    mine = tools.add_transaction("100", "debit", payee="Other Nobody", category="Miscellaneous", source="chat")["transaction"]
    assert tools.queue_miscellaneous_resort() == 1
    assert [r["id"] for r in tools.pending_classification(None)] == [t["id"]]
    assert tools.get_transaction(mine["id"])["category_source"] == "user"


def test_mask_for_model():
    assert not re.search(r"\d{9,}", classify.mask_for_model("ref 123456789012 and pay98765432101x"))
    assert classify.mask_for_model("rent 12500") == "rent 12500"
