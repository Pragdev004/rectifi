"""Password login for when Rectifi can be reached from beyond this laptop. Standard library only.

Why it exists: on your laptop only you can reach the app, so there is no login. Once it runs on a server (Oracle, or
Tailscale Funnel with a public address) anyone who finds the address could read your money. This is the lock.

The pieces:

  password hash   RECTIFI_PASSWORD_HASH = scrypt$16384$8$1$<salt>$<hash>   made by `python scripts/dev.py set-password`
  session secret  RECTIFI_SESSION_SECRET (random, at least 32 characters) signs the cookie
  cookie          rectifi_session = base64url(json{iat, exp, epoch}) + "." + base64url(HMAC-SHA256)
                  HttpOnly, SameSite=Strict, Path=/, 7 days, Secure in public mode
  epoch           app_settings.session_epoch. A cookie only works while its epoch matches, so bumping the number signs
                  everyone out at once ("sign out everywhere")
  login limiter   global (every request reaches us from 127.0.0.1, so there is no IP to count): 5 failures in a minute
                  adds a 2 second pause to each try; 10 failures in 15 minutes locks sign-in for 15 minutes

Nothing here logs or prints a password, a hash or a secret.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import time

from .. import config
from ..db import connect

COOKIE_NAME = "rectifi_session"
SESSION_SECONDS = 7 * 24 * 3600
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
MIN_SECRET_LENGTH = 32
HASH_PREFIX = "scrypt"
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN, SALT_BYTES = 2**14, 8, 1, 32, 16

# Login limiter (tunables)
DELAY_AFTER = 5                # failures within DELAY_WINDOW that start the pause
DELAY_WINDOW = 60              # seconds
DELAY_SECONDS = 2.0
LOCK_AFTER = 10                # failures within LOCK_WINDOW that lock sign-in
LOCK_WINDOW = 15 * 60          # seconds
LOCK_SECONDS = 15 * 60

EPOCH_KEY = "session_epoch"
LOCK_KEY = "login_lock"

MESSAGES = {
    "auth_required": "Sign in to use Rectifi.",
    "invalid_password": "That password isn't right.",
    "login_locked": "Too many tries. Wait 15 minutes.",
    "auth_off": "Sign-in isn't turned on for this Rectifi.",
    "auth_misconfigured": "Sign-in is switched on but not set up. On the server run `python scripts/dev.py set-password`.",
}


class StartupRefused(RuntimeError):
    """Public mode (or a password hash) without a working login setup. The message says how to fix it."""


def _now() -> float:
    return time.time()


async def pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


# ───────────────────────── password hashing ─────────────────────────

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length, maxmem=128 * r * (n + p + 2) + (1 << 20))


def hash_password(password: str, salt: bytes | None = None) -> str:
    """'scrypt$16384$8$1$<salt>$<hash>' (url-safe base64, no padding). A new random salt each time."""
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN)
    return f"{HASH_PREFIX}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def parse_hash(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    """The parts of a stored hash, or None if it is not a well-formed one of ours (including absurd cost settings)."""
    parts = (stored or "").strip().split("$")
    if len(parts) != 6 or parts[0] != HASH_PREFIX:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _b64d(parts[4]), _b64d(parts[5])
    except (ValueError, binascii.Error):
        return None
    if not (2**10 <= n <= 2**16 and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4):
        return None
    if len(salt) < 8 or not 16 <= len(digest) <= 64:
        return None
    return n, r, p, salt, digest


def verify_password(password: str, stored: str) -> bool:
    """True if `password` matches the stored hash. Never raises on bad input; compares in constant time."""
    parsed = parse_hash(stored)
    if parsed is None or not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH:
        return False
    n, r, p, salt, digest = parsed
    return hmac.compare_digest(_scrypt(password, salt, n, r, p, len(digest)), digest)


def password_problem(password: str) -> str | None:
    """Why this password can't be used, in a plain sentence, or None if it is fine."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters. A few random words in a row works well."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    return None


def new_secret() -> str:
    return secrets.token_urlsafe(48)


# ───────────────────────── settings and start-up ─────────────────────────

def password_hash() -> str:
    return os.getenv("RECTIFI_PASSWORD_HASH", "").strip()


def session_secret() -> str:
    return os.getenv("RECTIFI_SESSION_SECRET", "").strip()


def login_required() -> bool:
    """True when /api needs a session: a password is set, or the app is reachable beyond this laptop (public mode)."""
    return bool(password_hash()) or config.public_mode()


def startup_problem() -> str | None:
    """Why the login can't work as configured (None when it can, or when it is not needed)."""
    if not login_required():
        return None
    if parse_hash(password_hash()) is None:
        return ("Rectifi is reachable beyond this laptop (or a password is set) but RECTIFI_PASSWORD_HASH is missing or not "
                "valid. Run `python scripts/dev.py set-password` and put the two lines it prints into your environment.")
    if len(session_secret()) < MIN_SECRET_LENGTH:
        return (f"RECTIFI_SESSION_SECRET is missing or shorter than {MIN_SECRET_LENGTH} characters. "
                "Run `python scripts/dev.py set-password`; it makes a new one.")
    return None


def check_startup() -> None:
    problem = startup_problem()
    if problem:
        raise StartupRefused(problem)


# ───────────────────────── session cookie ─────────────────────────

def _sign(secret: str, body: str) -> str:
    return _b64e(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def make_session(secret: str, epoch: int, *, now: float | None = None, ttl: int = SESSION_SECONDS) -> str:
    issued = int(_now() if now is None else now)
    body = _b64e(json.dumps({"iat": issued, "exp": issued + ttl, "epoch": int(epoch)}, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(secret, body)}"


def read_session(value: str | None, secret: str, *, now: float | None = None) -> dict | None:
    """The payload of a cookie whose signature is right and which has not expired, else None. Does not check the epoch."""
    if not value or not secret or value.count(".") != 1 or len(value) > 1024:
        return None
    body, signature = value.split(".")
    try:
        if not hmac.compare_digest(signature.encode("ascii"), _sign(secret, body).encode("ascii")):
            return None
        data = json.loads(_b64d(body))
    except (ValueError, UnicodeError, binascii.Error):
        return None
    if not isinstance(data, dict) or not all(type(data.get(k)) is int for k in ("iat", "exp", "epoch")):
        return None
    current = _now() if now is None else now
    if data["exp"] <= current or data["iat"] > current + 300:
        return None
    return data


def cookie_value(header: str | None, name: str = COOKIE_NAME) -> str | None:
    for part in (header or "").split(";"):
        key, sep, val = part.strip().partition("=")
        if sep and key == name:
            return val.strip('"')
    return None


def cookie_secure() -> bool:
    return config.public_mode()


def current_epoch() -> int:
    from .inputs import ensure_db

    ensure_db()
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (EPOCH_KEY,)).fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def bump_epoch() -> int:
    """Sign everyone out: every cookie issued so far stops working. Returns the new epoch."""
    from .inputs import ensure_db

    ensure_db()
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (%s, '1'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = to_jsonb(COALESCE((app_settings.value #>> '{}')::int, 0) + 1), updated_at = now() "
            "RETURNING value", (EPOCH_KEY,)).fetchone()
    return int(row["value"])


def new_session_cookie() -> str:
    return make_session(session_secret(), current_epoch())


def session_valid(cookie_header: str | None) -> bool:
    """Is this Cookie header a live session? Checks the signature and expiry first (no database), then the epoch."""
    data = read_session(cookie_value(cookie_header), session_secret())
    return data is not None and data["epoch"] == current_epoch()


# ───────────────────────── login limiter ─────────────────────────

class Outcome:
    """What one sign-in try came to: ok / invalid / locked, how long to wait before answering, and (locked) retry seconds."""

    def __init__(self, status: str, delay: float = 0.0, retry_after: int = 0) -> None:
        self.status, self.delay, self.retry_after = status, delay, retry_after


class LoginLimiter:
    """Counts failed tries across the whole app. Kept in memory and copied to app_settings.login_lock so that a restart
    doesn't hand an attacker a fresh set of tries. A database problem never blocks a try; memory keeps counting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded = False
        self._failures: list[float] = []
        self._locked_until = 0.0

    def reset_memory(self) -> None:
        with self._lock:
            self._loaded, self._failures, self._locked_until = False, [], 0.0

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            from .inputs import ensure_db

            ensure_db()
            with connect() as conn:
                row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (LOCK_KEY,)).fetchone()
            saved = row["value"] if row and isinstance(row["value"], dict) else {}
            self._failures = [float(t) for t in saved.get("failures", []) if isinstance(t, (int, float))]
            self._locked_until = float(saved.get("locked_until", 0) or 0)
        except Exception:                                  # a broken row or a database problem: start from empty
            pass
        self._loaded = True

    def _save(self) -> None:
        try:
            with connect() as conn:
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                    (LOCK_KEY, json.dumps({"failures": self._failures, "locked_until": self._locked_until})))
        except Exception:
            pass

    def attempt(self, password: str, stored_hash: str) -> Outcome:
        with self._lock:
            self._load()
            now = _now()
            if self._locked_until > now:
                return Outcome("locked", retry_after=max(1, math.ceil(self._locked_until - now)))
            self._failures = [t for t in self._failures if now - t < LOCK_WINDOW]
            delay = DELAY_SECONDS if sum(1 for t in self._failures if now - t < DELAY_WINDOW) >= DELAY_AFTER else 0.0
            if verify_password(password, stored_hash):
                if self._failures or self._locked_until:
                    self._failures, self._locked_until = [], 0.0
                    self._save()
                return Outcome("ok", delay=delay)
            self._failures.append(now)
            if len(self._failures) >= LOCK_AFTER:
                self._failures, self._locked_until = [], now + LOCK_SECONDS
                self._save()
                return Outcome("locked", retry_after=LOCK_SECONDS)
            self._save()
            return Outcome("invalid", delay=delay)


limiter = LoginLimiter()
