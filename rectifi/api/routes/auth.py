"""/api/auth: who is signed in, sign in, sign out, sign out everywhere.

Sign-in is only on when RECTIFI_PASSWORD_HASH is set or the app is public (see api/auth.py). On a laptop with no
password `GET /api/auth/status` says {"required": false} and the browser never shows the login page.
Importing this module must not load langchain.
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import auth
from ..errors import ApiError, error_response
from ..inputs import read_json


def _ok(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status)


def _set_cookie(response: Response, value: str) -> None:
    response.set_cookie(auth.COOKIE_NAME, value, max_age=auth.SESSION_SECONDS, path="/", httponly=True, samesite="strict",
                        secure=auth.cookie_secure())


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(auth.COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=auth.cookie_secure())


def _require_on() -> None:
    if not auth.login_required():
        raise ApiError(400, "auth_off", auth.MESSAGES["auth_off"])
    if auth.startup_problem():
        raise ApiError(503, "auth_misconfigured", auth.MESSAGES["auth_misconfigured"])


async def status(request):
    required = auth.login_required()
    signed_in = False
    if required and not auth.startup_problem():
        signed_in = await run_in_threadpool(auth.session_valid, request.headers.get("cookie"))
    return _ok({"required": required, "signed_in": signed_in or not required})


async def login(request):
    data = await read_json(request)
    _require_on()
    password = data.get("password")
    if not isinstance(password, str) or not password:
        raise ApiError(422, "invalid", "Type your password.", "password")
    outcome = await run_in_threadpool(auth.limiter.attempt, password, auth.password_hash())
    if outcome.delay:
        await auth.pause(outcome.delay)
    if outcome.status == "locked":
        return error_response(429, "login_locked", auth.MESSAGES["login_locked"], headers={"Retry-After": str(outcome.retry_after)})
    if outcome.status != "ok":
        return error_response(401, "invalid_password", auth.MESSAGES["invalid_password"], "password")
    cookie = await run_in_threadpool(auth.new_session_cookie)
    response = _ok({"ok": True})
    _set_cookie(response, cookie)
    return response


async def logout(request):
    response = _ok({"ok": True})
    _clear_cookie(response)
    return response


async def logout_all(request):
    _require_on()
    await run_in_threadpool(auth.bump_epoch)
    response = _ok({"ok": True})
    _clear_cookie(response)
    return response


routes = [
    Route("/auth/status", status, methods=["GET"]),
    Route("/auth/login", login, methods=["POST"]),
    Route("/auth/logout", logout, methods=["POST"]),
    Route("/auth/logout-all", logout_all, methods=["POST"]),
]
