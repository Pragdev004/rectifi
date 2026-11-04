"""/api/knowledge: the Library page. What is in the library, add or remove your own files, and re-index.

    GET    /knowledge/docs         guides (built in) and your files, with how many passages each has
    POST   /knowledge/docs         add a .md or .txt file (multipart, field "file"). Statements are refused.
    DELETE /knowledge/docs/{id}    remove one of YOUR files (guides cannot be removed)
    GET    /knowledge/status       what is indexed, with which embedding model, and the re-index job
    POST   /knowledge/reindex      start a re-index in the background (one at a time); poll /knowledge/status

A route validates, calls tools.* and shapes the answer. The indexing itself (embeddings, langchain) is imported inside the handler or
the worker thread, so importing this module loads no model library. One worker process (CLAUDE.md rule 13), so the job state is a module global.
"""

from __future__ import annotations

import os
import threading
import time

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ... import config, tools, userctx
from .. import wire
from ..errors import ApiError
from ..inputs import api, ensure_db, path_int
from ..security import UPLOAD_LIMIT

_lock = threading.Lock()
_job: dict = {"state": "idle", "done": 0, "total": 0, "error": None, "result": None, "started_at": None, "finished_at": None}


def job_snapshot() -> dict:
    with _lock:
        return dict(_job)


def _explain(exc: Exception) -> str:
    try:
        from ...agent.llm import explain_error

        return explain_error(exc)
    except Exception:
        return "The embedding model is not answering. Check that Ollama is running and the embedding model is pulled."


def _run_job(corpora: tuple[str, ...]) -> None:
    try:
        from ...rag import embeddings as rag_embeddings
        from ...rag import index

        embeddings = rag_embeddings.get_rag_embeddings()

        def progress(n: int) -> None:
            with _lock:
                _job["done"] += n

        result = index.reindex(embeddings, corpora=corpora, progress=progress)
        with _lock:
            _job.update(state="done", result=result, error=None, finished_at=time.time())
    except Exception as exc:  # shown to the person in plain words; the job never crashes the server
        with _lock:
            _job.update(state="failed", error=_explain(exc), finished_at=time.time())


def start_reindex(corpora: tuple[str, ...] = ("guide", "note", "upload", "txn")) -> bool:
    """Start the background re-index. False if one is already running (or, in multi-user mode, nobody is bound: the index
    belongs to a person, so the start-up call does nothing and the owner's sign-in hook starts it)."""
    if config.multiuser() and not userctx.is_bound():
        return False
    with _lock:
        if _job["state"] == "running":
            return False
        _job.update(state="running", done=0, total=0, error=None, result=None, started_at=time.time(), finished_at=None)
    try:
        from ...rag import index

        total = index.count_work(config.embed_model())
    except Exception:
        total = 0
    with _lock:
        _job["total"] = total
    userctx.spawn(_run_job, corpora, name="rag-reindex")       # keeps the person who started it
    return True


def auto_reindex(corpora: tuple[str, ...] = ("guide", "note", "upload", "txn")) -> bool:
    """The automatic re-index (app start, after an import). Off when RECTIFI_NO_AUTO_REINDEX=1 (tests set it). Never raises."""
    if os.getenv("RECTIFI_NO_AUTO_REINDEX", "").strip() == "1":
        return False
    try:
        return start_reindex(corpora)
    except Exception:                                           # noqa: BLE001
        return False


def join_job(timeout: float = 30.0) -> None:
    """For tests: wait until the background job is no longer running."""
    end = time.time() + timeout
    while time.time() < end:
        if job_snapshot()["state"] != "running":
            return
        time.sleep(0.02)


@api()
def list_docs(request):
    rows = tools.rag_list_documents()
    stored = {(r["corpus"], r["source"]) for r in rows}
    missing = [{"id": None, "corpus": "guide", "title": g["title"], "source": g["source"], "chunks": 0, "created_at": None}
               for g in tools.rag_guide_files() if ("guide", g["source"]) not in stored]
    docs = [wire.knowledge_doc(r) for r in [*rows, *missing] if r["corpus"] in ("guide", "upload")]
    return {"docs": docs}


@api()
def status(request):
    st = tools.rag_status(config.embed_model())
    return wire.knowledge_status(st, job_snapshot(), len(tools.rag_guide_files()))


@api()
def reindex(request):
    started = start_reindex()
    return {"started": started, "job": job_snapshot()}


async def add_doc(request: Request):
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "multipart/form-data":
        raise ApiError(415, "unsupported_media", "Send the file as a file upload (multipart/form-data).")
    form = await request.form(max_files=1, max_fields=3, max_part_size=64 * 1024)
    try:
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise ApiError(422, "invalid", "Choose a .md or .txt file to add.", "file")
        if upload.size is not None and upload.size > UPLOAD_LIMIT:
            raise ApiError(413, "too_large", "That file is too large.", "file")
        data = await upload.read()
        name = (upload.filename or "").strip()
    finally:
        await form.close()
    await run_in_threadpool(ensure_db)
    return JSONResponse(wire.to_wire(await run_in_threadpool(store_upload, name, data)), status_code=201)


def store_upload(name: str, data: bytes) -> dict:
    try:
        source, title, text = tools.rag_check_upload(name, data)
    except tools.RectifiError as exc:
        raise ApiError(422, "invalid", str(exc), "file") from None
    saved = tools.rag_upsert_document("upload", source, title, text)
    indexed, note = False, None
    try:
        from ...rag import embeddings as rag_embeddings
        from ...rag import index

        index.reindex(rag_embeddings.get_rag_embeddings(), corpora=("upload",))
        indexed = True
    except Exception as exc:  # the file is saved; it will be indexed on the next re-index
        note = _explain(exc)
    doc = next(d for d in tools.rag_list_documents("upload") if d["id"] == saved["id"])
    return {"doc": wire.knowledge_doc(doc), "indexed": indexed, "note": note}


@api()
def delete_doc(request):
    doc_id = path_int(request, "doc_id")
    doc = tools.rag_get_document(doc_id)
    if doc is None:
        raise ApiError(404, "not_found", "That file is not in the library.")
    if doc["corpus"] != "upload":
        raise ApiError(409, "builtin", "Built-in guides cannot be removed. Only the files you added can.")
    tools.rag_delete_document(doc_id, corpus="upload")
    return {"deleted": doc_id}


routes = [
    Route("/knowledge/docs", list_docs, methods=["GET"]),
    Route("/knowledge/docs", add_doc, methods=["POST"]),
    Route("/knowledge/docs/{doc_id}", delete_doc, methods=["DELETE"]),
    Route("/knowledge/status", status, methods=["GET"]),
    Route("/knowledge/reindex", reindex, methods=["POST"]),
]
