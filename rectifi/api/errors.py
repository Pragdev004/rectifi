"""One error shape for the whole API: {"error": {"code", "message", "field"}}. The message is one plain sentence
that is safe to show. Nothing here ever includes a request body, a password or a token."""

from __future__ import annotations

import logging

import psycopg
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from ..db import DatabaseUnavailable
from ..tools import RectifiError

log = logging.getLogger("rectifi.api")

DB_ERRORS = (DatabaseUnavailable, psycopg.OperationalError, PoolTimeout)

DB_DOWN_HINT = ("Your data is safe. Postgres just isn't running. Start it with `python scripts/dev.py db` (Docker), "
                "or check DATABASE_URL in .env. Then try again.")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field


class BodyTooLarge(Exception):
    """Raised by the body-size guard while a handler is reading the request."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"body over {limit} bytes")
        self.limit = limit


def error_body(code: str, message: str, field: str | None = None) -> dict:
    return {"error": {"code": code, "message": message, "field": field}}


def error_response(status: int, code: str, message: str, field: str | None = None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(error_body(code, message, field), status_code=status, headers=headers)


def too_large_message(limit: int) -> str:
    if limit >= 1024 * 1024:
        return f"That is larger than {limit // (1024 * 1024)} MB."
    return f"That is larger than {limit // 1024} KB."


def _is_api(request: Request) -> bool:
    return request.url.path == "/api" or request.url.path.startswith("/api/")


async def api_error_handler(request: Request, exc: ApiError):
    return error_response(exc.status, exc.code, exc.message, exc.field)


async def rectifi_error_handler(request: Request, exc: RectifiError):
    from ..pdf_statement import NEEDS_PASSWORD_MESSAGE, WRONG_PASSWORD_MESSAGE

    text = str(exc)
    if text == NEEDS_PASSWORD_MESSAGE:
        return error_response(422, "pdf_password_needed", text)
    if text == WRONG_PASSWORD_MESSAGE:
        return error_response(422, "pdf_password_wrong", text)
    return error_response(422, "invalid", text)


async def db_error_handler(request: Request, exc: Exception):
    base = str(exc) if isinstance(exc, DatabaseUnavailable) else "Rectifi can't reach its database."
    return error_response(503, "db_unavailable", f"{base} {DB_DOWN_HINT}", headers={"Retry-After": "10"})


async def body_too_large_handler(request: Request, exc: BodyTooLarge):
    return error_response(413, "too_large", too_large_message(exc.limit))


async def http_exception_handler(request: Request, exc: HTTPException):
    if not _is_api(request):
        return PlainTextResponse(exc.detail if isinstance(exc.detail, str) else "Not found", status_code=exc.status_code)
    if exc.status_code == 404:
        return error_response(404, "not_found", "There is nothing at that address.")
    if exc.status_code == 405:
        return error_response(405, "method_not_allowed", "That address doesn't accept this kind of request.",
                              headers=dict(exc.headers or {}))
    return error_response(exc.status_code, "error", "Something is wrong with that request.")


async def unexpected_error_handler(request: Request, exc: Exception):
    # Type and path only: never the message (it can hold a body) and never the body itself.
    log.error("unexpected %s on %s %s", type(exc).__name__, request.method, request.url.path)
    return error_response(500, "internal", "Something went wrong on the laptop. Nothing was lost; try again.")


EXCEPTION_HANDLERS = {
    ApiError: api_error_handler,
    RectifiError: rectifi_error_handler,
    DatabaseUnavailable: db_error_handler,
    psycopg.OperationalError: db_error_handler,
    PoolTimeout: db_error_handler,
    BodyTooLarge: body_too_large_handler,
    HTTPException: http_exception_handler,
    Exception: unexpected_error_handler,
}
