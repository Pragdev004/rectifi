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
UPLOAD_PATH = "/api/imports"
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
        return UPLOAD_LIMIT + UPLOAD_OVERHEAD if path == UPLOAD_PATH else JSON_LIMIT

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_api(scope["path"]) or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        limit = self.limit_for(scope["path"])
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            response = JSONResponse(error_body("too_large", too_large_message(UPLOAD_LIMIT if scope["path"] == UPLOAD_PATH else limit)),
                                    status_code=413)
            return await response(scope, receive, send)
        seen = 0

        async def counting_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge(UPLOAD_LIMIT if scope["path"] == UPLOAD_PATH else limit)
            return message

        await self.app(scope, counting_receive, send)
