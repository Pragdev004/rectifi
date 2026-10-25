"""The phone SMS gateway (rectifi/sms_gateway.py) and the tools behind it, driven by Starlette's TestClient.

Everything is deterministic: no language model is involved, and one test proves it by making every model call explode.
"""

import socket
import threading
import time
from datetime import date, datetime
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from rectifi import sms_gateway, tools
from rectifi.db import DatabaseUnavailable, connect

BANK_SMS = "Rs.250.00 debited from A/c XX1234 on 21-10-26 to VPA swiggy@icici UPI Ref No 429183746521"
NO_REF_SMS = "Rs.40.00 debited from A/c XX1234 on 20-10-26 to chai point"
OTP_SMS = "123456 is your OTP for login. Do not share it with anyone. -HDFC Bank"
PROMO_SMS = "Congratulations! Pre-approved credit limit of Rs 2,00,000 waiting for you. Apply now http://x.co/abc"
UNREADABLE_SMS = "Rs 500.00 transferred from A/c XX1234 to your wallet"        # money moved, but no direction the rules can prove


@pytest.fixture
def client():
    return TestClient(sms_gateway.create_app())


@pytest.fixture
def device():
    return tools.create_device("Test Android")


def post(client, token, body=None, **kwargs):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if body is not None:
        kwargs["json"] = body
    return client.post("/sms", headers=headers, **kwargs)


def sms(text, sender="AX-HDFCBK", stamp=None):
    body = {"from": sender, "text": text}
    if stamp is not None:
        body["sentStamp"] = stamp
    return body


# ───────────────────────── health and auth ─────────────────────────

def test_health_needs_no_login_and_reveals_nothing(client, device):
    reply = client.get("/health")
    assert reply.status_code == 200 and reply.json() == {"ok": True}
    assert device["name"] not in reply.text and "rf_" not in reply.text


def test_sms_without_a_token_is_401(client):
    assert post(client, None, sms(BANK_SMS)).status_code == 401
    assert client.post("/sms", headers={"Authorization": "Basic abc"}, json=sms(BANK_SMS)).status_code == 401
    assert client.post("/sms", headers={"Authorization": "Bearer "}, json=sms(BANK_SMS)).status_code == 401


def test_sms_with_a_wrong_token_is_401_and_stores_nothing(client, device):
    reply = post(client, device["token"] + "x", sms(BANK_SMS))
    assert reply.status_code == 401
    assert reply.headers["www-authenticate"] == "Bearer"
    assert tools.list_transactions() == []


def test_a_revoked_token_is_401(client, device):
    assert post(client, device["token"], sms(OTP_SMS)).status_code == 200
    assert tools.revoke_device(device["id"]) is True
    assert post(client, device["token"], sms(BANK_SMS)).status_code == 401
    assert tools.revoke_device(device["id"]) is False         # already revoked
    assert tools.list_transactions() == []


def test_other_methods_and_paths_are_not_served(client):
    assert client.get("/sms").status_code == 405
    assert client.get("/transactions").status_code == 404


def test_a_good_token_updates_last_seen(client, device):
    assert tools.list_devices()[0]["last_seen_at"] is None
    post(client, device["token"], sms(OTP_SMS))
    assert tools.list_devices()[0]["last_seen_at"] is not None


def test_too_many_wrong_tokens_from_one_client_are_throttled(monkeypatch):
    monkeypatch.setattr(sms_gateway, "FAILED_AUTH_LIMIT", 3)
    client = TestClient(sms_gateway.create_app())
    assert [post(client, "nope", sms(BANK_SMS)).status_code for _ in range(5)] == [401, 401, 401, 429, 429]


# ───────────────────────── what happens to an SMS ─────────────────────────

def test_a_bank_sms_is_added_and_the_same_sms_again_is_a_duplicate(client, device):
    tools.add_account("Savings", "1234")
    first = post(client, device["token"], sms(BANK_SMS, stamp=1792573200000))
    assert first.status_code == 200 and first.json() == {"status": "added"}
    again = post(client, device["token"], sms(BANK_SMS, stamp=1792573200000))
    assert again.status_code == 200 and again.json() == {"status": "duplicate"}
    [t] = tools.list_transactions()
    assert (t["amount"], t["direction"], t["source"], t["upi_ref"], t["txn_date"]) == (
        Decimal("250.00"), "debit", "sms", "429183746521", date(2026, 10, 21))
    assert t["account"] == "Savings" and t["category"] == "Food & Dining"


def test_the_same_upi_reference_arriving_in_a_different_sms_is_still_a_duplicate(client, device):
    post(client, device["token"], sms(BANK_SMS, stamp=1792573200000))
    other_wording = "Dear customer, INR 250.00 debited from account 1234 on 21-10-26. UPI Ref No 429183746521. Not you? Call bank"
    assert post(client, device["token"], sms(other_wording, stamp=1792573300000)).json() == {"status": "duplicate"}
    assert len(tools.list_transactions()) == 1


def test_a_retried_sms_without_a_upi_reference_is_one_transaction_but_two_real_ones_are_two(client, device):
    stamp = 1792573200000
    assert post(client, device["token"], sms(NO_REF_SMS, stamp=stamp)).json() == {"status": "added"}
    assert post(client, device["token"], sms(NO_REF_SMS, stamp=stamp)).json() == {"status": "duplicate"}   # the phone retried
    assert post(client, device["token"], sms(NO_REF_SMS, stamp=stamp + 60_000)).json() == {"status": "added"}  # a second real one
    assert len(tools.list_transactions()) == 2


def test_the_date_comes_from_the_phone_timestamp_when_the_sms_has_none(client, device):
    stamp = int(datetime(2026, 10, 5, 12, 0).astimezone().timestamp() * 1000)
    post(client, device["token"], sms("Rs 99.00 debited from A/c XX1234 to Chai Shop", stamp=stamp))
    assert tools.list_transactions()[0]["txn_date"] == date(2026, 10, 5)


def test_an_otp_is_ignored_and_nothing_is_stored(client, device):
    reply = post(client, device["token"], sms(OTP_SMS))
    assert reply.status_code == 200 and reply.json() == {"status": "ignored"}
    assert tools.list_transactions() == [] and tools.list_inbox("pending") == []
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM sms_seen").fetchone()["n"] == 0


def test_offers_and_chats_are_ignored(client, device):
    assert post(client, device["token"], sms(PROMO_SMS)).json() == {"status": "ignored"}
    assert post(client, device["token"], sms("Hi, I sent you Rs 500 for lunch, received?", sender="+919876543210")).json() == {"status": "ignored"}
    assert post(client, device["token"], sms("Your bill of Rs 500 is due on 25th")).json() == {"status": "ignored"}
    assert tools.list_transactions() == [] and tools.list_inbox("pending") == []


def test_an_unreadable_bank_sms_waits_in_the_inbox_and_is_not_guessed(client, device):
    reply = post(client, device["token"], sms(UNREADABLE_SMS))
    assert reply.status_code == 200 and reply.json() == {"status": "needs_review"}
    assert tools.list_transactions() == []
    [item] = tools.list_inbox("pending")
    assert item["raw_text"] == UNREADABLE_SMS and item["sender"] == "AX-HDFCBK" and item["device"] == "Test Android"
    assert item["status"] == "pending"
    # the phone sends it again: still one inbox row, and the phone is told 200 so it stops retrying
    again = post(client, device["token"], sms(UNREADABLE_SMS))
    assert again.status_code == 200 and again.json() == {"status": "duplicate"}
    assert len(tools.list_inbox("pending")) == 1


def test_an_sms_that_mentions_an_otp_but_also_says_debited_goes_to_review_not_to_the_books(client, device):
    text = "Rs 500.00 debited from A/c XX1234. Never share your OTP with anyone."
    assert post(client, device["token"], sms(text)).json() == {"status": "needs_review"}
    assert tools.list_transactions() == []


def test_a_date_in_the_future_goes_to_review(client, device):
    text = "Rs 500.00 debited from A/c XX1234 on 01-01-2099 to Someone"
    assert post(client, device["token"], sms(text)).json() == {"status": "needs_review"}
    assert "future" in tools.list_inbox("pending")[0]["reason"]


def test_inbox_dismiss_and_add_flow():
    client_ = TestClient(sms_gateway.create_app())
    token = tools.create_device("P")["token"]
    post(client_, token, sms(UNREADABLE_SMS))
    [item] = tools.list_inbox("pending")
    guess = tools.inbox_prefill(item["id"])
    assert guess["amount"] == Decimal("500.00") and guess["direction"] is None and guess["inbox_id"] == item["id"]
    assert tools.dismiss_inbox_item(item["id"]) is True
    assert tools.dismiss_inbox_item(item["id"]) is False
    assert tools.list_inbox("pending") == [] and len(tools.list_inbox("dismissed")) == 1
    post(client_, token, sms("Rs 70.00 transferred from A/c XX1234 to your wallet"))
    [second] = tools.list_inbox("pending")
    assert tools.mark_inbox_added(second["id"]) is True and tools.list_inbox("pending") == []
    with pytest.raises(tools.RectifiError):
        tools.inbox_prefill(99999)


# ───────────────────────── which account ─────────────────────────

def test_the_account_is_found_from_the_last_four_digits_in_the_sms(client, device):
    tools.add_account("Savings", "1111")
    tools.add_account("Wallet", "2222")
    post(client, device["token"], sms("Rs.60.00 debited from A/c XX2222 on 21-10-26 UPI Ref No 100000000001 to chai@ybl"))
    post(client, device["token"], sms("Rs.70.00 debited from A/c XX1111 on 21-10-26 UPI Ref No 100000000002 to tea@ybl"))
    accounts = {t["amount"]: t["account"] for t in tools.list_transactions()}
    assert accounts == {Decimal("60.00"): "Wallet", Decimal("70.00"): "Savings"}


def test_with_one_account_an_sms_that_names_no_account_goes_to_it(client, device):
    tools.add_account("Savings", "1111")
    post(client, device["token"], sms("Rs 80.00 debited on 21-10-26 UPI Ref No 100000000003 to tea@ybl"))
    assert tools.list_transactions()[0]["account"] == "Savings"


def test_with_two_accounts_an_sms_that_names_none_is_left_unlinked(client, device):
    tools.add_account("Savings", "1111")
    tools.add_account("Wallet", "2222")
    post(client, device["token"], sms("Rs 80.00 debited on 21-10-26 UPI Ref No 100000000003 to tea@ybl"))
    assert tools.list_transactions()[0]["account"] is None


def test_an_sms_naming_an_unknown_account_is_not_attached_by_guess(client, device):
    tools.add_account("Savings", "1111")
    post(client, device["token"], sms("Rs 90.00 debited from A/c XX9999 on 21-10-26 UPI Ref No 100000000004 to tea@ybl"))
    assert tools.list_transactions()[0]["account"] is None


def test_the_only_account_without_saved_digits_takes_an_sms_that_names_some(client, device):
    tools.add_account("Savings")
    post(client, device["token"], sms("Rs 90.00 debited from A/c XX9999 on 21-10-26 UPI Ref No 100000000004 to tea@ybl"))
    assert tools.list_transactions()[0]["account"] == "Savings"


# ───────────────────────── input checks ─────────────────────────

def test_rate_limit_is_per_device_and_answers_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(sms_gateway, "RATE_LIMIT", 3)
    client = TestClient(sms_gateway.create_app())
    one, two = tools.create_device("One")["token"], tools.create_device("Two")["token"]
    codes = [post(client, one, sms(OTP_SMS)).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert post(client, one, sms(OTP_SMS)).headers["retry-after"] == "60"
    assert post(client, two, sms(OTP_SMS)).status_code == 200            # another phone is not affected


def test_rate_limiter_forgets_old_hits():
    now = [0.0]
    limiter = sms_gateway.RateLimiter(2, window=60, clock=lambda: now[0])
    assert [limiter.allow("a"), limiter.allow("a"), limiter.allow("a")] == [True, True, False]
    now[0] = 61
    assert limiter.allow("a") is True


def test_a_body_over_8_kb_is_413(client, device):
    assert post(client, device["token"], sms("x" * 9000)).status_code == 413
    exactly_small = post(client, device["token"], sms(OTP_SMS))
    assert exactly_small.status_code == 200


def test_a_streamed_body_over_8_kb_is_413_even_without_a_content_length(client, device):
    def chunks():
        for _ in range(10):
            yield b"y" * 1000

    reply = client.post("/sms", headers={"Authorization": f"Bearer {device['token']}"}, content=chunks())
    assert reply.status_code == 413


@pytest.mark.parametrize("body", [b"not json at all", b"[1, 2, 3]", b'{"from": "AX-HDFCBK"}', b'{"text": ""}', b'{"text": 5}', b"\xff\xfe"],
                         ids=["not json", "a list", "no text", "empty text", "text is a number", "not utf-8"])
def test_a_malformed_body_is_422(client, device, body):
    reply = client.post("/sms", headers={"Authorization": f"Bearer {device['token']}", "Content-Type": "application/json"}, content=body)
    assert reply.status_code == 422 and "error" in reply.json()


def test_keys_are_matched_loosely_and_a_raw_newline_in_the_text_is_accepted(client, device):
    tools.add_account("Savings", "1234")
    raw = ('{"Sender": "AX-HDFCBK", "Message": "Rs.55.00 debited from A/c XX1234 on 21-10-26\nto VPA tea@ybl '
           'UPI Ref No 100000000009", "sentStamp": "1792573200000"}').encode()
    reply = client.post("/sms", headers={"Authorization": f"Bearer {device['token']}"}, content=raw)
    assert reply.json() == {"status": "added"}
    body = {"sender": "AX-HDFCBK", "body": "Rs.56.00 debited from A/c XX1234 on 21-10-26 UPI Ref No 100000000010 to a@ybl",
            "sentStamp": "2026-10-21T10:00:00+05:30"}
    assert post(client, device["token"], body).json() == {"status": "added"}


def test_an_sms_containing_double_quotes_still_arrives_when_the_app_pastes_it_in_raw(client, device):
    """The Android template does not escape the SMS text, so a quote inside it breaks the JSON. The exact template is rescued."""
    tools.add_account("Savings", "1234")
    raw = ('{"from": "AX-HDFCBK", "text": "Rs.65.00 debited from A/c XX1234 on 21-10-26 to VPA "tea"@ybl '
           'UPI Ref No 100000000011", "sentStamp": 1792573200000}').encode()
    reply = client.post("/sms", headers={"Authorization": f"Bearer {device['token']}"}, content=raw)
    assert reply.status_code == 200 and reply.json() == {"status": "added"}
    [t] = tools.list_transactions()
    assert t["amount"] == Decimal("65.00")
    with connect() as conn:
        stored = conn.execute("SELECT raw_text FROM transactions").fetchone()["raw_text"]
    assert stored.startswith("Rs.65.00 debited") and '"tea"' in stored
    # anything that is not that template is still refused
    junk = client.post("/sms", headers={"Authorization": f"Bearer {device['token']}"}, content=b'{"other": "x", "text": "a"b"}')
    assert junk.status_code == 422


def test_parse_stamp_understands_milliseconds_seconds_and_iso():
    ms = sms_gateway.parse_stamp(1792573200000)
    assert ms == sms_gateway.parse_stamp("1792573200000") == sms_gateway.parse_stamp(1792573200)
    assert sms_gateway.parse_stamp("2026-10-21T10:00:00Z") == datetime.fromisoformat("2026-10-21T10:00:00+00:00")
    assert sms_gateway.parse_stamp("2026-10-21T10:00:00").tzinfo is not None
    for junk in (None, True, "abc", 5, [1], {"a": 1}, "2999-01-01T00:00:00Z", 9_999_999_999_999):
        assert sms_gateway.parse_stamp(junk) is None


def test_database_down_is_503_so_the_phone_retries(monkeypatch, client, device):
    def down(*a, **k):
        raise DatabaseUnavailable("down")

    monkeypatch.setattr(tools, "ingest_sms", down)
    reply = post(client, device["token"], sms(BANK_SMS))
    assert reply.status_code == 503 and "Retry-After" in reply.headers


# ───────────────────────── secrets and models ─────────────────────────

def test_the_database_holds_only_a_hash_of_the_token(client, device):
    token = device["token"]
    post(client, token, sms(BANK_SMS))
    post(client, token, sms(UNREADABLE_SMS))
    with connect() as conn:
        tables = [r["tablename"] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]
        for table in tables:
            dump = " ".join(r["j"] for r in conn.execute(f'SELECT row_to_json(t)::text AS j FROM "{table}" t').fetchall())
            assert token not in dump and token[len("rf_"):] not in dump, table
        stored = conn.execute("SELECT token_hash FROM phone_devices").fetchone()["token_hash"]
    assert len(stored) == 64 and stored != token                            # a SHA-256 hex digest
    assert "token" not in tools.list_devices()[0] and "token_hash" not in tools.list_devices()[0]
    assert token.startswith("rf_") and len(token) >= 40


def test_two_devices_get_different_tokens_and_the_right_device_is_found():
    a, b = tools.create_device("A"), tools.create_device("B")
    assert a["token"] != b["token"]
    assert tools.authenticate_device(a["token"])["id"] == a["id"]
    assert tools.authenticate_device(b["token"])["name"] == "B"
    assert tools.authenticate_device("rf_" + "x" * 43) is None and tools.authenticate_device("") is None and tools.authenticate_device(None) is None
    with pytest.raises(tools.RectifiError):
        tools.create_device("   ")


def test_neither_the_token_nor_the_sms_text_reaches_the_logs(monkeypatch, caplog, client, device):
    def explode(*a, **k):
        raise RuntimeError(f"boom {BANK_SMS} {device['token']}")

    monkeypatch.setattr(tools, "ingest_sms", explode)
    with caplog.at_level("DEBUG"):
        reply = post(client, device["token"], sms(BANK_SMS))
    assert reply.status_code == 500 and reply.json() == {"error": "internal error"}
    assert device["token"] not in caplog.text and "429183746521" not in caplog.text and BANK_SMS not in reply.text


def test_ingesting_an_sms_never_calls_a_language_model(monkeypatch, client, device):
    import rectifi.agent.graph as graph
    import rectifi.agent.llm as llm

    def boom(*a, **k):
        raise AssertionError("an SMS must never reach an LLM")

    monkeypatch.setattr(llm, "get_llm", boom)
    monkeypatch.setattr(graph, "get_llm", boom)
    tools.add_account("Savings", "1234")
    codes = [post(client, device["token"], sms(t)).json()["status"] for t in (BANK_SMS, OTP_SMS, UNREADABLE_SMS, PROMO_SMS)]
    assert codes == ["added", "ignored", "needs_review", "ignored"]


def test_the_gateway_and_tools_do_not_import_any_model_code():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "rectifi"
    for name in ("sms_gateway.py", "tools.py", "parsing.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "rectifi.agent" not in source and "from .agent" not in source and "langchain" not in source, name


# ───────────────────────── helpers used by the Phone page ─────────────────────────

def test_private_ips_list_tailscale_first_and_skip_loopback_link_local_and_public(monkeypatch):
    class NoNetwork:
        def __init__(self, *a, **k): raise OSError("no route")

    monkeypatch.setattr(sms_gateway.socket, "socket", NoNetwork)
    monkeypatch.setattr(sms_gateway.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.168.1.20", 0)), (2, 1, 6, "", ("127.0.0.1", 0))])
    monkeypatch.setattr(sms_gateway.socket, "gethostbyname_ex",
                        lambda host: (host, [], ["192.168.1.20", "10.0.0.7", "100.101.102.103", "169.254.9.9", "8.8.4.4", "172.20.1.1"]))
    found = sms_gateway.private_ipv4s()
    assert [f["ip"] for f in found] == ["100.101.102.103", "192.168.1.20", "10.0.0.7", "172.20.1.1"]
    assert "Tailscale" in found[0]["label"] and "Tailscale" not in found[1]["label"]


def test_private_ips_returns_an_empty_list_when_the_machine_has_no_network(monkeypatch):
    def fail(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(sms_gateway.socket, "socket", fail)
    monkeypatch.setattr(sms_gateway.socket, "getaddrinfo", fail)
    monkeypatch.setattr(sms_gateway.socket, "gethostbyname_ex", fail)
    assert sms_gateway.private_ipv4s() == []


def test_a_real_server_answers_health_and_the_test_message_is_ignored_and_stores_nothing():
    """One cheap end-to-end check over a real socket: uvicorn in a thread on a free port."""
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(sms_gateway.create_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        assert sms_gateway.gateway_reachable(port) is True
        device = tools.create_device("Real socket")
        assert sms_gateway.send_test_sms(device["token"], port) == (200, "ignored")
        assert sms_gateway.send_test_sms("rf_wrong", port)[0] == 401
        assert tools.list_transactions() == [] and tools.list_inbox("pending") == []
        assert tools.list_devices()[0]["last_seen_at"] is not None
    finally:
        server.should_exit = True
        thread.join(10)
    assert sms_gateway.gateway_reachable(port, timeout=0.5) is False
    assert sms_gateway.send_test_sms("rf_x", port)[0] is None
