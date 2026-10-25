"""Accounts: the list with each balance check, adding an account, and recording a bank balance."""

from __future__ import annotations

from starlette.routing import Route

from ... import tools
from ..errors import ApiError
from ..inputs import api, date_field, money_field, path_int, text_field
from .. import wire


def check(account_id: int) -> tuple[dict | None, str | None]:
    """(balance check, None) or (None, the plain reason there isn't one yet)."""
    try:
        return wire.recon(tools.reconcile(account_id)), None
    except tools.RectifiError as exc:
        return None, str(exc)


def require_account(account_id: int) -> dict:
    found = next((a for a in tools.list_accounts() if a["id"] == account_id), None)
    if found is None:
        raise ApiError(404, "not_found", f"There is no account #{account_id}.")
    return found


@api()
def list_accounts(request):
    items = []
    for a in tools.list_accounts():
        recon, reason = check(a["id"])
        items.append({"id": a["id"], "name": a["name"], "last4": a["last4"], "transactions": a["transactions"],
                      "last_snapshot": a["last_snapshot"], "reconcile": recon, "reason": reason})
    default = tools.default_account()
    return {"items": items, "default_account_id": default["id"] if default else None}


@api(body=True)
def add_account(request, data):
    name = text_field(data, "name", required=False, max_len=80)
    last4 = text_field(data, "last4", required=False, max_len=12)
    return {"account": tools.add_account(name or "", last4)}


@api(body=True)
def record_balance(request, data):
    account = require_account(path_int(request, "account_id"))
    as_of = date_field(data, "as_of")
    balance = money_field(data, "balance")
    done = tools.record_balance(account["id"], as_of, balance)
    recon, reason = check(account["id"])
    return {**done, "reconcile": recon, "reason": reason}


routes = [
    Route("/accounts", list_accounts, methods=["GET"]),
    Route("/accounts", add_account, methods=["POST"]),
    Route("/accounts/{account_id}/balances", record_balance, methods=["POST"]),
]
