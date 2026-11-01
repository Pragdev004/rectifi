"""Web Push, written small: VAPID (RFC 8292), message encryption (RFC 8291) and one POST. No model is involved, ever.

Only `cryptography`, `http_ece` and `httpx` are used. The VAPID key pair is made on first use:
  env RECTIFI_VAPID_PRIVATE_KEY  (base64url of the raw 32-byte private key), or
  file .rectifi/vapid_private.pem (created with mode 0600; the folder is in .gitignore).
The private key is never logged, returned or put in an error message. The same goes for a subscription's `endpoint`:
it is a capability URL (whoever has it can push to that phone), so it is only ever used to send, never printed.

If the key is replaced, every phone has to turn alerts on again (a subscription is tied to the key it was made with).
`key_fingerprint()` lets the page show that a change happened.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import http_ece
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .tools import RectifiError

log = logging.getLogger("rectifi.webpush")

KEY_ENV = "RECTIFI_VAPID_PRIVATE_KEY"
SUB_ENV = "RECTIFI_VAPID_SUB"
KEY_DIR = Path(__file__).resolve().parent.parent / ".rectifi"
KEY_FILE = "vapid_private.pem"
JWT_LIFETIME = 12 * 3600                 # push services refuse more than 24 hours
MAX_PAYLOAD = 2048                       # bytes of JSON before encryption (the services allow about 4 KB after)
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")   # SSRF guard
FALLBACK_SUB = "mailto:rectifi@localhost.invalid"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_KEY: ec.EllipticCurvePrivateKey | None = None


class _HidePushUrls(logging.Filter):
    """httpx logs every request URL at INFO. A push URL is a secret, so those lines are dropped (other httpx lines stay)."""

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        return not any(host in text for host in PUSH_HOSTS)


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_HidePushUrls())


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ───────────────────────── keys ─────────────────────────

def _from_env(raw: str) -> ec.EllipticCurvePrivateKey:
    try:
        return ec.derive_private_key(int.from_bytes(b64u_decode(raw.strip()), "big"), ec.SECP256R1())
    except Exception as exc:                                     # never include the value in the message
        raise RectifiError(f"{KEY_ENV} is not a valid base64url P-256 private key.") from exc


def load_or_create_keys() -> ec.EllipticCurvePrivateKey:
    """The VAPID private key: from the environment, else from .rectifi/vapid_private.pem (created 0600 on first use)."""
    global _KEY
    if _KEY is not None:
        return _KEY
    raw = os.getenv(KEY_ENV, "").strip()
    if raw:
        _KEY = _from_env(raw)
        return _KEY
    path = KEY_DIR / KEY_FILE
    if path.exists():
        _KEY = serialization.load_pem_private_key(path.read_bytes(), password=None)
        return _KEY
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    try:
        KEY_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
    except FileExistsError:                                      # another process was first: use its key
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except OSError as exc:
        raise RectifiError("Rectifi cannot save its push key here. Set RECTIFI_VAPID_PRIVATE_KEY instead.") from exc
    _KEY = key
    return _KEY


def _reset_for_tests() -> None:
    global _KEY
    _KEY = None


def public_key_bytes() -> bytes:
    return load_or_create_keys().public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key_b64() -> str:
    """The application server key the browser needs to subscribe (base64url of the 65-byte public point)."""
    return b64u(public_key_bytes())


def key_fingerprint() -> str:
    """A short fingerprint of the public key, shown on the Delivery tab so you can tell when the key changed."""
    digest = hashlib.sha256(public_key_bytes()).hexdigest()[:12]
    return " ".join(digest[i:i + 4] for i in range(0, 12, 4))


# ───────────────────────── VAPID ─────────────────────────

def _first_origin() -> str | None:
    for origin in (os.getenv("RECTIFI_ALLOWED_ORIGINS") or "").split(","):
        origin = origin.strip().rstrip("/")
        if origin.startswith("https://"):
            return origin
    for host in (os.getenv("RECTIFI_ALLOWED_HOSTS") or "").split(","):
        host = host.strip()
        if host and "*" not in host and host not in ("localhost", "127.0.0.1", "::1"):
            return "https://" + host
    return None


def vapid_subject() -> str:
    return os.getenv(SUB_ENV, "").strip() or _first_origin() or FALLBACK_SUB


def _endpoint_origin(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    return f"{parts.scheme}://{parts.netloc}"


def vapid_auth(endpoint: str, now: float | datetime | None = None) -> str:
    """The Authorization header value: 'vapid t=<ES256 JWT>,k=<public key>'. aud is the push service's origin; exp is 12 hours away."""
    if isinstance(now, datetime):
        now = now.timestamp()
    issued = int(time.time() if now is None else now)
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": _endpoint_origin(endpoint), "exp": issued + JWT_LIFETIME, "sub": vapid_subject()}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    r, s = decode_dss_signature(load_or_create_keys().sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={header}.{claims}.{signature},k={public_key_b64()}"


# ───────────────────────── encryption and sending ─────────────────────────

def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None, private_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """RFC 8291 aes128gcm for one message. A fresh ephemeral P-256 key is made per message unless a test passes its own."""
    return http_ece.encrypt(
        payload,
        salt=salt,
        private_key=private_key or ec.generate_private_key(ec.SECP256R1()),
        dh=b64u_decode(p256dh),
        auth_secret=b64u_decode(auth),
        version="aes128gcm",
    )


def allowed_endpoint(url: str) -> bool:
    """Only real push services, over https, on the normal port, with no user name. This stops a saved 'subscription' from making
    Rectifi call localhost or your router (SSRF)."""
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or not host or parts.username or parts.password or port not in (None, 443):
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in PUSH_HOSTS)


def build_payload(title: str, body: str, url: str, tag: str | None) -> bytes:
    """The JSON the service worker shows: {'t': title, 'b': body, 'u': '/alerts#12', 'tag': ...}. Short keys keep it small."""
    data = {"t": title, "b": body, "u": url}
    if tag:
        data["tag"] = tag
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def send(sub: dict, payload: bytes, ttl: int = 3600, urgency: str = "normal", client: httpx.Client | None = None) -> str:
    """Send one push to one phone. Returns 'ok', 'gone' (the phone unsubscribed: delete it), 'retry' (try later) or 'too_big'."""
    if urgency not in ("very-low", "low", "normal", "high"):
        urgency = "normal"
    endpoint = sub["endpoint"]
    if not allowed_endpoint(endpoint):
        log.warning("push refused: the subscription is not on a known push service")
        return "gone"
    if len(payload) > MAX_PAYLOAD:
        return "too_big"
    try:
        body = encrypt(payload, sub["p256dh"], sub["auth"])
        headers = {"TTL": str(int(ttl)), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                   "Urgency": urgency, "Authorization": vapid_auth(endpoint)}
        owned = client is None
        http = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        try:
            response = http.post(endpoint, content=body, headers=headers)
        finally:
            if owned:
                http.close()
    except Exception as exc:                                     # network, bad key material: never print the endpoint or keys
        log.warning("push failed: %s", type(exc).__name__)
        return "retry"
    status = response.status_code
    if status in (200, 201, 202):
        return "ok"
    if status in (404, 410):
        return "gone"
    if status == 413:
        return "too_big"
    log.warning("push service answered HTTP %s", status)
    return "retry"
