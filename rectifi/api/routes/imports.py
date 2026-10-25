"""Statement import over HTTP: upload a CSV/Excel/PDF, see the result (or a preview to fix), commit, look at, undo.

The same functions the Streamlit Add page calls do all the work (read_table, assess_statement, import_rows ...).
A PDF password goes from the form into read_table and is then dropped: it is never stored, logged or echoed."""

from __future__ import annotations

import hashlib

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ... import tools
from ...importer import ROLES, assess_statement, import_rows, normalise, preview_totals, read_table
from .. import uploads, wire
from ..errors import ApiError
from ..inputs import api, ensure_db, int_field, path_int, text_field
from ..security import UPLOAD_LIMIT

ALLOWED_EXTENSIONS = (".csv", ".xlsx", ".xls", ".pdf")
NOTHING_IMPORTED = ("Nothing could be imported: every row needs review. Rectifi can't prove from the balance (or a Dr/Cr mark) "
                    "whether that money went out or came in, and it does not guess. Add them by hand, or use the CSV or Excel statement.")
PREVIEW_ROWS = 12
PREVIEW_PROBLEMS = 5


# ───────────────────────── shapes ─────────────────────────

def batch_card(batch: dict) -> dict:
    return {k: batch[k] for k in ("id", "account_id", "account", "filename", "created_at", "added", "merged", "duplicates",
                                   "needs_review", "undone_at", "rows_left")}


def balance_check(account_id: int | None) -> tuple[dict | None, str | None]:
    if account_id is None:
        return None, None
    try:
        return wire.recon(tools.reconcile(account_id)), None
    except tools.RectifiError as exc:
        return None, str(exc)


def outcome_for(batch_id: int, *, kind: str, how: str | None, review_lines: list[str], skipped: list[str]) -> dict:
    batch = tools.get_import_batch(batch_id)
    if batch is None:
        raise ApiError(404, "not_found", "That import is gone.")
    recon, reason = balance_check(batch["account_id"])
    return {"outcome": kind, "batch": batch_card(batch), "how": how, "needs_review_lines": review_lines,
            "skipped": skipped, "reconcile": recon, "reconcile_reason": reason}


def columns_of(table) -> list[str]:
    return [str(c) for c in table.columns if not str(c).startswith("__")]


def read_mapping(data: dict, table) -> dict:
    raw = data.get("mapping")
    if not isinstance(raw, dict):
        raise ApiError(422, "invalid", "Send the column choices as {role: column or null}.", "mapping")
    columns = set(columns_of(table))
    mapping: dict[str, str | None] = {}
    for role in ROLES:
        value = raw.get(role)
        if value in (None, ""):
            mapping[role] = None
        elif isinstance(value, str) and value in columns:
            mapping[role] = value
        else:
            raise ApiError(422, "invalid", f"There is no column called {str(value)[:60]!r}.", f"mapping.{role}")
    return mapping


def build_preview(table, mapping: dict, check) -> dict:
    rows, problems = normalise(table, mapping)
    review = [r for r in rows if r.review]
    clean = [r for r in rows if not r.review]
    pick = tools.account_for_statement(check.account_last4)
    return {
        "rows": [{"date": r.txn_date, "narration": r.narration, "direction": r.direction, "amount": r.amount, "balance": r.balance}
                 for r in clean[:PREVIEW_ROWS]],
        "review_rows": [{"date": r.txn_date, "narration": r.narration, "amount": r.amount, "why": r.review} for r in review],
        "problems": problems[:PREVIEW_PROBLEMS],
        "totals": preview_totals(clean),
        "suggested_account_id": pick["account"]["id"] if pick["account"] else None,
    }


# ───────────────────────── POST /api/imports ─────────────────────────

def process_upload(filename: str, data: bytes, password: str | None, manual: bool) -> dict:
    ensure_db()
    digest = hashlib.sha256(data).hexdigest()
    seen = uploads.recent.get(digest)
    if seen:
        batch = tools.get_import_batch(seen["batch_id"])
        if batch is not None and batch["undone_at"] is None and batch["created_at"] == seen["created_at"]:
            return outcome_for(seen["batch_id"], kind="already_imported", how=seen["how"],
                               review_lines=seen["needs_review_lines"], skipped=seen["skipped"])
        uploads.recent.forget(digest)
    table = read_table(data, filename, password)
    password = None                                      # the password has done its one job
    check = assess_statement(table)
    accounts = tools.list_accounts()
    account_reason = None
    if not accounts:
        account_reason = "There is no account yet. Add the bank account on the Accounts page first, so imported rows know where they belong."
    elif check.confident and not manual:
        pick = tools.account_for_statement(check.account_last4)
        if pick["account"] is not None:
            how = "digits" if pick["by_digits"] else ("most_recent" if len(accounts) > 1 else "only")
            return finish_import(check.rows, pick["account"]["id"], filename, digest, how, check.problems)
        account_reason = pick["reason"]
    reasons = list(check.reasons)
    if check.confident and manual:
        reasons = ["You asked to check the columns and choose the account yourself, so nothing was imported yet."]
    upload_id = uploads.pending.put({"table": table, "check": check, "filename": filename, "hash": digest})
    preview, preview_error = None, None
    try:
        preview = build_preview(table, check.mapping, check)
    except tools.RectifiError as exc:
        preview_error = str(exc)
    return {"outcome": "preview", "upload_id": upload_id, "filename": filename, "reasons": reasons, "account_reason": account_reason,
            "columns": columns_of(table), "mapping": check.mapping, "preview": preview, "preview_error": preview_error}


def finish_import(rows, account_id: int, filename: str, digest: str, how: str, problems: list[str]) -> dict:
    result = import_rows(rows, account_id, filename)
    if result.batch_id is None:
        raise ApiError(422, "invalid", NOTHING_IMPORTED)
    skipped = list(problems[:PREVIEW_PROBLEMS])
    created = tools.get_import_batch(result.batch_id)["created_at"]       # tells this batch from a later one that reuses the id
    uploads.recent.put(digest, {"batch_id": result.batch_id, "created_at": created, "how": how,
                                "needs_review_lines": list(result.needs_review), "skipped": skipped})
    return outcome_for(result.batch_id, kind="imported", how=how, review_lines=list(result.needs_review), skipped=skipped)


async def post_import(request: Request):
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "multipart/form-data":
        raise ApiError(415, "unsupported_media", "Send the statement as a file upload (multipart/form-data).")
    form = await request.form(max_files=1, max_fields=5, max_part_size=64 * 1024)
    try:
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise ApiError(422, "invalid", "Choose a statement file to upload.", "file")
        name = (upload.filename or "").strip()
        if not name.lower().endswith(ALLOWED_EXTENSIONS):
            raise ApiError(415, "unsupported_media", "Rectifi reads CSV, Excel (.xlsx) and PDF statements.", "file")
        if upload.size is not None and upload.size > UPLOAD_LIMIT:
            raise ApiError(413, "too_large", "That file is larger than 10 MB.", "file")
        data = await upload.read()
        if len(data) > UPLOAD_LIMIT:
            raise ApiError(413, "too_large", "That file is larger than 10 MB.", "file")
        password = form.get("password")
        password = password if isinstance(password, str) and password else None
        manual = form.get("manual") == "1"
    finally:
        await form.close()
    result = await run_in_threadpool(process_upload, name, data, password, manual)
    return JSONResponse(wire.to_wire(result))


# ───────────────────────── preview, commit, look, undo ─────────────────────────

@api(body=True)
def post_preview(request, data):
    pending = uploads.pending.get(text_field(data, "upload_id", max_len=64))
    mapping = read_mapping(data, pending["table"])
    return {"preview": build_preview(pending["table"], mapping, pending["check"]), "mapping": mapping}


@api(body=True)
def post_commit(request, data):
    upload_id = text_field(data, "upload_id", max_len=64)
    pending = uploads.pending.get(upload_id)
    account_id = int_field(data, "account_id")
    if all(a["id"] != account_id for a in tools.list_accounts()):
        raise ApiError(422, "invalid", f"There is no account #{account_id}.", "account_id")
    mapping = read_mapping(data, pending["table"])
    rows, problems = normalise(pending["table"], mapping)
    outcome = finish_import(rows, account_id, pending["filename"], pending["hash"], "chosen", problems)
    uploads.pending.drop(upload_id)
    return outcome


@api()
def get_batch(request):
    batch_id = path_int(request, "batch_id")
    batch = tools.get_import_batch(batch_id)
    if batch is None:
        raise ApiError(404, "not_found", f"There is no import #{batch_id}.")
    recon, reason = balance_check(batch["account_id"])
    return {"batch": batch_card(batch), "reconcile": recon, "reconcile_reason": reason}


@api()
def undo_batch(request):
    batch_id = path_int(request, "batch_id")
    done = tools.undo_import(batch_id)
    batch = tools.get_import_batch(batch_id)
    return {**done, "batch": batch_card(batch) if batch else None}


routes = [
    Route("/imports", post_import, methods=["POST"]),
    Route("/imports/preview", post_preview, methods=["POST"]),
    Route("/imports/commit", post_commit, methods=["POST"]),
    Route("/imports/{batch_id}", get_batch, methods=["GET"]),
    Route("/imports/{batch_id}/undo", undo_batch, methods=["POST"]),
]

