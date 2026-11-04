"""/api/account/export, DELETE /api/account, GET /api/privacy.

A wrapper (CLAUDE.md rule 13): each route validates input and calls one function in rectifi/account.py. Nothing here
logs or echoes a password. /api/privacy is public (SessionLock exempts it) and touches no user data and no database.
"""

from __future__ import annotations

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ... import account, config, users
from .. import ask, auth
from . import charts
from ..errors import error_response
from ..inputs import api


@api()
def export(request):
    """A ZIP of CSV files with the signed-in person's own rows. Row level security picks the rows; the connection opens the sealed text."""
    data, name = account.build_export()
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def _drain() -> None:
    """Chat turns and chart runs that were still going when the owner deleted the account: stop them and wait a while."""
    ask.drain_turns(10.0)
    charts.drain_runs(10.0)


@api(body=True)
def delete(request, data):
    """Needs the password and the typed username. Deletes every row the person owns and ends their sessions."""
    who = getattr(request.state, "signed_in", None)
    if who is None:
        return error_response(401, "auth_required", auth.MESSAGES["auth_required"])
    password, confirm = data.get("password"), data.get("confirm_username")
    if not isinstance(password, str) or not password or len(password) > users.MAX_PASSWORD_LENGTH:
        return error_response(422, "invalid", "Type your password.", "password")
    if not isinstance(confirm, str) or not confirm.strip() or len(confirm) > 100:
        return error_response(422, "invalid", "Type your username to confirm.", "confirm_username")
    try:
        account.delete_account(who.uid, who.username, password, confirm, drain=_drain)
    except users.AuthError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return error_response(exc.status, exc.code, exc.message, exc.field, headers=headers)
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=auth.cookie_secure())
    return response


async def privacy(request):
    """Public facts about what is and is not protected. No user data, no database."""
    return JSONResponse(account.privacy_facts(), headers={"Cache-Control": "no-cache"})


routes = [
    Route("/account/export", export, methods=["GET"]),
    Route("/account", delete, methods=["DELETE"]),
    Route("/privacy", privacy, methods=["GET"]),
]
