"""One background worker for slow jobs (today: sorting an import with the local model).

CLAUDE.md says one worker, so there is exactly one daemon thread named `rectifi-bg` and a queue in front of it. A job is
a row in classify_jobs; the queue only holds job ids. After a restart `requeue_unfinished()` puts the 'queued' and
'running' jobs back; that is safe to repeat because classify only ever touches rows that are still pending.
"""

from __future__ import annotations

import queue
import threading

from .. import config, tools, userctx

# The queue holds job ids. `_owners` remembers the person each job was queued under. The worker is one thread for the whole
# process, so it binds that person per job: a job never runs as someone else, and in multi-user mode never with nobody bound
# (multi-user plan, D7/D8).
_queue: "queue.Queue[int]" = queue.Queue()
_owners: dict[int, userctx.UserContext] = {}
_thread: threading.Thread | None = None
_lock = threading.Lock()
_requeued = False


def _loop() -> None:
    while True:
        job_id = _queue.get()
        try:
            with userctx.bound(_owners.pop(job_id, None) or _fallback_context()):
                run_job(job_id)
        except Exception:                                       # noqa: BLE001  (a failed job must never kill the worker)
            pass
        finally:
            _queue.task_done()


def _fallback_context() -> userctx.UserContext:
    """A job nobody registered a person for (a test that puts a bare id on the queue). Laptop mode: the one person. Multi-user
    mode: nobody, so the job fails closed (NoUser is caught by the loop) instead of running as a guess."""
    if config.multiuser():
        raise userctx.NoUser("This job has no person.")
    return userctx.LAPTOP


def _enqueue(job_id: int, ctx: userctx.UserContext) -> None:
    _owners[job_id] = ctx
    _queue.put(job_id)


def _ensure_worker() -> None:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = userctx.spawn(_loop, name="rectifi-bg")


def run_job(job_id: int, llm=None) -> dict | None:
    """Run one classify job now, in this thread (the worker calls this; tests call it directly)."""
    from .classify import classify_pending

    job = tools.get_classify_job(job_id)
    if job is None or job["status"] in ("cancelled", "done"):
        return None
    tools.update_classify_job(job_id, status="running", error=None)
    return classify_pending(job["batch_id"], llm=llm, job_id=job_id)


def submit(job_id: int) -> None:
    """Queue a job for the worker."""
    requeue_unfinished()
    _ensure_worker()
    _enqueue(job_id, userctx.current())


def requeue_unfinished() -> int:
    """Once per process: put jobs that a restart interrupted back on the queue. Returns how many.

    Multi-user mode: only for the person bound right now (the owner, from the sign-in hook), because a job needs that
    person's data key. With nobody bound it does nothing and does not use up the once-per-process turn."""
    global _requeued
    if config.multiuser() and not userctx.is_bound():
        return 0
    with _lock:
        if _requeued:
            return 0
        _requeued = True
    ctx = userctx.current()
    jobs = tools.unfinished_classify_jobs()
    if jobs:
        _ensure_worker()
    for job in jobs:
        _enqueue(job["id"], ctx)
    return len(jobs)


def reset_for_tests() -> None:
    """Forget that the restart requeue already ran (tests call it to run the requeue again)."""
    global _requeued
    _requeued = False


def queue_for_batch(batch_id: int | None) -> dict:
    """Make a classify job for the pending rows of an import (or all pending rows) and start it, if the model is on.
    Returns {"job_id": int|None, "status": str|None}. Status 'no_model' means the rows wait; the person can start them
    later from the import page (POST /api/classify/jobs)."""
    from .llm import llm_status

    if not tools.pending_classification(batch_id, limit=1):
        return {"job_id": None, "status": None}
    if not config.auto_classify():
        job_id = tools.create_classify_job(batch_id, "no_model", "Automatic sorting is off (RECTIFI_AUTO_CLASSIFY=0).")
        return {"job_id": job_id, "status": "no_model"}
    ok, why = llm_status()
    if not ok:
        job_id = tools.create_classify_job(batch_id, "no_model", why)
        return {"job_id": job_id, "status": "no_model"}
    job_id = tools.create_classify_job(batch_id, "queued")
    if job_id is not None:
        submit(job_id)
    return {"job_id": job_id, "status": "queued"}
