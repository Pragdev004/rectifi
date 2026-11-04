"""Sealing text so the database holds only ciphertext (multi-user plan, section 4). Crypto only: no database, no langchain.

Envelopes (text, safe in a TEXT column):
  rx1.<base64url nonce|ciphertext>        AES-256-GCM under the person's data key. AAD 'rectifi|rx1|<uid>'.
  rs1.<base64url eph|nonce|ciphertext>    for a job that runs while the person is signed out (no data key): the server
                                          knows only their PUBLIC key (X25519 + HKDF-SHA256 + AES-256-GCM). Only the
                                          private key, which the data key wraps, can open it. Opened at next sign-in.
  b1:<hex>                                a keyed hash (HMAC-SHA256), for dedupe keys that must not leak text:
                                          blind() under a key derived from the data key (works only while signed in; the data
                                          key is in no dump, so these hashes cannot be guessed offline),
                                          blind_ingest() under users.ingest_salt AND the server's RECTIFI_BLIND_KEY (also works
                                          signed out; the salt is in the database, the key is not, so a dump alone cannot guess them).

Laptop mode (RECTIFI_MULTIUSER unset): seal() and blind() return their input, so nothing changes for one person.

What this does NOT do: the server sees passwords and text in memory while it works, and the owner can change the code.
The Privacy page says so. Standard primitives from `cryptography`; the way they are put together is ours and has had no
outside review.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import config, userctx

RX = "rx1."
RS = "rs1."
BLIND = "b1:"

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 16, 8, 1
_SCRYPT_SLOTS = threading.BoundedSemaphore(2)       # at most 2 password hashes at once, so a flood cannot eat all memory


class VaultError(Exception):
    """A value could not be sealed or opened (wrong person, tampered, not an envelope)."""


class BlindKeyMissing(VaultError):
    """RECTIFI_BLIND_KEY is not set (multi-user mode). The app refuses to start without it; this is the second line of defence."""


class VaultLocked(VaultError):
    """The data key is not here: the person is signed out. The API answers 401 "sign in again"."""


# ───────────── small helpers ─────────────

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _aad(kind: str, uid: int) -> bytes:
    return f"rectifi|{kind}|{uid}".encode()


def _hkdf(secret: bytes, info: bytes, length: int = 32, salt: bytes | None = None) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


def is_envelope(value: Any) -> bool:
    return isinstance(value, str) and (value.startswith(RX) or value.startswith(RS))


def _ctx(ctx: userctx.UserContext | None) -> userctx.UserContext:
    return ctx if ctx is not None else userctx.current()


def _active() -> bool:
    return config.multiuser()


# ───────────── seal / open ─────────────

def _seal_rx(data: bytes, uid: int, key: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, _aad("rx1", uid))


def _seal_rs(data: bytes, uid: int, public_key: bytes) -> bytes:
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(public_key))
    key = _hkdf(shared, _aad("rs1", uid))
    nonce = os.urandom(12)
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return eph_pub + nonce + AESGCM(key).encrypt(nonce, data, _aad("rs1", uid))


def _seal_raw(data: bytes, ctx: userctx.UserContext) -> tuple[str, bytes]:
    if ctx.key is not None:
        return RX, _seal_rx(data, ctx.uid, ctx.key)
    if ctx.public_key is not None:
        return RS, _seal_rs(data, ctx.uid, ctx.public_key)
    raise VaultLocked("You are signed out and no public key is on file, so this cannot be sealed. Sign in again.")


def seal(text: str | None, ctx: userctx.UserContext | None = None) -> str | None:
    """Text -> envelope. None stays None. Laptop mode returns the text unchanged."""
    if text is None or not _active():
        return text
    prefix, blob = _seal_raw(text.encode("utf-8"), _ctx(ctx))
    return prefix + _b64(blob)


def seal_json(obj: Any, ctx: userctx.UserContext | None = None) -> str | None:
    if obj is None:
        return None
    return seal(json.dumps(obj, separators=(",", ":"), default=str), ctx)


def seal_bytes(data: bytes | None, ctx: userctx.UserContext | None = None) -> bytes | None:
    """Bytes -> bytes (b'rx1.' + nonce + ciphertext), for BYTEA columns. Laptop mode returns the bytes unchanged."""
    if data is None or not _active():
        return data
    prefix, blob = _seal_raw(data, _ctx(ctx))
    return prefix.encode("ascii") + blob


def _open_raw(prefix: str, blob: bytes, ctx: userctx.UserContext) -> bytes:
    try:
        if prefix == RX:
            if ctx.key is None:
                raise VaultLocked("Sign in again to read this.")
            return AESGCM(ctx.key).decrypt(blob[:12], blob[12:], _aad("rx1", ctx.uid))
        if ctx.private_key is None:
            raise VaultLocked("Sign in again to read this.")
        eph_pub, nonce, ct = blob[:32], blob[32:44], blob[44:]
        shared = X25519PrivateKey.from_private_bytes(ctx.private_key).exchange(X25519PublicKey.from_public_bytes(eph_pub))
        return AESGCM(_hkdf(shared, _aad("rs1", ctx.uid))).decrypt(nonce, ct, _aad("rs1", ctx.uid))
    except (InvalidTag, ValueError) as exc:
        raise VaultError("This value is damaged, or it belongs to someone else.") from exc


def open_value(value: str, ctx: userctx.UserContext | None = None) -> str:
    """Envelope -> text. Raises VaultLocked with no key, VaultError when the value is tampered with or not this person's."""
    ctx = _ctx(ctx)
    if not is_envelope(value):
        raise VaultError("This is not a sealed value.")
    try:
        blob = _unb64(value[4:])
    except ValueError as exc:
        raise VaultError("This value is damaged.") from exc
    try:
        return _open_raw(value[:4], blob, ctx).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VaultError("This value is damaged.") from exc


def open_json(value: str, ctx: userctx.UserContext | None = None) -> Any:
    return json.loads(open_value(value, ctx))


def open_bytes(value: bytes, ctx: userctx.UserContext | None = None) -> bytes:
    ctx = _ctx(ctx)
    head = bytes(value[:4])
    if head not in (RX.encode(), RS.encode()):
        raise VaultError("This is not a sealed value.")
    return _open_raw(head.decode(), bytes(value[4:]), ctx)


def open_cell(value: Any, ctx: userctx.UserContext) -> Any:
    """What the row factory does with one cell. Not an envelope: unchanged. A forged or damaged one: left as text.
    No key: strict raises VaultLocked, otherwise None. Laptop mode never gets here."""
    if not is_envelope(value):
        return value
    try:
        return open_value(value, ctx)
    except VaultLocked:
        if ctx.strict:
            raise
        return None
    except VaultError:
        return value


# ───────────── keyed hashes ─────────────

def _blind_key(ctx: userctx.UserContext) -> bytes:
    if ctx.key is None:
        raise VaultLocked("Sign in again: this needs your key.")
    return _hkdf(ctx.key, _aad("blind", ctx.uid))


def blind(text: str | None, ctx: userctx.UserContext | None = None) -> str | None:
    """A stable per-person fingerprint of `text` (HMAC under a key from the data key). Needs a signed-in session."""
    if text is None or not _active():
        return text
    digest = hmac.new(_blind_key(_ctx(ctx)), text.encode("utf-8"), hashlib.sha256).hexdigest()
    return BLIND + digest


def _server_blind_key() -> bytes:
    key = config.blind_key()
    if key is None:
        raise BlindKeyMissing("RECTIFI_BLIND_KEY is not set, so keyed hashes cannot be made. Run `python scripts/dev.py set-password`.")
    return key


def blind_key_fingerprint() -> str:
    """A short fingerprint of the server blind key, stored per person (users.blind_fp) to notice that the key changed. It is an
    HMAC of a fixed label under the key, cut to 64 bits: it cannot be turned back into the key."""
    return hmac.new(_server_blind_key(), b"rectifi|blind-key-fingerprint|v1", hashlib.sha256).hexdigest()[:16]


def blind_ingest(text: str | None, ctx: userctx.UserContext | None = None) -> str | None:
    """Like blind(), but it works while the person is signed out (the SMS gateway), so it cannot use the data key. Keyed by the
    per-person ingest salt (in the database) combined with the server's RECTIFI_BLIND_KEY (not in the database or its backups)."""
    if text is None or not _active():
        return text
    ctx = _ctx(ctx)
    if ctx.ingest_salt is None:
        raise VaultLocked("No ingest salt for this person.")
    key = _hkdf(_server_blind_key(), _aad("ingest", ctx.uid), salt=ctx.ingest_salt)
    return BLIND + hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


# ───────────── keys (used by sign-up, login and recovery; no database here) ─────────────

def new_data_key() -> bytes:
    return os.urandom(32)


def new_ingest_salt() -> bytes:
    return os.urandom(32)


def new_keypair() -> tuple[bytes, bytes]:
    """(public, private) X25519 raw bytes."""
    private = X25519PrivateKey.generate()
    return (private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
            private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))


def derive_password_keys(password: str, salt: bytes) -> tuple[bytes, bytes]:
    """scrypt(password) -> (verifier, key-encrypting key). Two halves of one 64-byte output, so checking a password and
    unlocking the data key cost the same and neither is a cheap target. Store only sha256(verifier)."""
    with _SCRYPT_SLOTS:
        raw = Scrypt(salt=salt, length=64, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(password.encode("utf-8"))
    return raw[:32], raw[32:]


def verifier_hash(verifier: bytes) -> bytes:
    return hashlib.sha256(verifier).digest()


def verifier_matches(verifier: bytes, stored_hash: bytes) -> bool:
    return hmac.compare_digest(verifier_hash(verifier), stored_hash)


def new_recovery_code() -> str:
    """125 random bits as 25 base32 characters in 5 groups: 'ABCDE-FGHIJ-...'. Shown once."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    chars = "".join(alphabet[secrets.randbelow(32)] for _ in range(25))
    return "-".join(chars[i:i + 5] for i in range(0, 25, 5))


def recovery_key(code: str, salt: bytes) -> bytes:
    """The key-encrypting key a recovery code gives. The code is high-entropy, so HKDF is enough (no scrypt needed)."""
    clean = "".join(ch for ch in code.upper() if ch.isalnum())
    return _hkdf(clean.encode("ascii", errors="ignore"), b"rectifi|recovery", salt=salt)


def wrap_key(kek: bytes, secret: bytes, label: str) -> bytes:
    """Encrypt a key under another key. `label` binds the purpose (for example 'dek|7'), so wrapped keys cannot be swapped."""
    nonce = os.urandom(12)
    return nonce + AESGCM(kek).encrypt(nonce, secret, f"rectifi|wrap|{label}".encode())


def unwrap_key(kek: bytes, wrapped: bytes, label: str) -> bytes:
    try:
        return AESGCM(kek).decrypt(wrapped[:12], wrapped[12:], f"rectifi|wrap|{label}".encode())
    except InvalidTag as exc:
        raise VaultError("That key does not open this.") from exc
