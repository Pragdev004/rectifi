"""/api/auth: who is signed in, sign up, sign in, sign out, password change, recovery.

Three modes, one set of URLs:
  * laptop, no password:       {"required": false}; the browser never shows the login page.
  * one shared password:       RECTIFI_PASSWORD_HASH (api/auth.py). Unchanged.
  * multi-user (RECTIFI_MULTIUSER=1): invite sign-up in two steps, accounts, per-account limits (rectifi/users.py).

Sign-up is two calls so the person has their recovery code before the account exists for real:
  POST /auth/signup {invite, username, password}  -> 201 {signup_token, recovery_code}. No cookie, no session.
  POST /auth/signup/confirm {signup_token}        -> the account is confirmed, the invite is used, the cookie is set.
Nothing here logs or echoes a password, a code, a token or a cookie. Importing this module must not load langchain.
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ... import config, users
from .. import auth
from ..errors import ApiError, error_response
from ..inputs import ensure_db, read_json


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


def _require_multiuser(message_key: str = "not_multiuser") -> None:
    _require_on()
    if not config.multiuser():
        code = "signup_off" if message_key == "signup_off" else "not_multiuser"
        raise ApiError(400, code, auth.MESSAGES[message_key])


def _refusal(exc: users.AuthError) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return error_response(exc.status, exc.code, exc.message, exc.field, headers=headers)


def _text(data: dict, name: str, *, max_len: int = 1024) -> str:
    value = data.get(name)
    label = name.replace("_", " ").capitalize()
    if not isinstance(value, str) or not value.strip():
        raise ApiError(422, "invalid", f"{label} is needed.", name)
    if len(value) > max_len:
        raise ApiError(422, "invalid", f"{label} is too long.", name)
    return value


def _call(fn, *args):
    """Run a blocking users.* call in a worker thread, with the tables ensured first. An AuthError comes back as the value."""
    def work():
        ensure_db()
        try:
            return fn(*args)
        except users.AuthError as exc:
            return exc
    return run_in_threadpool(work)


def _session_cookie(signed_in, epoch: int | None = None) -> str:
    return auth.make_user_session(auth.session_secret(), signed_in, epoch=epoch)


def _run_sign_in_hooks(signed_in) -> None:
    from ... import hooks                                   # the hooks import their work inside functions: no langchain here

    hooks.after_sign_in(signed_in.ctx)


# ───────────────────────── status ─────────────────────────

async def status(request):
    required = auth.login_required()
    multi = config.multiuser()
    signed_in, username, is_owner = False, None, not multi
    if required and not auth.startup_problem():
        if multi:
            who = await run_in_threadpool(auth.resolve_user, request.headers.get("cookie"))
            if who is not None:
                signed_in, username, is_owner = True, who.username, who.is_owner
        else:
            signed_in = await run_in_threadpool(auth.session_valid, request.headers.get("cookie"))
    return _ok({"required": required, "signed_in": signed_in or not required, "multiuser": multi, "username": username,
                "is_owner": is_owner})


# ───────────────────────── sign up (two steps) ─────────────────────────

async def signup(request):
    data = await read_json(request)
    _require_multiuser("signup_off")
    result = await _call(users.signup, _text(data, "invite", max_len=200), _text(data, "username", max_len=100), _text(data, "password"))
    if isinstance(result, users.AuthError):
        return _refusal(result)
    token, code = result
    return _ok({"signup_token": token, "recovery_code": code}, 201)


async def signup_confirm(request):
    data = await read_json(request)
    _require_multiuser("signup_off")
    result = await _call(users.confirm_signup, _text(data, "signup_token", max_len=200))
    if isinstance(result, users.AuthError):
        return _refusal(result)
    response = _ok({"ok": True, "username": result.username, "is_owner": result.is_owner})
    _set_cookie(response, _session_cookie(result))
    await run_in_threadpool(_run_sign_in_hooks, result)
    return response


# ───────────────────────── sign in, out ─────────────────────────

async def login(request):
    data = await read_json(request)
    _require_on()
    password = data.get("password")
    if not isinstance(password, str) or not password:
        raise ApiError(422, "invalid", "Type your password.", "password")
    if config.multiuser():
        return await _login_multiuser(data, password)
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


async def _login_multiuser(data: dict, password: str) -> Response:
    username = data.get("username")
    if not isinstance(username, str) or not username.strip():
        raise ApiError(422, "invalid", "Type your username.", "username")
    outcome = await _call(users.login, username[:100], password)
    if outcome.delay:
        await auth.pause(outcome.delay)
    if outcome.status == "locked":
        return error_response(429, "login_locked", users.MESSAGES["login_locked"], headers={"Retry-After": str(outcome.retry_after)})
    if outcome.status != "ok":
        return error_response(401, "invalid_login", users.MESSAGES["invalid_login"])
    who = outcome.signed_in
    response = _ok({"ok": True, "username": who.username, "is_owner": who.is_owner})
    _set_cookie(response, _session_cookie(who))
    await run_in_threadpool(_run_sign_in_hooks, who)
    return response


async def logout(request):
    if config.multiuser():
        data = auth.read_user_session(auth.cookie_value(request.headers.get("cookie")), auth.session_secret())
        if data is not None:
            await _call(users.end_session, data["uid"], data["sid"])
    response = _ok({"ok": True})
    _clear_cookie(response)
    return response


async def logout_all(request):
    _require_on()
    if config.multiuser():
        await _call(users.end_all_sessions, request.state.signed_in.uid)
    else:
        await run_in_threadpool(auth.bump_epoch)
    response = _ok({"ok": True})
    _clear_cookie(response)
    return response


# ───────────────────────── password, recovery ─────────────────────────

async def change_password(request):
    data = await read_json(request)
    _require_multiuser()
    who = request.state.signed_in
    result = await _call(users.change_password, who.uid, who.sid, _text(data, "current"), _text(data, "new"))
    if isinstance(result, users.AuthError):
        return _refusal(result)
    response = _ok({"ok": True})
    _set_cookie(response, _session_cookie(who, epoch=result))      # this session stays; every other one is signed out
    return response


async def recovery_code(request):
    data = await read_json(request)
    _require_multiuser()
    result = await _call(users.new_recovery_code_for, request.state.signed_in.uid, _text(data, "password"))
    if isinstance(result, users.AuthError):
        return _refusal(result)
    return _ok({"recovery_code": result})


async def recover(request):
    data = await read_json(request)
    _require_multiuser()
    result = await _call(users.recover, _text(data, "username", max_len=100), _text(data, "recovery_code", max_len=100),
                         _text(data, "new_password"))
    if isinstance(result, users.AuthError):
        return _refusal(result)
    response = _ok({"recovery_code": result})                    # the NEW code; the old one no longer works; everyone is signed out
    _clear_cookie(response)
    return response


routes = [
    Route("/auth/status", status, methods=["GET"]),
    Route("/auth/signup", signup, methods=["POST"]),
    Route("/auth/signup/confirm", signup_confirm, methods=["POST"]),
    Route("/auth/login", login, methods=["POST"]),
    Route("/auth/logout", logout, methods=["POST"]),
    Route("/auth/logout-all", logout_all, methods=["POST"]),
    Route("/auth/change-password", change_password, methods=["POST"]),
    Route("/auth/recovery-code", recovery_code, methods=["POST"]),
    Route("/auth/recover", recover, methods=["POST"]),
]
