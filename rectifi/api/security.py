"""The guards in front of every request. Pure ASGI, no third-party code.

Threats: a web page on another site calling the API from the browser (CSRF), DNS rebinding against 127.0.0.1, other devices
and people on the tailnet, and local processes. Layers, outermost first:

  SecurityHeaders  CSP, nosniff, no-referrer on every response; no-store on /api
  HostGuard        the Host header must be one of RECTIFI_ALLOWED_HOSTS (the DNS-rebinding defence: a rebinding page still
                   sends Host: evil.example)
  TailscaleLock    optional (RECTIFI_TAILSCALE_USER): only that Tailscale login, or a browser on this laptop
  ApiGuard         /api needs the X-Rectifi-Request header (a cross-site page can't add it without a CORS preflight, and we
                   never answer one: OPTIONS is 403 and no Access-Control-* header is ever sent), rejects
                   Sec-Fetch-Site: cross-site, and checks Origin on writes
  BodyLimit        64 KB for JSON, 10 MB for a statement upload, counted as the bytes arrive

Tailscale traffic reaches us from 127.0.0.1, so nothing here trusts an IP address.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, PlainTextResponse

from .errors import BodyTooLarge, error_body, too_large_message

REQUEST_HEADER = "x-rectifi-request"
DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1,::1,*.ts.net"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
JSON_LIMIT = 64 * 1024
UPLOAD_LIMIT = 10 * 1024 * 1024
UPLOAD_OVERHEAD = 64 * 1024              # multipart boundaries and form fields around the file
UPLOAD_PATHS = frozenset({"/api/imports", "/api/knowledge/docs"})      # multipart uploads: statements, knowledge notes
UPLOAD_PATH = "/api/imports"             # kept for older imports of this name
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; font-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _split_list(value: str | None) -> list[str]:
    return [part.strip().lower() for part in (value or "").split(",") if part.strip()]


def allowed_hosts() -> list[str]:
    return _split_list(os.getenv("RECTIFI_ALLOWED_HOSTS") or DEFAULT_ALLOWED_HOSTS)


def allowed_origins() -> list[str]:
    return [o.rstrip("/") for o in _split_list(os.getenv("RECTIFI_ALLOWED_ORIGINS"))]


def hostname_of(host_header: str) -> str:
    """'127.0.0.1:8501' -> '127.0.0.1', '[::1]:8501' -> '::1', 'Laptop.tail1.ts.net' -> 'laptop.tail1.ts.net'."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else h
    if h.count(":") == 1:
        return h.split(":", 1)[0]
    return h


def host_allowed(host_header: str, patterns: list[str]) -> bool:
    name = hostname_of(host_header)
    if not name:
        return False
    for pattern in patterns:
        if pattern.startswith("*."):
            if name.endswith(pattern[1:]) and len(name) > len(pattern) - 1:
                return True
        elif name == pattern:
            return True
    return False


def origin_allowed(origin: str, host_header: str, extra: list[str]) -> bool:
    """May a browser page at `origin` call us (given the Host it reached us on)?"""
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if origin.rstrip("/").lower() in extra:
        return True
    host = parts.hostname.lower()
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return True                                    # a browser on this laptop (also Vite's dev server on 5173)
    if parts.scheme == "https" and host.endswith(".ts.net"):
        return True                                    # Tailscale Serve: whether it keeps the Host header is not confirmed
    return parts.netloc.lower() == (host_header or "").strip().lower()


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _forbidden(message: str) -> JSONResponse:
    return JSONResponse(error_body("forbidden", message), status_code=403)


class SecurityHeaders:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        api = _is_api(scope["path"])

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CSP
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                if api and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class HostGuard:
    def __init__(self, app, hosts: list[str] | None = None) -> None:
        self.app = app
        self.hosts = hosts if hosts is not None else allowed_hosts()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        host = Headers(scope=scope).get("host", "")
        if not host_allowed(host, self.hosts):
            if _is_api(scope["path"]):
                response = JSONResponse(error_body("bad_host", "This address isn't allowed to use Rectifi."), status_code=400)
            else:
                response = PlainTextResponse("This address isn't allowed to use Rectifi.", status_code=400)
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


class TailscaleLock:
    """Optional. With RECTIFI_TAILSCALE_USER=<login>, a request is allowed if Tailscale Serve says it came from that login
    (the Tailscale-User-Login header; Serve strips any copy a client sends), or if it has no such header and was addressed to
    this laptop itself (a browser on the laptop). Devices shared to other people, and tagged devices, get 403."""

    def __init__(self, app, user: str) -> None:
        self.app = app
        self.user = user.strip().lower()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        login = headers.get("tailscale-user-login")
        if login is not None:
            ok = login.strip().lower() == self.user
        else:
            ok = hostname_of(headers.get("host", "")) in LOOPBACK_HOSTS
        if not ok:
            if _is_api(scope["path"]):
                response = _forbidden("This Rectifi is locked to one Tailscale account.")
            else:
                response = PlainTextResponse("This Rectifi is locked to one Tailscale account.", status_code=403)
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


class ApiGuard:
    def __init__(self, app, extra_origins: list[str] | None = None) -> None:
        self.app = app
        self.extra_origins = extra_origins if extra_origins is not None else allowed_origins()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_api(scope["path"]) or scope["path"] == "/api/health":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        method = scope["method"]
        problem = None
        if method == "OPTIONS":
            problem = "Rectifi doesn't answer cross-site requests."
        elif headers.get(REQUEST_HEADER) != "1":
            problem = "That request didn't come from the Rectifi app."
        elif headers.get("sec-fetch-site", "").lower() == "cross-site":
            problem = "Rectifi doesn't answer requests from other sites."
        elif method in WRITE_METHODS:
            origin = headers.get("origin")
            if origin is not None:
                if not origin_allowed(origin, headers.get("host", ""), self.extra_origins):
                    problem = "Rectifi doesn't accept changes from that site."
            elif headers.get("sec-fetch-site") is not None:
                problem = "Rectifi doesn't accept changes from that site."
        if problem:
            return await _forbidden(problem)(scope, receive, send)
        await self.app(scope, receive, send)


class BodyLimit:
    """Refuse a body that is too big: by Content-Length at once, and by counting bytes as they arrive (a chunked upload has
    no Content-Length). The handler that is reading the body gets BodyTooLarge, which the error handlers turn into a 413."""

    def __init__(self, app) -> None:
        self.app = app

    @staticmethod
    def limit_for(path: str) -> int:
        return UPLOAD_LIMIT + UPLOAD_OVERHEAD if path in UPLOAD_PATHS else JSON_LIMIT

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_api(scope["path"]) or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        limit = self.limit_for(scope["path"])
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            response = JSONResponse(error_body("too_large", too_large_message(UPLOAD_LIMIT if scope["path"] in UPLOAD_PATHS else limit)),
                                    status_code=413)
            return await response(scope, receive, send)
        seen = 0

        async def counting_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge(UPLOAD_LIMIT if scope["path"] in UPLOAD_PATHS else limit)
            return message

        await self.app(scope, counting_receive, send)


# ═══ WS-HOST: begin ═══
# SessionLock (the login for public mode), the chart-frame CSP route and any other new guard go here. WS-HOST only.
# WS-AUTH also works inside this block: multi-user sessions, binding the person, and the FRIEND_ROUTES gate (D7).
import re  # noqa: E402

from starlette.concurrency import run_in_threadpool  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402

from .. import config as _config  # noqa: E402
from .. import userctx as _userctx  # noqa: E402
from . import auth as _auth  # noqa: E402
from .errors import DB_DOWN_HINT, DB_ERRORS  # noqa: E402

# Open without a session: the health check, the calls the login and sign-up pages make, and the public facts page.
# Everything else under /api (including /api/auth/logout-all) needs a session. Static files and the page shell hold no
# data and stay public.
SESSION_EXEMPT = frozenset({"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout", "/api/auth/signup",
                            "/api/auth/signup/confirm", "/api/auth/recover", "/api/privacy"})

# ── D7: AI is owner-only, deny by default ──
# In multi-user mode a friend (any account but the owner) reaches ONLY the routes below. Everything else under /api, including
# a route somebody adds later, answers 403 until a line is added here on purpose. Each entry: (methods, path pattern). A
# pattern is a full match; {id} stands for one path segment. The owner reaches everything.
_FRIEND_RULES = (
    # sign-in pages and your own account (the routes check the password or code themselves)
    ("GET POST", "/api/auth/{id}"), ("POST", "/api/auth/signup/confirm"),
    ("GET POST DELETE", "/api/account"), ("GET", "/api/account/{id}"), ("GET", "/api/privacy"),
    # what every page needs to start, the home page, the weekly review, insights (no "explain": that calls a model)
    ("GET", "/api/meta"), ("GET", "/api/overview"), ("GET", "/api/review"), ("PUT", "/api/review/commitment"),
    ("GET", "/api/insights"), ("POST", "/api/insights/apply"),
    # the ledger, adding by hand or by pasting an SMS, and the phone inbox
    ("GET POST", "/api/transactions"), ("PATCH DELETE", "/api/transactions/{id}"), ("POST", "/api/transactions/{id}/apply-similar"),
    ("POST", "/api/sms/preview"), ("POST", "/api/sms/save"), ("GET", "/api/inbox/{id}/prefill"),
    # statement imports (the model sorting job /api/classify/* is not here)
    ("POST", "/api/imports"), ("POST", "/api/imports/preview"), ("POST", "/api/imports/commit"), ("GET", "/api/imports/{id}"),
    ("POST", "/api/imports/{id}/undo"), ("GET", "/api/imports/{id}/rows"),
    # budgets, accounts, the phone SMS gateway pairing
    ("GET PUT", "/api/budgets"), ("POST", "/api/budgets/copy"), ("GET POST", "/api/accounts"), ("POST", "/api/accounts/{id}/balances"),
    ("GET", "/api/phone/status"), ("GET POST", "/api/phone/devices"), ("POST", "/api/phone/devices/{id}/revoke"),
    ("POST", "/api/phone/test"), ("GET", "/api/phone/inbox"), ("POST", "/api/phone/inbox/{id}/dismiss"),
    # alerts, leaks, merchant patterns, phone push (no "explain": that calls a model)
    ("GET", "/api/alerts"), ("GET", "/api/alerts/today"), ("POST", "/api/alerts/read-all"), ("GET", "/api/alerts/presets"),
    ("GET PUT", "/api/alerts/prefs"), ("GET POST", "/api/alerts/rules"), ("PATCH DELETE", "/api/alerts/rules/{id}"),
    ("POST", "/api/alerts/{id}/read"), ("POST", "/api/alerts/{id}/mute"), ("POST", "/api/alerts/{id}/snooze"),
    ("GET", "/api/leaks"), ("GET POST", "/api/merchants/patterns"), ("DELETE", "/api/merchants/patterns/{id}"),
    ("GET", "/api/push/key"), ("GET POST", "/api/push/subscriptions"), ("DELETE", "/api/push/subscriptions/{id}"), ("POST", "/api/push/test"),
)


def _compile_rules() -> tuple:
    out = []
    for methods, pattern in _FRIEND_RULES:
        regex = re.escape(pattern).replace(re.escape("{id}"), r"[^/]+")
        out.append((frozenset(methods.split()), re.compile("^" + regex + "$")))
    return tuple(out)


FRIEND_ROUTES = _compile_rules()


def friend_allowed(method: str, path: str) -> bool:
    """May a friend (not the owner) call this? Only what _FRIEND_RULES names. Anything else, including a route added later, is no."""
    method = method.upper()
    return any(method in methods and pattern.match(path) for methods, pattern in FRIEND_ROUTES)


class SessionLock:
    """When a password is set (or the app is public, or multi-user), /api needs a valid rectifi_session cookie, else 401.

    Sits right after HostGuard and before ApiGuard. OPTIONS passes through so ApiGuard can answer 403 as always. If the
    login is switched on but not set up, every guarded call is refused (503): it fails closed, never open.

    Multi-user mode adds three things for each guarded call: the cookie is checked against the database (the session row,
    the epoch, the secret that opens the data key); the person is bound with userctx.bound() for the rest of the request, so
    every database call is theirs; and a friend gets 403 for anything not in FRIEND_ROUTES. request.state.uid is the real
    integer id there. In laptop and single-password mode it stays "owner" and nothing is bound (user 1)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope["path"] if scope["type"] == "http" else ""
        if scope["type"] == "http":
            scope.setdefault("state", {})["uid"] = "owner"      # whose memory: one signed-in owner (read as request.state.uid)
        if (scope["type"] != "http" or not _is_api(path) or path in SESSION_EXEMPT or scope["method"] == "OPTIONS"
                or not _auth.login_required()):
            return await self.app(scope, receive, send)
        if _auth.startup_problem():
            response = JSONResponse(error_body("auth_misconfigured", _auth.MESSAGES["auth_misconfigured"]), status_code=503)
            return await response(scope, receive, send)
        cookie = Headers(scope=scope).get("cookie")
        try:
            if _config.multiuser():
                who = await run_in_threadpool(_auth.resolve_user, cookie)
                ok = who is not None
            else:
                who = None
                ok = await run_in_threadpool(_auth.session_valid, cookie)
        except DB_ERRORS:
            response = JSONResponse(error_body("db_unavailable", f"Rectifi can't reach its database. {DB_DOWN_HINT}"),
                                    status_code=503, headers={"Retry-After": "10"})
            return await response(scope, receive, send)
        if not ok:
            response = JSONResponse(error_body("auth_required", _auth.MESSAGES["auth_required"]), status_code=401)
            return await response(scope, receive, send)
        if who is None:
            return await self.app(scope, receive, send)
        if not who.is_owner and not friend_allowed(scope["method"], path):
            return await _forbidden("This part of Rectifi is only for the person who runs it.")(scope, receive, send)
        scope["state"].update(uid=who.uid, username=who.username, signed_in=who)
        with _userctx.bound(who.ctx):
            await self.app(scope, receive, send)


def _assert_safe_role() -> None:
    """D11: multi-user or public mode refuses to run as a superuser or BYPASSRLS database role (it would skip row level
    security without a sound). If the database is down right now there is nothing to check; init_db checks again at start-up."""
    from .. import db

    if not (_config.multiuser() or _config.public_mode()):
        return
    try:
        db.assert_app_role(strict=True)
    except DB_ERRORS:
        pass


def install_session_lock(app) -> None:
    """Call once in create_app, right after the Starlette object is built. Refuses to start (StartupRefused) when the app is
    public, multi-user, or a password hash is set, without a usable login setup, and (UnsafeDatabaseRole) when multi-user or
    public mode would run as a superuser role. Then adds SessionLock after HostGuard."""
    _auth.check_startup()
    _assert_safe_role()
    if any(m.cls is SessionLock for m in app.user_middleware):
        return                                          # already installed (safe to call twice)
    if app.middleware_stack is not None:
        raise RuntimeError("install_session_lock must run before the first request.")
    at = next(i for i, m in enumerate(app.user_middleware) if m.cls is HostGuard)
    app.user_middleware.insert(at + 1, Middleware(SessionLock))
# ═══ WS-HOST: end ═══
