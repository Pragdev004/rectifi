"""P3: the Web Push transport. Encryption, VAPID signing, headers, status handling, the endpoint allow-list, no secrets in logs."""

import json
import logging
import os
import stat
import time

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from rectifi import webpush
from rectifi.webpush import b64u, b64u_decode

ENDPOINT = "https://fcm.googleapis.com/fcm/send/SECRET-ENDPOINT-TOKEN-123"


@pytest.fixture(autouse=True)
def vapid_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(webpush, "KEY_DIR", tmp_path / ".rectifi")
    monkeypatch.delenv(webpush.KEY_ENV, raising=False)
    monkeypatch.delenv(webpush.SUB_ENV, raising=False)
    monkeypatch.delenv("RECTIFI_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("RECTIFI_ALLOWED_HOSTS", raising=False)
    webpush._reset_for_tests()
    yield
    webpush._reset_for_tests()


def phone():
    """A fake phone: the private key it keeps, and the subscription it would send to the server."""
    private = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return private, auth, {"endpoint": ENDPOINT, "p256dh": b64u(public), "auth": b64u(auth)}


# ───────────── encryption ─────────────

def test_encrypt_then_decrypt_round_trip():
    private, auth, sub = phone()
    message = webpush.build_payload("Swiggy: 80% of weekly cap", "₹1,200 of ₹1,500 spent this week.", "/alerts#4", "merchant:Swiggy:week")
    body = webpush.encrypt(message, sub["p256dh"], sub["auth"])
    assert http_ece.decrypt(body, private_key=private, auth_secret=auth, version="aes128gcm") == message
    assert body != webpush.encrypt(message, sub["p256dh"], sub["auth"])           # fresh salt and key every time


def test_rfc8291_appendix_a_vector():
    as_private = ec.derive_private_key(int.from_bytes(b64u_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    body = webpush.encrypt(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        salt=b64u_decode("DGv6ra1nlYgDCS1FRnbzlw"), private_key=as_private)
    assert b64u(body) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
                          "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


# ───────────── VAPID ─────────────

def test_vapid_jwt_verifies_and_has_the_right_claims():
    now = 1_800_000_000
    header = webpush.vapid_auth(ENDPOINT, now)
    assert header.startswith("vapid t=") and ",k=" in header
    token, key = header[len("vapid t="):].split(",k=")
    head, claims, signature = token.split(".")
    assert json.loads(b64u_decode(head)) == {"typ": "JWT", "alg": "ES256"}
    body = json.loads(b64u_decode(claims))
    assert body["aud"] == "https://fcm.googleapis.com" and body["exp"] == now + 12 * 3600 and body["exp"] - now <= 24 * 3600
    assert body["sub"]
    raw = b64u_decode(signature)
    assert len(raw) == 64
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64u_decode(key))
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")), f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert key == webpush.public_key_b64() and len(b64u_decode(key)) == 65


def test_subject_comes_from_env_then_origin_then_fallback(monkeypatch):
    assert webpush.vapid_subject() == webpush.FALLBACK_SUB
    monkeypatch.setenv("RECTIFI_ALLOWED_HOSTS", "localhost,*.ts.net,pragadeeshwar.tail94c6aa.ts.net")
    assert webpush.vapid_subject() == "https://pragadeeshwar.tail94c6aa.ts.net"
    monkeypatch.setenv("RECTIFI_ALLOWED_ORIGINS", "https://rectifi.example.org/")
    assert webpush.vapid_subject() == "https://rectifi.example.org"
    monkeypatch.setenv(webpush.SUB_ENV, "mailto:me@example.org")
    assert webpush.vapid_subject() == "mailto:me@example.org"


def test_key_is_created_once_with_private_permissions_and_reused(tmp_path):
    first = webpush.public_key_b64()
    path = tmp_path / ".rectifi" / webpush.KEY_FILE
    assert path.exists()
    if os.name != "nt":                                                            # Windows has no 0600; the file sits in your own profile
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    webpush._reset_for_tests()
    assert webpush.public_key_b64() == first                                       # read back from the file
    assert len(webpush.key_fingerprint().split()) == 3


def test_key_from_environment(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    raw = private.private_numbers().private_value.to_bytes(32, "big")
    monkeypatch.setenv(webpush.KEY_ENV, b64u(raw))
    webpush._reset_for_tests()
    want = b64u(private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    assert webpush.public_key_b64() == want


# ───────────── sending ─────────────

def mock_client(status, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_headers_and_ok():
    private, auth, sub = phone()
    seen = []
    assert webpush.send(sub, b'{"t":"x","b":"y","u":"/alerts"}', ttl=900, urgency="high", client=mock_client(201, seen)) == "ok"
    (request,) = seen
    assert str(request.url) == ENDPOINT
    assert request.headers["TTL"] == "900" and request.headers["Urgency"] == "high"
    assert request.headers["Content-Encoding"] == "aes128gcm" and request.headers["Authorization"].startswith("vapid t=")
    assert http_ece.decrypt(request.content, private_key=private, auth_secret=auth, version="aes128gcm") == b'{"t":"x","b":"y","u":"/alerts"}'


@pytest.mark.parametrize("status,want", [(201, "ok"), (200, "ok"), (404, "gone"), (410, "gone"), (429, "retry"), (500, "retry"), (503, "retry"),
                                          (403, "retry"), (413, "too_big")])
def test_send_status_mapping(status, want):
    _, _, sub = phone()
    assert webpush.send(sub, b"{}", client=mock_client(status)) == want


def test_send_network_error_is_retry_and_too_big_payload():
    _, _, sub = phone()

    def boom(request):
        raise httpx.ConnectError("down")

    assert webpush.send(sub, b"{}", client=httpx.Client(transport=httpx.MockTransport(boom))) == "retry"
    assert webpush.send(sub, b"x" * (webpush.MAX_PAYLOAD + 1), client=mock_client(201)) == "too_big"


def test_send_never_posts_to_a_disallowed_endpoint():
    _, _, sub = phone()
    sub["endpoint"] = "http://127.0.0.1:8501/api/steal"
    seen = []
    assert webpush.send(sub, b"{}", client=mock_client(201, seen)) == "gone" and seen == []


@pytest.mark.parametrize("url,ok", [
    ("https://fcm.googleapis.com/fcm/send/abc", True),
    ("https://updates.push.services.mozilla.com/wpush/v2/abc", True),
    ("https://web.push.apple.com/abc", True),
    ("https://wns2-par02p.notify.windows.com/?token=abc", True),
    ("https://fcm.googleapis.com:443/x", True),
    ("http://fcm.googleapis.com/fcm/send/abc", False),
    ("https://localhost/x", False), ("https://127.0.0.1/x", False), ("https://10.0.0.5/x", False), ("https://[::1]/x", False),
    ("https://evil.com/fcm.googleapis.com", False), ("https://fcm.googleapis.com.evil.com/x", False),
    ("https://notfcm.googleapis.com/x", False), ("https://user@fcm.googleapis.com/x", False),
    ("https://fcm.googleapis.com:8443/x", False), ("ftp://fcm.googleapis.com/x", False), ("", False), ("garbage", False),
])
def test_allowed_endpoint(url, ok):
    assert webpush.allowed_endpoint(url) is ok


def test_secrets_never_reach_the_logs_or_return_values(caplog):
    caplog.set_level(logging.DEBUG)
    _, _, sub = phone()
    private_pem = webpush.load_or_create_keys().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    secret_number = webpush.load_or_create_keys().private_numbers().private_value.to_bytes(32, "big")

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    for client in (httpx.Client(transport=httpx.MockTransport(boom)), mock_client(500), mock_client(201), mock_client(410)):
        result = webpush.send(sub, b"{}", client=client)
        assert result in ("ok", "gone", "retry")
    logged = caplog.text
    assert "SECRET-ENDPOINT-TOKEN-123" not in logged and sub["auth"] not in logged and sub["p256dh"] not in logged
    assert b64u(secret_number) not in logged and private_pem.splitlines()[1] not in logged
