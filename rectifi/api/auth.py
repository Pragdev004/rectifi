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
  login limiter   (single password) global (every request reaches us from 127.0.0.1, so there is no IP to count): 5 failures in a minute
                  adds a 2 second pause to each try; 10 failures in 15 minutes locks sign-in for 15 minutes

Multi-user mode (RECTIFI_MULTIUSER=1) is the same cookie with three more fields: uid, sid (a user_sessions row) and k (the
secret that opens that row's wrapped data key). The checks live in rectifi/users.py; the single password is not used and the
limiter is per account. See docs/MULTIUSER_PLAN.md section 4.

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
    "signup_off": "Sign-up isn't turned on for this Rectifi.",
    "not_multiuser": "This Rectifi has one shared password. Change it with `python scripts/dev.py set-password` on the machine it runs on.",
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


def multiuser() -> bool:
    return config.multiuser()


def login_required() -> bool:
    """True when /api needs a session: a password is set, the app is reachable beyond this laptop (public mode), or it is
    multi-user (always: there are accounts)."""
    return bool(password_hash()) or config.public_mode() or config.multiuser()


def startup_problem() -> str | None:
    """Why the login can't work as configured (None when it can, or when it is not needed)."""
    if not login_required():
        return None
    if config.multiuser():                                  # accounts replace the single password; only the cookie secret is needed
        if len(session_secret()) < MIN_SECRET_LENGTH:
            return (f"RECTIFI_SESSION_SECRET is missing or shorter than {MIN_SECRET_LENGTH} characters. "
                    "Run `python scripts/dev.py set-password`; it makes a new one.")
        return config.blind_key_problem()
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
            "ON CONFLICT (user_id, key) DO UPDATE SET value = to_jsonb(COALESCE((app_settings.value #>> '{}')::int, 0) + 1), updated_at = now() "
            "RETURNING value", (EPOCH_KEY,)).fetchone()
    return int(row["value"])


def new_session_cookie() -> str:
    return make_session(session_secret(), current_epoch())


def session_valid(cookie_header: str | None) -> bool:
    """Is this Cookie header a live session? Checks the signature and expiry first (no database), then the epoch.
    Single-password mode only: a multi-user cookie (it has a uid) is never accepted here."""
    data = read_session(cookie_value(cookie_header), session_secret())
    return data is not None and "uid" not in data and data["epoch"] == current_epoch()


# ───────────────────────── multi-user cookie ─────────────────────────

def make_user_session(secret: str, signed_in, *, epoch: int | None = None, now: float | None = None, ttl: int = SESSION_SECONDS) -> str:
    """Cookie for a multi-user session: the usual fields plus uid, sid and k. `signed_in` is a users.SignedIn."""
    issued = int(_now() if now is None else now)
    payload = {"iat": issued, "exp": issued + ttl, "epoch": int(signed_in.epoch if epoch is None else epoch),
               "uid": int(signed_in.uid), "sid": signed_in.sid, "k": _b64e(signed_in.k)}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(secret, body)}"


def read_user_session(value: str | None, secret: str, *, now: float | None = None) -> dict | None:
    """Like read_session, but also needs uid (int), sid and k (text). Does not look in the database."""
    data = read_session(value, secret, now=now)
    if data is None or type(data.get("uid")) is not int or not isinstance(data.get("sid"), str) or not isinstance(data.get("k"), str):
        return None
    return data


def resolve_user(cookie_header: str | None):
    """The users.SignedIn behind this Cookie header, or None for anything wrong: tampered, expired, deleted session, wrong
    epoch, another person's uid. Blocking (a database read): call it from a thread."""
    from .. import users
    from .inputs import ensure_db

    data = read_user_session(cookie_value(cookie_header), session_secret())
    if data is None:
        return None
    ensure_db()
    return users.open_session(data["uid"], data["sid"], data["k"], data["epoch"])


# ───────────────────────── known-device cookie ─────────────────────────
# A browser that signed in to an account once keeps a signed id for it. The sign-in limiter counts that browser on its own, so a
# stranger who shares its network address (everyone arrives through the same proxy) cannot use up its tries. The cookie is not a
# credential: it opens nothing, and a thief who has it still needs the password.

DEVICE_COOKIE = "rectifi_device"
DEVICE_SECONDS = 180 * 24 * 3600


def make_device_cookie(secret: str, uid: int, device_id: str | None = None) -> tuple[str, str]:
    """(cookie value, device id). Pass the id of an existing valid cookie to keep it; otherwise a fresh random one is made."""
    device_id = device_id or secrets.token_hex(8)
    body = _b64e(json.dumps({"uid": int(uid), "d": device_id}, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(secret, 'device.' + body)}", device_id


def read_device_cookie(value: str | None, secret: str) -> tuple[int, str] | None:
    """(uid, device id) of a cookie whose signature is right, else None."""
    if not value or not secret or value.count(".") != 1 or len(value) > 300:
        return None
    body, signature = value.split(".")
    try:
        if not hmac.compare_digest(signature.encode("ascii"), _sign(secret, "device." + body).encode("ascii")):
            return None
        data = json.loads(_b64d(body))
    except (ValueError, UnicodeError, binascii.Error):
        return None
    if not isinstance(data, dict) or type(data.get("uid")) is not int or not isinstance(data.get("d"), str) or not 4 <= len(data["d"]) <= 64:
        return None
    return data["uid"], data["d"]


# ───────────────────────── who is the client ─────────────────────────

def _as_ip(text: str):
    import ipaddress

    try:
        ip = ipaddress.ip_address(text.strip().strip("[]"))
    except ValueError:
        return None
    return ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip


def _limiter_key(ip) -> str:
    """The text the limiter counts. An IPv6 client is counted by its /64 (one person usually has a whole /64 to hand out)."""
    import ipaddress

    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    return str(ip)


def client_address(peer: str | None, forwarded_for: str | None, trusted: tuple) -> str:
    """The address the sign-in limiter should count for one request.

    `peer` is the socket's address. A forwarded header (X-Forwarded-For) is believed ONLY when the peer is one of the `trusted`
    proxies (RECTIFI_TRUSTED_PROXIES; empty by default, so the header is ignored). Even then the list is read from the right:
    each hop that is itself a trusted proxy is skipped, and the first address that is not one is the client. Anything a client
    wrote at the left of the list is never reached. A header that is not an address is ignored (the peer is used)."""
    peer_ip = _as_ip(peer or "")
    if peer_ip is None:
        return peer or ""
    if not trusted or not any(peer_ip in net for net in trusted if net.version == peer_ip.version):
        return _limiter_key(peer_ip)
    for hop in reversed((forwarded_for or "").split(",")):
        ip = _as_ip(hop)
        if ip is None:
            break
        if not any(ip in net for net in trusted if net.version == ip.version):
            return _limiter_key(ip)
    return _limiter_key(peer_ip)


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
                    "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
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
