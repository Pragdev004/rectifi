"""Small helpers every route uses: read a JSON body, pick fields from it, and run a sync handler off the event loop."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from datetime import date

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import config
from ..db import init_db
from ..money import parse_amount
from .errors import ApiError
from .wire import to_wire

_ready_url: str | None = None
_ready_lock = threading.Lock()


def ensure_db() -> None:
    """Create the tables once per database URL, the first time a request needs them. If Postgres is down this raises
    DatabaseUnavailable (a 503) and the next request simply tries again, so the app recovers when Postgres is back."""
    global _ready_url
    url = config.database_url()
    if _ready_url == url:
        return
    with _ready_lock:
        if _ready_url != url:
            init_db()
            _ready_url = url


def reset_ready() -> None:
    global _ready_url
    _ready_url = None


async def read_json(request: Request) -> dict:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise ApiError(415, "unsupported_media", "Send this as JSON (Content-Type: application/json).")
    raw = await request.body()
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        raise ApiError(422, "invalid", "That request body is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ApiError(422, "invalid", "That request body must be a JSON object.")
    return data


def api(*, body: bool = False, status: int = 200) -> Callable:
    """Turn a plain function into a Starlette endpoint.

    The function is `fn(request)` or, with body=True, `fn(request, data)`. It runs in a worker thread (the database calls
    are synchronous), after the tables are ensured. Its return value is sent as JSON through to_wire (Decimal -> string);
    return a Response to send something else."""

    def decorate(fn: Callable) -> Callable:
        def work(*args):
            ensure_db()
            return fn(*args)

        async def handler(request: Request) -> Response:
            args = (request, await read_json(request)) if body else (request,)
            result = await run_in_threadpool(work, *args)
            if isinstance(result, Response):
                return result
            return JSONResponse(to_wire(result), status_code=status)

        handler.__name__ = fn.__name__
        return handler

    return decorate


# ───────────────────────── field readers ─────────────────────────

def _label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def text_field(data: dict, name: str, *, required: bool = True, max_len: int = 200, default: str | None = None) -> str | None:
    value = data.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ApiError(422, "invalid", f"{_label(name)} is needed.", name)
        return default
    if not isinstance(value, str):
        raise ApiError(422, "invalid", f"{_label(name)} must be text.", name)
    if len(value) > max_len:
        raise ApiError(422, "invalid", f"{_label(name)} is too long (the limit is {max_len} characters).", name)
    return value.strip()


def money_field(data: dict, name: str, *, required: bool = True) -> str | None:
    """A money amount sent as a string. The text goes straight to tools.* (parse_amount); the API never turns it into a number."""
    value = data.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ApiError(422, "invalid", f"{_label(name)} is needed.", name)
        return None
    if not isinstance(value, str):
        raise ApiError(422, "invalid", 'Send amounts as text, like "250.00".', name)
    if len(value) > 32:
        raise ApiError(422, "invalid", "That amount is too long.", name)
    text = value.strip()
    try:
        parse_amount(text)      # only to say which field is wrong; tools.* does the real parse
    except ValueError:
        raise ApiError(422, "invalid", "That doesn't look like an amount. Type digits, like 250 or 1,250.50.", name) from None
    return text


def int_field(data: dict, name: str, *, required: bool = True) -> int | None:
    value = data.get(name)
    if value is None:
        if required:
            raise ApiError(422, "invalid", f"{_label(name)} is needed.", name)
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(422, "invalid", f"{_label(name)} must be a whole number.", name)
    return value


def path_int(request: Request, name: str) -> int:
    raw = str(request.path_params.get(name, ""))
    if not raw.isdigit() or len(raw) > 9:
        raise ApiError(404, "not_found", "There is nothing at that address.")
    return int(raw)


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def date_field(data: dict, name: str, *, required: bool = True) -> str | None:
    """A YYYY-MM-DD string, passed on to tools.resolve_date (which also refuses the future)."""
    value = text_field(data, name, required=required, max_len=10)
    if value is None:
        return None
    if not _ISO_DATE.fullmatch(value):
        raise ApiError(422, "invalid", "Dates look like 2026-10-21.", name)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ApiError(422, "invalid", "That isn't a real date.", name) from None
    return value


def query(request: Request, name: str, default: str | None = None) -> str | None:
    value = request.query_params.get(name)
    return value if value not in (None, "") else default
