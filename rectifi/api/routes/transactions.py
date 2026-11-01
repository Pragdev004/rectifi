"""The ledger and the Add page: list, add, edit, delete, paste-an-SMS and the phone-inbox prefill."""

from __future__ import annotations

from starlette.routing import Route

from ... import alerts, tools
from ..errors import ApiError
from ..inputs import api, date_field, int_field, money_field, path_int, query, text_field
from .. import wire

DEFAULT_LIMIT = 500
MAX_LIMIT = 2000
NOTE_MAX = 500
SMS_MAX = tools.SMS_TEXT_MAX


def _limit(request) -> int:
    raw = query(request, "limit", str(DEFAULT_LIMIT))
    if not raw.isdigit() or int(raw) < 1:
        raise ApiError(422, "invalid", "The limit must be a whole number.", "limit")
    return min(int(raw), MAX_LIMIT)


@api()
def list_transactions(request):
    period = query(request, "period", "all")
    start = end = None
    if period != "all":
        start, end = tools.month_bounds(period)
    account = query(request, "account")
    unlinked = account == "none"
    if account is not None and not unlinked and not account.isdigit():
        raise ApiError(422, "invalid", "The account filter must be an account id or \"none\".", "account")
    search = query(request, "search")
    if search and len(search) > 100:
        raise ApiError(422, "invalid", "That search is too long.", "search")
    limit = _limit(request)
    source = query(request, "source")
    if source and source != "earlier" and source not in tools.CATEGORY_SOURCES:
        raise ApiError(422, "invalid", "The source filter is not one of the known ways a category is chosen.", "source")
    rows = tools.list_transactions(
        start, end, category=query(request, "category"), search=search, source=source or None,
        account=int(account) if account and not unlinked else None, unlinked=unlinked, limit=limit + 1)
    return {"items": [wire.txn(t) for t in rows[:limit]], "truncated": len(rows) > limit, "limit": limit}


def _known_account(account_id: int | None) -> int | None:
    if account_id is not None and all(a["id"] != account_id for a in tools.list_accounts()):
        raise ApiError(422, "invalid", f"There is no account #{account_id}.", "account_id")
    return account_id


@api(body=True)
def add_transaction(request, data):
    amount = money_field(data, "amount")
    direction = text_field(data, "direction", max_len=10)
    when = date_field(data, "date", required=False)
    payee = text_field(data, "payee", required=False, max_len=120)
    category = text_field(data, "category", required=False, max_len=80)
    note = text_field(data, "note", required=False, max_len=NOTE_MAX)
    account_id = _known_account(int_field(data, "account_id", required=False))
    inbox_id = int_field(data, "inbox_id", required=False)
    extra = {}
    if inbox_id is not None:
        pre = tools.inbox_prefill(inbox_id)                        # the reference and the SMS come from the database, never the browser
        extra = {"source": "sms", "upi_ref": pre["upi_ref"], "raw_text": pre["raw_text"]}
    result = tools.add_transaction(amount, direction, when, payee=payee, category=category, account=account_id, note=note, **extra)
    if inbox_id is not None:
        tools.mark_inbox_added(inbox_id)
    if result["status"] == "added":
        alerts.kick("add")
    return {"status": result["status"], "transaction": wire.txn(result["transaction"])}


@api(body=True)
def edit_transaction(request, data):
    txn_id = path_int(request, "txn_id")
    fields = [name for name in ("category", "note", "account_id") if name in data]
    if not fields:
        raise ApiError(422, "invalid", "Nothing to change: send a category, a note or an account.")
    if tools.get_transaction(txn_id) is None:
        raise ApiError(404, "not_found", f"There is no transaction #{txn_id}.")
    account_id = _known_account(int_field(data, "account_id", required=False)) if "account_id" in data else None
    category = text_field(data, "category", max_len=80) if "category" in data else None
    note = None
    if "note" in data:
        raw = data["note"]
        if raw is not None and not isinstance(raw, str):
            raise ApiError(422, "invalid", "Note must be text.", "note")
        if raw is not None and len(raw) > NOTE_MAX:
            raise ApiError(422, "invalid", f"Note is too long (the limit is {NOTE_MAX} characters).", "note")
        note = (raw or "").strip() or None
    learned = None
    if "account_id" in data:
        tools.set_account(txn_id, account_id)
    if "category" in data:
        learned = tools.set_category(txn_id, category, remember=True)["learned_rule"]
    if "note" in data:
        tools.update_note(txn_id, note)
    return {"transaction": wire.txn(tools.get_transaction(txn_id)), "learned_rule": learned}


@api()
def delete_transaction(request):
    return {"deleted": tools.delete_transaction(path_int(request, "txn_id"))}


def _sms_text(data: dict) -> str:
    text = text_field(data, "text", max_len=SMS_MAX)
    return text


@api(body=True)
def sms_preview(request, data):
    return {"parsed": tools.preview_pasted_sms(_sms_text(data))}


@api(body=True)
def sms_save(request, data):
    result = tools.save_pasted_sms(_sms_text(data))
    if result["status"] == "added":
        alerts.kick("paste")
    return {"status": result["status"], "transaction": wire.txn(result["transaction"])}


@api()
def inbox_prefill(request):
    return tools.inbox_prefill(path_int(request, "item_id"))


routes = [
    Route("/transactions", list_transactions, methods=["GET"]),
    Route("/transactions", add_transaction, methods=["POST"]),
    Route("/transactions/{txn_id}", edit_transaction, methods=["PATCH"]),
    Route("/transactions/{txn_id}", delete_transaction, methods=["DELETE"]),
    Route("/sms/preview", sms_preview, methods=["POST"]),
    Route("/sms/save", sms_save, methods=["POST"]),
    Route("/inbox/{item_id}/prefill", inbox_prefill, methods=["GET"]),
]
