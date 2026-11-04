"""People, invites, sign-up, sign-in, sessions, password change and recovery (multi-user plan D1, D2, D7, D9 and section 4).

This is the only module (with db.py, tools.authenticate_device and alerts.tick) that opens `db.connect_system`, because
`users`, `invites` and `user_sessions` are reached only that way. Standard library plus `vault` (cryptography); it never
imports rectifi.api or langchain. The web layer (api/auth.py, api/routes/auth.py, api/security.py) calls into here.

How the keys fit together (vault.py has the primitives):

  password --scrypt(N=2^16)--> verifier (stored as sha256) + key-encrypting key
  data key (32 random bytes) --wrapped by the password's key--> users.dek_by_password
                              --wrapped by the recovery code's key--> users.dek_by_recovery
  X25519 private key          --wrapped by the data key--> users.private_key_wrapped
  session: a random `k` goes in the cookie; user_sessions.dek_wrapped holds the data key wrapped under `k`. A database
  dump alone cannot open a session, a restart signs nobody out, and logout deletes the row.

There is no reset path (D2): only the password or the recovery code can unwrap the data key. No function here, and no
command in scripts/, sets a new password without one of them.

Nothing here logs or returns a password, a recovery code, a token, a key or a cookie, apart from the one return value
that is meant to show it once (`signup` returns the recovery code and the sign-up token).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field

import psycopg

from . import db, userctx, vault

log = logging.getLogger("rectifi.users")

OWNER_UID = userctx.OWNER_UID

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
INVITE_DAYS = 7

PENDING_SECONDS = 30 * 60               # an unconfirmed sign-up is thrown away after this
SESSION_SECONDS = 7 * 24 * 3600
TOUCH_SECONDS = 5 * 60                  # last_seen_at is written at most this often

# Sign-in limiter (tunables). Design (docs/handoffs/fix-round1.md): a stranger who knows a username must not be able to keep that
# person out. So there is NO lock on the account. What exists instead:
#   * per (account, client) pair: a pause after DELAY_AFTER misses, and a block of that pair only after LOCK_AFTER misses.
#     "Client" is the browser's known-device id (a signed cookie from an earlier good sign-in) or else its network address.
#   * per account, across all clients: a response delay that grows with the misses (never a refusal, and a known device skips it).
#   * recovery counts in its own bucket, so password misses never block it (and the other way round).
#   * globally: caps on password hashing in flight, in three lanes (round 3, N7): a known device with a valid signed cookie for that
#     account (AUTH_DEVICE_INFLIGHT), a proven recovery (AUTH_RECOVERY_INFLIGHT) and everyone else (AUTH_INFLIGHT). The rest get 429 at
#     once. A stranger's junk can only fill the "other" lane. The three caps together bound the threadpool threads busy hashing.
#     There is no budget of misses that refuses new clients: a stranger's junk must never keep real people out (round 2, N2).
#   * every good sign-in, sign-up confirm and recovery sets the signed device cookie (api/routes/auth.py), so a person who is
#     behind the same proxy address as a stranger is a known device and is never refused or delayed for the stranger's misses.
#   * `client` is the real client address only when RECTIFI_TRUSTED_PROXIES vouches for the proxy (api/auth.client_address).
#     Residual: a brand-new browser, behind a shared address, while a stranger is failing from that same address, can be blocked
#     for 15 minutes. The recovery code always works there (its own bucket) and gives that browser its device cookie.
DELAY_AFTER = 5                         # misses within DELAY_WINDOW by one client that start a pause
DELAY_WINDOW = 60
DELAY_SECONDS = 2.0
LOCK_AFTER = 10                         # misses within LOCK_WINDOW that block that one (account, client) pair
LOCK_WINDOW = 15 * 60
LOCK_SECONDS = 15 * 60
ACCOUNT_FREE_MISSES = 10                # misses on one account (all clients, LOCK_WINDOW) before the account-wide delay starts
ACCOUNT_MAX_DELAY = 30.0                # that delay doubles per miss up to this many seconds
AUTH_INFLIGHT = 4                       # hashes in flight in the "other" lane (strangers, unknown names, sign-up); more get 429 at once
AUTH_DEVICE_INFLIGHT = 2                # the known-device lane: needs a valid signed device cookie for that account (checked before hashing)
AUTH_RECOVERY_INFLIGHT = 1              # the recovery lane: used only after the recovery code was proven
SIGNUP_LIMIT = 10                       # sign-up attempts, from anyone, per SIGNUP_WINDOW (global: every request is from 127.0.0.1)
SIGNUP_WINDOW = 3600
THROTTLE_LIMIT = 10_000                 # (account, client) pairs remembered by the limiter, so memory stays bounded

MESSAGES = {
    "invalid_invite": "That invite code isn't valid.",
    "invalid_login": "That username or password isn't right.",
    "invalid_password": "That password isn't right.",
    "invalid_recovery": "That username or recovery code isn't right.",
    "login_locked": "Too many tries. Wait 15 minutes.",
    "login_busy": "Rectifi is busy signing people in. Wait a moment and try again.",
    "signup_limited": "Too many sign-up tries. Wait an hour.",
    "username_taken": "That username is taken.",
    "confirm_failed": "We couldn't finish setting up your account. Nothing was changed. Try again in a moment.",
    "signup_expired": "This sign-up has expired or isn't valid. Start again with your invite code.",
}

_DUMMY_SALT = b"rectifi-dummy-salt-for-unknown-users"[:16]


class AuthError(Exception):
    """A refusal with a status, a stable code and one plain sentence that is safe to show."""

    def __init__(self, status: int, code: str, message: str, field: str | None = None, retry_after: int = 0) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.field, self.retry_after = status, code, message, field, retry_after


@dataclass(frozen=True)
class SignedIn:
    """Who just signed in, and the pieces of their session. `k` is the secret that goes in the cookie."""

    uid: int
    username: str
    is_owner: bool
    sid: str
    k: bytes = field(repr=False)             # the cookie secret: never in a repr, a log line or a traceback
    epoch: int
    ctx: userctx.UserContext


class Attempt:
    """What one sign-in try came to: ok / invalid / locked, how long to wait before answering, and the retry seconds."""

    def __init__(self, status: str, signed_in: SignedIn | None = None, delay: float = 0.0, retry_after: int = 0) -> None:
        self.status, self.signed_in, self.delay, self.retry_after = status, signed_in, delay, retry_after


def _now() -> float:
    return time.time()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _label(sid: str) -> str:
    return f"session|{sid}"


# ───────────────────────── invites ─────────────────────────

def _normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def _code_hash(code: str) -> str:
    return hashlib.sha256(("rectifi|invite|" + _normalize_code(code)).encode("ascii", errors="ignore")).hexdigest()


def new_invite_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"            # no 0/O/1/I: easy to read out loud
    chars = "".join(alphabet[secrets.randbelow(len(alphabet))] for _ in range(20))
    return "-".join(chars[i:i + 4] for i in range(0, 20, 4))


def create_invite(kind: str = "friend", days: int = INVITE_DAYS) -> dict:
    """Make a one-time sign-up code. The code is returned here once; only its hash is stored."""
    if kind not in ("friend", "owner"):
        raise ValueError("kind must be 'friend' or 'owner'")
    if not 1 <= int(days) <= 90:
        raise ValueError("days must be between 1 and 90")
    code = new_invite_code()
    expires = _now() + int(days) * 86400
    with db.connect_system("users: make an invite") as conn:
        if kind == "owner":
            claimed = conn.execute("SELECT verifier IS NOT NULL AS claimed FROM users WHERE id = %s", (OWNER_UID,)).fetchone()
            if claimed is not None and claimed["claimed"]:
                raise AuthError(409, "owner_claimed", "The owner account already has a password. An owner invite is not needed.")
        row = conn.execute("INSERT INTO invites (code_hash, kind, expires_at) VALUES (%s, %s, to_timestamp(%s)) RETURNING id",
                           (_code_hash(code), kind, expires)).fetchone()
    return {"id": row["id"], "code": code, "kind": kind, "expires_at": expires}


def list_invites() -> list[dict]:
    """Every invite with its state. Never the code (only a hash exists)."""
    now = _now()
    with db.connect_system("users: list invites") as conn:
        rows = conn.execute(
            "SELECT i.id, i.kind, extract(epoch FROM i.expires_at) AS expires_at, i.used_at IS NOT NULL AS used, "
            "i.reserved_by, u.username AS reserved_name FROM invites i LEFT JOIN users u ON u.id = i.reserved_by ORDER BY i.id").fetchall()
    out = []
    for r in rows:
        state = "used" if r["used"] else "reserved" if r["reserved_by"] is not None else "expired" if float(r["expires_at"]) <= now else "open"
        out.append({"id": r["id"], "kind": r["kind"], "state": state, "expires_at": float(r["expires_at"]), "reserved_by": r["reserved_name"]})
    return out


def revoke_invite(invite_id: int) -> bool:
    """Cancel an invite that has not been used. A person who already reserved it but has not confirmed can't finish."""
    with db.connect_system("users: revoke an invite") as conn:
        row = conn.execute("DELETE FROM invites WHERE id = %s AND used_at IS NULL RETURNING id", (int(invite_id),)).fetchone()
    return row is not None


# ───────────────────────── limiters ─────────────────────────

class _Window:
    """A bounded, thread-safe list of recent event times."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._times: list[float] = []

    def reset(self) -> None:
        with self._lock:
            self._times = []

    def hit(self, limit: int, window: int, now: float) -> bool:
        """Record one event. False (and not recorded) when `limit` events already happened in the last `window` seconds."""
        with self._lock:
            self._times = [t for t in self._times if now - t < window]
            if len(self._times) >= limit:
                return False
            self._times.append(now)
            return True


    def count(self, window: int, now: float) -> int:
        with self._lock:
            self._times = [t for t in self._times if now - t < window]
            return len(self._times)

    def add(self, now: float, keep: int = 100_000) -> None:
        with self._lock:
            self._times.append(now)
            del self._times[:-keep]


signup_window = _Window()


class _Throttle:
    """Misses per (account name, client) pair and per account name, in memory and bounded. Unknown names are counted like real ones,
    so a stranger learns nothing from the limiter. Nothing here ever locks an account: only one pair can be blocked."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pairs: dict[tuple, tuple[list[float], float]] = {}      # pair -> (miss times, blocked until)
        self._names: dict[str, list[float]] = {}                      # name -> miss times, all clients

    def reset(self) -> None:
        with self._lock:
            self._pairs, self._names = {}, {}

    def blocked_for(self, pair: tuple, now: float) -> int:
        with self._lock:
            until = self._pairs.get(pair, ([], 0.0))[1]
            return max(1, int(until - now + 0.999)) if until > now else 0

    def delay_for(self, pair: tuple, name: str, now: float, *, known_device: bool = False) -> float:
        """How long to wait before answering: the pair's pause, or the account-wide delay (skipped for a known device)."""
        with self._lock:
            times = self._pairs.get(pair, ([], 0.0))[0]
            pause = DELAY_SECONDS if sum(1 for t in times if now - t < DELAY_WINDOW) >= DELAY_AFTER else 0.0
            extra = 0.0
            if not known_device:
                n = sum(1 for t in self._names.get(name, []) if now - t < LOCK_WINDOW)
                if n > ACCOUNT_FREE_MISSES:
                    extra = min(ACCOUNT_MAX_DELAY, float(2 ** min(n - ACCOUNT_FREE_MISSES, 10)))
            return max(pause, extra)

    def fail(self, pair: tuple, name: str, now: float) -> bool:
        """Record a miss. True when it blocked this pair."""
        with self._lock:
            for table in (self._pairs, self._names):
                if len(table) >= THROTTLE_LIMIT and (pair if table is self._pairs else name) not in table:
                    table.pop(next(iter(table)))
            self._names[name] = [t for t in self._names.get(name, []) if now - t < LOCK_WINDOW] + [now]
            times, until = self._pairs.get(pair, ([], 0.0))
            times = [t for t in times if now - t < LOCK_WINDOW] + [now]
            if len(times) >= LOCK_AFTER:
                self._pairs[pair] = ([], now + LOCK_SECONDS)
                return True
            self._pairs[pair] = (times, until)
            return False

    def clear(self, pair: tuple) -> None:
        with self._lock:
            self._pairs.pop(pair, None)


login_throttle = _Throttle()
recover_throttle = _Throttle()          # its own bucket: password misses never block recovery, recovery misses never block sign-in


def reset_limiters() -> None:
    """Forget in-memory counters (tests; a restart does the same). Account counters live in the database."""
    signup_window.reset()
    login_throttle.reset()
    recover_throttle.reset()


def _ghost_name(username: str) -> str:
    return (username or "").strip().lower()[:64]


def _locked_seconds(row, now: float) -> int:
    until = float(row["locked_epoch"]) if row["locked_epoch"] is not None else 0.0
    return max(1, int(until - now + 0.999)) if until > now else 0


def _register_failure(uid: int, now: float) -> bool:
    """Add one failure to this account; lock it at LOCK_AFTER. True when it locked. Only this account changes."""
    with db.connect_system("users: count a failed try") as conn:
        row = conn.execute("SELECT failures FROM users WHERE id = %s FOR UPDATE", (uid,)).fetchone()
        if row is None:
            return False
        failures = [float(t) for t in (row["failures"] or []) if isinstance(t, (int, float)) and now - float(t) < LOCK_WINDOW] + [now]
        if len(failures) >= LOCK_AFTER:
            conn.execute("UPDATE users SET failures = '[]'::jsonb, locked_until = to_timestamp(%s) WHERE id = %s", (now + LOCK_SECONDS, uid))
            return True
        conn.execute("UPDATE users SET failures = %s::jsonb WHERE id = %s", (json.dumps(failures), uid))
        return False


def _clear_failures(uid: int) -> None:
    with db.connect_system("users: clear failed tries") as conn:
        conn.execute("UPDATE users SET failures = '[]'::jsonb, locked_until = NULL WHERE id = %s", (uid,))


# ───────────────────────── looking people up ─────────────────────────

_USER_COLUMNS = ("id, username, scrypt_salt, verifier, dek_by_password, dek_by_recovery, recovery_salt, public_key, private_key_wrapped, "
                 "ingest_salt, session_epoch, encrypted, confirmed_at IS NOT NULL AS confirmed, failures, "
                 "extract(epoch FROM locked_until) AS locked_epoch")


def _user_by_name(conn, username: str):
    return conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE lower(username) = lower(%s)", (username,)).fetchone()


def _user_by_id(conn, uid: int):
    return conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s", (uid,)).fetchone()


def _can_sign_in(row) -> bool:
    return row is not None and row["confirmed"] and row["verifier"] is not None and row["dek_by_password"] is not None


_LANES = {"device": AUTH_DEVICE_INFLIGHT, "recovery": AUTH_RECOVERY_INFLIGHT, "other": AUTH_INFLIGHT}
_AUTH_SLOTS = threading.BoundedSemaphore(AUTH_INFLIGHT)                       # the "other" lane (kept under this name for the tests)
_LANE_SLOTS = {"device": threading.BoundedSemaphore(AUTH_DEVICE_INFLIGHT), "recovery": threading.BoundedSemaphore(AUTH_RECOVERY_INFLIGHT),
               "other": _AUTH_SLOTS}


def _derive(password: str, salt: bytes, lane: str = "other") -> tuple[bytes, bytes]:
    """scrypt, behind a cap on how many hashes run or wait at once, per lane. A flood of sign-ins gets 429 at once instead of
    holding threadpool threads (the session check needs them): at most sum(_LANES) threads are ever busy here. The caller picks
    "device" only for a verified device cookie of that account and "recovery" only after the code was proven."""
    slots = _LANE_SLOTS[lane]
    if not slots.acquire(blocking=False):
        raise AuthError(429, "login_busy", MESSAGES["login_busy"], retry_after=2)
    try:
        return vault.derive_password_keys(password, salt)
    finally:
        slots.release()


def _burn_scrypt(password: str, lane: str = "other") -> None:
    """The same work as checking a real password, so an unknown username takes as long as a wrong password."""
    try:
        _derive(password[:MAX_PASSWORD_LENGTH], _DUMMY_SALT, lane)
    except AuthError:
        raise                                           # busy: the caller answers 429
    except Exception:                                   # noqa: BLE001  (timing only)
        pass


def _check_password(row, password: str, lane: str = "other") -> bytes | None:
    """The data key if `password` is right for this user, else None. One scrypt either way."""
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH or not password:
        _burn_scrypt("x", lane)
        return None
    verifier, kek = _derive(password, bytes(row["scrypt_salt"]), lane)
    if not vault.verifier_matches(verifier, bytes(row["verifier"])):
        return None
    try:
        return vault.unwrap_key(kek, bytes(row["dek_by_password"]), "dek|password")
    except vault.VaultError:
        return None


def password_problem(password) -> str | None:
    if not isinstance(password, str):
        return "Type a password."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters. A few random words in a row works well."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    return None


def username_problem(username) -> str | None:
    if not isinstance(username, str) or not USERNAME_RE.match(username.strip()):
        return "Use 3 to 32 letters, digits, dots, dashes or underscores, starting with a letter or digit."
    return None


# ───────────────────────── clearing unconfirmed sign-ups ─────────────────────────

RESERVED_NAMES = ("owner",)             # row 1's placeholder name: no friend may take it, so resetting row 1 can never clash


def _reset_owner_claim(conn) -> None:
    """Row 1 back to 'no password yet' after an owner claim nobody confirmed. The name goes back to 'owner'; if somebody already
    holds that name (a database from before the name was reserved) row 1 gets a unique placeholder instead of an error."""
    sets = ("scrypt_salt = NULL, verifier = NULL, dek_by_password = NULL, dek_by_recovery = NULL, recovery_salt = NULL, "
            "public_key = NULL, private_key_wrapped = NULL, ingest_salt = NULL, confirmed_at = NULL, sweep_state = NULL, blind_fp = NULL")
    for name in ("owner", f"owner-{secrets.token_hex(4)}"):
        try:
            with conn.transaction():                          # a savepoint: a clash undoes only this try
                conn.execute(f"UPDATE users SET username = %s, {sets} WHERE id = %s AND confirmed_at IS NULL", (name, OWNER_UID))
            return
        except psycopg.errors.UniqueViolation:
            continue


def purge_unconfirmed(now: float | None = None) -> int:
    """Drop sign-ups nobody confirmed in time (and expired sessions). The username and the invite become free again.
    The owner's row is never deleted: an unconfirmed owner claim is rolled back to 'no password yet'.
    One bad row never stops this: it runs at the start of sign-up, confirm and sign-in, so a raise here would lock everybody out."""
    now = _now() if now is None else now
    removed = 0
    with db.connect_system("users: clear unconfirmed sign-ups") as conn:
        stale = conn.execute("SELECT id, user_id FROM user_sessions WHERE pending AND expires_at <= to_timestamp(%s)", (now,)).fetchall()
        for s in stale:
            try:
                with conn.transaction():
                    if s["user_id"] == OWNER_UID:
                        _reset_owner_claim(conn)
                        conn.execute("UPDATE invites SET reserved_by = NULL WHERE reserved_by = %s AND used_at IS NULL", (OWNER_UID,))
                    else:
                        conn.execute("DELETE FROM users WHERE id = %s AND confirmed_at IS NULL", (s["user_id"],))
                removed += 1
            except psycopg.Error as exc:
                log.error("could not clear one unconfirmed sign-up (%s); skipped", type(exc).__name__)
        conn.execute("DELETE FROM user_sessions WHERE expires_at <= to_timestamp(%s)", (now,))
        removed += conn.execute(
            "DELETE FROM users u WHERE u.confirmed_at IS NULL AND u.id <> %s AND u.created_at < to_timestamp(%s) "
            "AND NOT EXISTS (SELECT 1 FROM user_sessions s WHERE s.user_id = u.id AND s.pending)",
            (OWNER_UID, now - PENDING_SECONDS)).rowcount
    return removed


# ───────────────────────── sign-up (two steps) ─────────────────────────

def _check_invite(conn, code: str, now: float):
    row = conn.execute("SELECT id, kind FROM invites WHERE code_hash = %s AND reserved_by IS NULL AND used_at IS NULL "
                       "AND expires_at > to_timestamp(%s)", (_code_hash(code), now)).fetchone()
    if row is None:
        raise AuthError(400, "invalid_invite", MESSAGES["invalid_invite"], "invite")
    return row


def signup(invite_code: str, username: str, password: str) -> tuple[str, str]:
    """Step one. Reserves the invite, makes the user (unconfirmed) and a pending session. Returns (signup_token, recovery_code).
    No cookie, no session and no way to sign in until `confirm_signup`."""
    now = _now()
    if not isinstance(invite_code, str) or not _normalize_code(invite_code):
        raise AuthError(422, "invalid", "Type your invite code.", "invite")
    problem = username_problem(username)
    if problem:
        raise AuthError(422, "invalid", problem, "username")
    problem = password_problem(password)
    if problem:
        raise AuthError(422, "invalid", problem, "password")
    username = username.strip()
    if not signup_window.hit(SIGNUP_LIMIT, SIGNUP_WINDOW, now):
        raise AuthError(429, "signup_limited", MESSAGES["signup_limited"], retry_after=SIGNUP_WINDOW)
    purge_unconfirmed(now)
    with db.connect_system("users: check an invite") as conn:
        _check_invite(conn, invite_code, now)                    # cheap check first, so a wrong code costs no scrypt

    salt = secrets.token_bytes(16)
    verifier, kek = _derive(password, salt)
    dek = vault.new_data_key()
    public, private = vault.new_keypair()
    code = vault.new_recovery_code()
    recovery_salt = secrets.token_bytes(16)
    keys = {
        "scrypt_salt": salt, "verifier": vault.verifier_hash(verifier),
        "dek_by_password": vault.wrap_key(kek, dek, "dek|password"),
        "dek_by_recovery": vault.wrap_key(vault.recovery_key(code, recovery_salt), dek, "dek|recovery"),
        "recovery_salt": recovery_salt, "public_key": public,
        "private_key_wrapped": vault.wrap_key(dek, private, "privkey"), "ingest_salt": vault.new_ingest_salt(),
    }
    sid, k = str(uuid.uuid4()), secrets.token_bytes(32)
    try:
        with db.connect_system("users: sign up") as conn:
            invite = _check_invite(conn, invite_code, now)
            if invite["kind"] == "owner":
                uid = OWNER_UID
                reserved = conn.execute("UPDATE invites SET reserved_by = %s WHERE id = %s AND reserved_by IS NULL AND used_at IS NULL "
                                        "RETURNING id", (uid, invite["id"])).fetchone()
                claimed = conn.execute(
                    "UPDATE users SET username = %s, scrypt_salt = %s, verifier = %s, dek_by_password = %s, dek_by_recovery = %s, "
                    "recovery_salt = %s, public_key = %s, private_key_wrapped = %s, ingest_salt = %s, failures = '[]'::jsonb, "
                    "locked_until = NULL, session_epoch = session_epoch + 1 WHERE id = %s AND verifier IS NULL RETURNING id",
                    (username, keys["scrypt_salt"], keys["verifier"], keys["dek_by_password"], keys["dek_by_recovery"],
                     keys["recovery_salt"], keys["public_key"], keys["private_key_wrapped"], keys["ingest_salt"], uid)).fetchone()
                if reserved is None or claimed is None:
                    raise AuthError(400, "invalid_invite", MESSAGES["invalid_invite"], "invite")
            else:
                if username.lower() in RESERVED_NAMES:
                    raise AuthError(409, "username_taken", MESSAGES["username_taken"], "username")
                row = conn.execute(
                    "INSERT INTO users (username, scrypt_salt, verifier, dek_by_password, dek_by_recovery, recovery_salt, public_key, "
                    "private_key_wrapped, ingest_salt, encrypted, blind_fp) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s) RETURNING id",
                    (username, keys["scrypt_salt"], keys["verifier"], keys["dek_by_password"], keys["dek_by_recovery"],
                     keys["recovery_salt"], keys["public_key"], keys["private_key_wrapped"], keys["ingest_salt"],
                     vault.blind_key_fingerprint())).fetchone()
                uid = row["id"]
                reserved = conn.execute("UPDATE invites SET reserved_by = %s WHERE id = %s AND reserved_by IS NULL AND used_at IS NULL "
                                        "RETURNING id", (uid, invite["id"])).fetchone()
                if reserved is None:
                    raise AuthError(400, "invalid_invite", MESSAGES["invalid_invite"], "invite")
            conn.execute("INSERT INTO user_sessions (id, user_id, dek_wrapped, pending, expires_at) VALUES (%s, %s, %s, true, to_timestamp(%s))",
                         (sid, uid, vault.wrap_key(k, dek, _label(sid)), now + PENDING_SECONDS))
    except psycopg.errors.UniqueViolation:
        raise AuthError(409, "username_taken", MESSAGES["username_taken"], "username") from None
    return f"{sid}.{_b64(k)}", code


def _parse_token(token) -> tuple[str, bytes] | None:
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 200:
        return None
    sid, _, k_text = token.partition(".")
    try:
        sid = str(uuid.UUID(sid))
        k = _unb64(k_text)
    except (ValueError, UnicodeError):
        return None
    return (sid, k) if len(k) == 32 else None


def _context_from(row, dek: bytes) -> userctx.UserContext:
    private = None
    if row["private_key_wrapped"] is not None:
        try:
            private = vault.unwrap_key(dek, bytes(row["private_key_wrapped"]), "privkey")
        except vault.VaultError:
            private = None
    return userctx.UserContext(uid=row["id"], key=dek, public_key=bytes(row["public_key"]) if row["public_key"] is not None else None,
                               ingest_salt=bytes(row["ingest_salt"]) if row["ingest_salt"] is not None else None, strict=True,
                               private_key=private)


def _open_session_row(conn, uid: int, dek: bytes, now: float) -> SignedIn:
    """Make a fresh (non-pending) session for a person whose data key is `dek`."""
    row = _user_by_id(conn, uid)
    sid, k = str(uuid.uuid4()), secrets.token_bytes(32)
    conn.execute("INSERT INTO user_sessions (id, user_id, dek_wrapped, pending, expires_at, last_seen_at) "
                 "VALUES (%s, %s, %s, false, to_timestamp(%s), to_timestamp(%s))",
                 (sid, uid, vault.wrap_key(k, dek, _label(sid)), now + SESSION_SECONDS, now))
    return SignedIn(uid=uid, username=row["username"], is_owner=uid == OWNER_UID, sid=sid, k=k, epoch=row["session_epoch"],
                    ctx=_context_from(row, dek))


def _rekey_owner_dedupe(ctx: userctx.UserContext, conn) -> None:
    """The owner's old rows hold plain dedupe keys; every new write holds keyed ones. Make them the same kind NOW, before the
    claim is confirmed and before the first cookie exists, so a statement or SMS that arrives after the claim finds the payments
    that are already here. (Until `confirmed_at` is set the phone gateway gets no ingest salt, so nothing new is written meanwhile.)
    It runs on the confirm's own connection, so it is ONE transaction with `confirmed_at`: if this raises, the confirm is rolled
    back with it and the person is told to try again. There is no half-done state, and no 'log it and carry on'."""
    from . import seal_sweep                                   # inside the function: seal_sweep imports account, which imports this module

    seal_sweep.rekey_dedupe(ctx, conn=conn)


def confirm_signup(token: str) -> SignedIn:
    """Step two. Marks the person confirmed, uses the invite, gives them their own copy of the defaults and a real session."""
    now = _now()
    expired = AuthError(400, "signup_expired", MESSAGES["signup_expired"])
    parsed = _parse_token(token)
    if parsed is None:
        raise expired
    sid, k = parsed
    purge_unconfirmed(now)
    with db.connect_system("users: confirm a sign-up") as conn:
        pending = conn.execute("SELECT user_id FROM user_sessions WHERE id = %s AND pending AND expires_at > to_timestamp(%s)",
                               (sid, now)).fetchone()
        if pending is None:
            raise expired
        uid = pending["user_id"]
        # Lock order, here and in purge_unconfirmed: the person's users row FIRST, then the invite. Whoever holds the row finishes
        # before the other looks. A purge that got there first has deleted the pending session, so we find it gone and stop with
        # nothing touched; a confirm that got there first has set confirmed_at, so the purge finds nothing left to reset.
        locked = conn.execute("SELECT 1 FROM users WHERE id = %s AND confirmed_at IS NULL FOR UPDATE", (uid,)).fetchone()
        pending = conn.execute("SELECT dek_wrapped FROM user_sessions WHERE id = %s AND pending AND expires_at > to_timestamp(%s) FOR UPDATE",
                               (sid, now)).fetchone() if locked is not None else None
        if pending is None:
            raise expired
        try:
            dek = vault.unwrap_key(k, bytes(pending["dek_wrapped"]), _label(sid))
        except vault.VaultError:
            raise expired from None
        invite = conn.execute("SELECT id FROM invites WHERE reserved_by = %s AND used_at IS NULL AND expires_at > to_timestamp(%s) FOR UPDATE",
                              (uid, now)).fetchone()
        if invite is None:                                    # revoked or expired while waiting
            conn.execute("DELETE FROM user_sessions WHERE id = %s", (sid,))
            raise expired
        if uid == OWNER_UID:                                  # same transaction as confirmed_at: both happen or neither does
            try:
                _rekey_owner_dedupe(_context_from(_user_by_id(conn, uid), dek), conn)
            except Exception as exc:                          # noqa: BLE001
                log.error("owner claim: re-keying the dedupe keys failed (%s); the claim was not confirmed", type(exc).__name__)
                raise AuthError(500, "confirm_failed", MESSAGES["confirm_failed"]) from None
        conn.execute("UPDATE users SET confirmed_at = to_timestamp(%s), sweep_state = CASE WHEN id = %s THEN 'pending' ELSE sweep_state END, "
                     "blind_fp = %s WHERE id = %s", (now, OWNER_UID, vault.blind_key_fingerprint(), uid))
        conn.execute("UPDATE invites SET used_at = to_timestamp(%s) WHERE id = %s", (now, invite["id"]))
        conn.execute("DELETE FROM user_sessions WHERE id = %s", (sid,))        # the sign-up token is spent; the cookie gets a new secret
        signed_in = _open_session_row(conn, uid, dek, now)
    db.seed_user(uid)
    return signed_in


# ───────────────────────── sign-in, sessions ─────────────────────────

def _client_pair(name: str, row, client: str, device: tuple[int, str] | None) -> tuple[tuple, bool]:
    """((name, client key), known_device). A browser that proves an earlier good sign-in to THIS account (a signed device id
    the web layer checked) is counted on its own, apart from everyone who shares its network address."""
    if device is not None and row is not None and device[0] == row["id"]:
        return (name, "dev:" + device[1]), True
    return (name, "ip:" + (client or "")), False


def login(username: str, password: str, client: str = "", device: tuple[int, str] | None = None) -> Attempt:
    """Check a username and password. Unknown names, unconfirmed people and wrong passwords all cost one scrypt and get
    the same message. Misses are counted per (account, client), never per account alone: a stranger who guesses a name
    blocks only his own pair, and slows (never refuses) the account's other new clients. See the limiter notes at the top.
    `client` is the network address; `device` is (uid, id) from a verified known-device cookie, or None."""
    now = _now()
    purge_unconfirmed(now)
    name = _ghost_name(username)
    row = None
    if isinstance(username, str) and username.strip():
        with db.connect_system("users: find a person to sign in") as conn:
            row = _user_by_name(conn, username.strip())
    pair, known = _client_pair(name, row, client, device)
    blocked = login_throttle.blocked_for(pair, now)
    if blocked:
        return Attempt("locked", retry_after=blocked)
    delay = login_throttle.delay_for(pair, name, now, known_device=known)

    def miss() -> Attempt:
        if login_throttle.fail(pair, name, now):
            return Attempt("locked", retry_after=LOCK_SECONDS)
        return Attempt("invalid", delay=delay)

    lane = "device" if known else "other"                  # `known` is true only for a verified cookie of THIS account
    if not _can_sign_in(row):
        _burn_scrypt(password if isinstance(password, str) else "", lane)
        return miss()
    dek = _check_password(row, password, lane)
    if dek is None:
        return miss()
    login_throttle.clear(pair)
    with db.connect_system("users: open a session") as conn:
        signed_in = _open_session_row(conn, row["id"], dek, now)
    return Attempt("ok", signed_in=signed_in, delay=delay)


def open_session(uid, sid, k_text, epoch) -> SignedIn | None:
    """The person behind a cookie's (uid, sid, k, epoch), or None. Fails closed on anything off: a deleted or expired
    session, a pending one, another person's id, a stale epoch, a secret that does not open the wrapped key."""
    if type(uid) is not int or type(epoch) is not int or not isinstance(sid, str) or not isinstance(k_text, str):
        return None
    try:
        sid = str(uuid.UUID(sid))
        k = _unb64(k_text)
    except (ValueError, UnicodeError):
        return None
    if len(k) != 32:
        return None
    now = _now()
    with db.connect_system("users: check a session") as conn:
        found = conn.execute(
            "SELECT s.dek_wrapped, extract(epoch FROM s.last_seen_at) AS seen FROM user_sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.id = %s AND s.user_id = %s AND NOT s.pending AND s.expires_at > to_timestamp(%s) "
            "AND u.confirmed_at IS NOT NULL AND u.session_epoch = %s", (sid, uid, now, epoch)).fetchone()
        if found is None:
            return None
        try:
            dek = vault.unwrap_key(k, bytes(found["dek_wrapped"]), _label(sid))
        except vault.VaultError:
            return None
        row = _user_by_id(conn, uid)
        if found["seen"] is None or now - float(found["seen"]) > TOUCH_SECONDS:
            conn.execute("UPDATE user_sessions SET last_seen_at = to_timestamp(%s) WHERE id = %s", (now, sid))
    return SignedIn(uid=uid, username=row["username"], is_owner=uid == OWNER_UID, sid=sid, k=k, epoch=row["session_epoch"], ctx=_context_from(row, dek))


def end_session(uid: int, sid: str) -> None:
    """Logout: delete this session row. The cookie is useless from then on."""
    try:
        sid = str(uuid.UUID(sid))
    except (ValueError, TypeError):
        return
    with db.connect_system("users: sign out") as conn:
        conn.execute("DELETE FROM user_sessions WHERE id = %s AND user_id = %s", (sid, int(uid)))


def end_all_sessions(uid: int) -> int:
    """Sign this person out everywhere: new epoch and every session row gone. Returns the new epoch."""
    with db.connect_system("users: sign out everywhere") as conn:
        conn.execute("DELETE FROM user_sessions WHERE user_id = %s", (int(uid),))
        row = conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id = %s RETURNING session_epoch", (int(uid),)).fetchone()
    return row["session_epoch"] if row else 0


def uid_of(username: str) -> int | None:
    """The id of the account with this username (any case), or None. For the web layer, right after a proof of who they are."""
    if not isinstance(username, str) or not username.strip():
        return None
    with db.connect_system("users: find an id by name") as conn:
        row = conn.execute("SELECT id FROM users WHERE lower(username) = lower(%s) AND confirmed_at IS NOT NULL", (username.strip(),)).fetchone()
    return row["id"] if row else None


def profile(uid: int) -> dict | None:
    with db.connect_system("users: who is this") as conn:
        row = conn.execute("SELECT username FROM users WHERE id = %s", (int(uid),)).fetchone()
    return {"username": row["username"], "is_owner": int(uid) == OWNER_UID} if row else None


# ───────────────────────── password, recovery ─────────────────────────

def _signed_in_user(conn, uid: int):
    row = _user_by_id(conn, uid)
    if not _can_sign_in(row):
        raise AuthError(401, "auth_required", "Sign in to use Rectifi.")
    return row


def _verify_current(uid: int, password: str) -> tuple[dict, bytes]:
    """Check the current password for a signed-in person, counting a miss against their account. Returns (row, data key)."""
    now = _now()
    with db.connect_system("users: read a person") as conn:
        row = _signed_in_user(conn, uid)
    locked = _locked_seconds(row, now)
    if locked:
        raise AuthError(429, "login_locked", MESSAGES["login_locked"], retry_after=locked)
    dek = _check_password(row, password)
    if dek is None:
        if _register_failure(uid, now):
            raise AuthError(429, "login_locked", MESSAGES["login_locked"], retry_after=LOCK_SECONDS)
        raise AuthError(401, "invalid_password", MESSAGES["invalid_password"], "current")
    if row["failures"] or row["locked_epoch"] is not None:
        _clear_failures(uid)
    return row, dek


def change_password(uid: int, keep_sid: str, current: str, new: str) -> int:
    """Set a new password. The data key does not change, so everything stays readable. Every other session is signed out.
    Returns the new epoch (the caller re-issues this session's cookie with it)."""
    problem = password_problem(new)
    if problem:
        raise AuthError(422, "invalid", problem, "new")
    row, dek = _verify_current(uid, current)
    salt = secrets.token_bytes(16)
    verifier, kek = _derive(new, salt)
    with db.connect_system("users: change a password") as conn:
        updated = conn.execute("UPDATE users SET scrypt_salt = %s, verifier = %s, dek_by_password = %s, session_epoch = session_epoch + 1, "
                               "failures = '[]'::jsonb, locked_until = NULL WHERE id = %s RETURNING session_epoch",
                               (salt, vault.verifier_hash(verifier), vault.wrap_key(kek, dek, "dek|password"), uid)).fetchone()
        conn.execute("DELETE FROM user_sessions WHERE user_id = %s AND id <> %s", (uid, keep_sid))
    return updated["session_epoch"]


def new_recovery_code_for(uid: int, password: str) -> str:
    """Make a new recovery code (needs the password). The old code stops working at once."""
    row, dek = _verify_current(uid, password)
    code = vault.new_recovery_code()
    salt = secrets.token_bytes(16)
    with db.connect_system("users: new recovery code") as conn:
        conn.execute("UPDATE users SET dek_by_recovery = %s, recovery_salt = %s WHERE id = %s",
                     (vault.wrap_key(vault.recovery_key(code, salt), dek, "dek|recovery"), salt, uid))
    return code


def recover(username: str, recovery_code: str, new_password: str, client: str = "", device: tuple[int, str] | None = None) -> str:
    """Forgot the password: the recovery code unwraps the data key, a new password wraps it again, a NEW recovery code
    replaces the old one, and every session is signed out. Wrong username and wrong code look the same. This is the only
    way back besides the password: there is no admin reset. Misses are counted in the recovery bucket only (per account and
    client), so nobody's password guesses can block it. The code has 125 bits, so the bucket only has to cap the work."""
    problem = password_problem(new_password)
    if problem:
        raise AuthError(422, "invalid", problem, "new_password")
    now = _now()
    name = _ghost_name(username)
    bad = AuthError(401, "invalid_recovery", MESSAGES["invalid_recovery"])
    row = None
    if isinstance(username, str) and username.strip():
        with db.connect_system("users: find a person to recover") as conn:
            row = _user_by_name(conn, username.strip())
    pair, _known = _client_pair(name, row, client, device)
    blocked = recover_throttle.blocked_for(pair, now)
    if blocked:
        raise AuthError(429, "login_locked", MESSAGES["login_locked"], retry_after=blocked)
    dek = None
    if _can_sign_in(row) and row["dek_by_recovery"] is not None and row["recovery_salt"] is not None             and isinstance(recovery_code, str) and 0 < len(recovery_code) <= 100:
        try:
            dek = vault.unwrap_key(vault.recovery_key(recovery_code, bytes(row["recovery_salt"])), bytes(row["dek_by_recovery"]), "dek|recovery")
        except vault.VaultError:
            dek = None
    if dek is None:
        if recover_throttle.fail(pair, name, now):
            raise AuthError(429, "login_locked", MESSAGES["login_locked"], retry_after=LOCK_SECONDS)
        raise bad
    recover_throttle.clear(pair)
    salt = secrets.token_bytes(16)
    verifier, kek = _derive(new_password, salt, "recovery")          # reached only with a proven recovery code
    code = vault.new_recovery_code()
    recovery_salt = secrets.token_bytes(16)
    with db.connect_system("users: recover an account") as conn:
        conn.execute("UPDATE users SET scrypt_salt = %s, verifier = %s, dek_by_password = %s, dek_by_recovery = %s, recovery_salt = %s, "
                     "session_epoch = session_epoch + 1, failures = '[]'::jsonb, locked_until = NULL WHERE id = %s",
                     (salt, vault.verifier_hash(verifier), vault.wrap_key(kek, dek, "dek|password"),
                      vault.wrap_key(vault.recovery_key(code, recovery_salt), dek, "dek|recovery"), recovery_salt, row["id"]))
        conn.execute("DELETE FROM user_sessions WHERE user_id = %s", (row["id"],))
    return code
