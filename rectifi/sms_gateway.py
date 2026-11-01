"""The phone SMS gateway: a tiny web server your phone posts bank SMS to.

Nothing can read SMS "from a phone number". Instead the phone forwards each bank SMS to this laptop, over your own
network, and Rectifi reads it with fixed rules (tools.ingest_sms). No AI model sees any of it.

    GET  /health   {"ok": true}. No login, and no data.
    POST /sms      Authorization: Bearer <token>, JSON body up to 8 KB. Answers:
                     200 {"status": "added" | "duplicate" | "ignored" | "needs_review"}
                         (all four are 200 on purpose: the phone app retries anything that is not 2xx)
                     401 unknown or revoked token     413 body too big     422 body not understood
                     429 too many requests            503 database not reachable (the phone retries later)

It listens on 127.0.0.1 (this laptop only) unless RECTIFI_SMS_HOST says otherwise. The phone reaches it through
`tailscale serve` (HTTPS, only your own devices; README: "Use Rectifi on your phone"). Setting RECTIFI_SMS_HOST=0.0.0.0 opens
it to everyone on the same Wi-Fi, and then the token is the only lock. It is OFF unless you start it.
The token is never logged or printed, and neither is any SMS text.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import psycopg
from psycopg_pool import PoolTimeout
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import alerts, config, tools
from .db import DatabaseUnavailable, init_db

log = logging.getLogger("rectifi.sms_gateway")

MAX_BODY = 8 * 1024
RATE_LIMIT = 60                   # SMS per device per minute
RATE_WINDOW = 60.0
FAILED_AUTH_LIMIT = 30            # wrong tokens per client address per minute
DB_ERRORS = (DatabaseUnavailable, psycopg.OperationalError, PoolTimeout)

TEXT_KEYS = ("text", "message", "body")
SENDER_KEYS = ("from", "sender")
STAMP_KEYS = ("sentstamp", "sent_stamp", "sent_at", "receivedstamp", "received_stamp")


class RateLimiter:
    """At most `limit` events per `window` seconds for each key (a device id, or a client address)."""

    def __init__(self, limit: int, window: float = RATE_WINDOW, clock=time.monotonic) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: dict[object, deque] = {}

    def _recent(self, key) -> deque:
        hits = self._hits.setdefault(key, deque())
        cutoff = self.clock() - self.window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def full(self, key) -> bool:
        return len(self._recent(key)) >= self.limit

    def add(self, key) -> None:
        self._hits.setdefault(key, deque()).append(self.clock())

    def allow(self, key) -> bool:
        if self.full(key):
            return False
        self.add(key)
        return True


def _reply(status: int, payload: dict, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})})


def bearer_token(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() or None if scheme.lower() == "bearer" else None


def parse_stamp(value) -> datetime | None:
    """The phone's timestamp: epoch milliseconds (the Android app), epoch seconds, or an ISO 8601 string. None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    when: datetime | None = None
    try:
        number = float(value) if not isinstance(value, str) else (float(value) if value.strip().replace(".", "", 1).isdigit() else None)
        if number is not None:
            seconds = number / 1000 if number > 1e11 else number
            when = datetime.fromtimestamp(seconds).astimezone() if seconds > 1e8 else None
        elif isinstance(value, str):
            when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            when = when.astimezone() if when.tzinfo is None else when
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    if when is None or when > datetime.now().astimezone() + timedelta(days=1):
        return None                    # a phone with a wrong clock: better no timestamp than a wrong one
    return when


# The Android template pastes the SMS text in as it is. If the SMS itself contains a double quote, the JSON breaks.
# This rescues exactly the template the Phone page gives out: {"from": "...", "text": "...", "sentStamp": 123}
_TEMPLATE_RESCUE = re.compile(
    r'^\s*\{\s*"from"\s*:\s*"(?P<sender>.*?)"\s*,\s*"text"\s*:\s*"(?P<text>.*)"\s*,\s*"sentStamp"\s*:\s*(?P<stamp>[^,}\s]+)\s*\}\s*$',
    re.DOTALL,
)


def parse_payload(body: bytes) -> tuple[str, str | None, datetime | None]:
    """JSON body -> (text, sender, sent_at). Keys are matched without regard to case. Raises ValueError if unusable."""
    try:
        raw = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The body is not valid JSON.") from exc
    try:
        # strict=False lets a raw newline inside the SMS text through; phone apps often paste the text in as it is
        data = json.loads(raw, strict=False)
    except ValueError as exc:
        rescued = _TEMPLATE_RESCUE.match(raw)
        if rescued is None:
            raise ValueError("The body is not valid JSON.") from exc
        data = {"from": rescued["sender"], "text": rescued["text"], "sentStamp": rescued["stamp"].strip('"')}
    if not isinstance(data, dict):
        raise ValueError("The body must be a JSON object.")
    lowered = {str(k).lower(): v for k, v in data.items()}
    text = next((lowered[k] for k in TEXT_KEYS if lowered.get(k) not in (None, "")), None)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("No SMS text found. Send it as \"text\" (or \"message\" or \"body\").")
    sender = next((lowered[k] for k in SENDER_KEYS if lowered.get(k) not in (None, "")), None)
    sender = str(sender) if isinstance(sender, (str, int)) and not isinstance(sender, bool) else None
    stamp = next((parse_stamp(lowered[k]) for k in STAMP_KEYS if k in lowered and parse_stamp(lowered[k])), None)
    return text, sender, stamp


async def _read_body(request: Request) -> bytes | None:
    """The body, or None if it is bigger than MAX_BODY. Stops reading as soon as the limit is passed."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return None
    return bytes(body)


def create_app() -> Starlette:
    device_limit = RateLimiter(RATE_LIMIT)
    failures = RateLimiter(FAILED_AUTH_LIMIT)

    async def health(request: Request) -> JSONResponse:
        return _reply(200, {"ok": True})

    async def sms(request: Request) -> JSONResponse:
        client = request.client.host if request.client else "?"
        if failures.full(client):
            return _reply(429, {"error": "too many wrong tokens, wait a minute"}, {"Retry-After": "60"})
        token = bearer_token(request)
        try:
            device = await run_in_threadpool(tools.authenticate_device, token) if token else None
        except DB_ERRORS:
            return _reply(503, {"error": "database not reachable"}, {"Retry-After": "30"})
        if device is None:
            failures.add(client)
            return _reply(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
        if not device_limit.allow(device["id"]):
            return _reply(429, {"error": "too many messages, slow down"}, {"Retry-After": "60"})
        body = await _read_body(request)
        if body is None:
            return _reply(413, {"error": f"body is larger than {MAX_BODY // 1024} KB"})
        try:
            text, sender, sent_at = parse_payload(body)
        except ValueError as exc:
            return _reply(422, {"error": str(exc)})
        try:
            result = await run_in_threadpool(tools.ingest_sms, device["id"], sender, text, sent_at)
        except DB_ERRORS:
            return _reply(503, {"error": "database not reachable"}, {"Retry-After": "30"})
        except Exception as exc:                       # never put the SMS or the token in a log line
            log.error("could not process an SMS (%s)", type(exc).__name__)
            return _reply(500, {"error": "internal error"})
        reply = _reply(200, {"status": result["status"]})
        if result["status"] == "added":                # a new payment: look for alerts after the phone has its answer
            reply.background = BackgroundTask(alerts.sweep_and_deliver, "sms")
        return reply

    @asynccontextmanager
    async def lifespan(app):
        try:
            await run_in_threadpool(init_db)
            await run_in_threadpool(alerts.ensure_watermark)
        except DB_ERRORS:
            log.warning("The database is not reachable yet. /sms answers 503 (the phone retries) until it is.")
        if config.sms_host_is_open():
            log.warning("Rectifi phone gateway is listening on %s, so anyone on your network with a device token can send "
                        "SMS to it. Prefer RECTIFI_SMS_HOST=127.0.0.1 and `python scripts/dev.py tailscale`.", config.sms_host())
        else:
            log.warning("Rectifi phone gateway is on, listening on %s (this laptop only).", config.sms_host())
        yield

    return Starlette(
        routes=[Route("/health", health, methods=["GET"]), Route("/sms", sms, methods=["POST"])],
        lifespan=lifespan,
    )


# ───────────────────────── helpers for the Phone page and doctor ─────────────────────────

def gateway_reachable(port: int | None = None, timeout: float = 1.0) -> bool:
    """Is a Rectifi gateway answering on this laptop? (GET /health on 127.0.0.1. No data is read.)"""
    url = f"http://127.0.0.1:{port or config.sms_port()}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            return reply.status == 200 and json.load(reply).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def send_test_sms(token: str, port: int | None = None, timeout: float = 5.0) -> tuple[int | None, str]:
    """Post a harmless message (not a transaction, so the gateway ignores it and stores nothing) to the local
    gateway with this token. Returns (http status or None, plain-English result). Proves: the gateway is up,
    the token works, and the JSON is understood."""
    body = json.dumps({"from": "RECTIFI-TEST", "text": "Rectifi test message. This is not a bank SMS.",
                       "sentStamp": int(time.time() * 1000)}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port or config.sms_port()}/sms", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return reply.status, str(json.load(reply).get("status"))
    except urllib.error.HTTPError as exc:
        return exc.code, {401: "the gateway did not accept this token", 429: "too many requests"}.get(exc.code, f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError):
        return None, "the gateway is not reachable on this laptop"


_CGNAT = ipaddress.ip_network("100.64.0.0/10")        # Tailscale gives every device an address in this range


def private_ipv4s() -> list[dict]:
    """This laptop's private IPv4 addresses as [{'ip', 'label'}], a Tailscale-style 100.64.0.0/10 address first.

    Socket calls only, so it works the same on Windows, macOS and Linux. Opening a UDP socket and "connecting" it
    sends no packet: it only makes the operating system pick the address it would use to reach the internet."""
    found: list[str] = []

    def add(ip: str) -> None:
        if ip not in found:
            found.append(ip)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            add(probe.getsockname()[0])
    except OSError:
        pass
    host = socket.gethostname()
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(host)[2]:
            add(ip)
    except OSError:
        pass

    tailscale, local = [], []
    for ip in found:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if address.is_loopback or address.is_link_local:
            continue
        if address in _CGNAT:
            tailscale.append({"ip": ip, "label": "Tailscale (works on any network once both devices have the app)"})
        elif address.is_private:
            local.append({"ip": ip, "label": "Local network (Wi-Fi or hotspot; phone and laptop must be on the same one)"})
    return tailscale + local


def main() -> None:
    """`python -m rectifi.sms_gateway`. `dev.py phone` runs uvicorn on the same factory."""
    import uvicorn

    uvicorn.run(create_app(), host=config.sms_host(), port=config.sms_port(), log_level="info")


if __name__ == "__main__":
    main()
