"""The Phone page and the Add page's "filled in from an SMS" mode, using Streamlit's AppTest."""

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from streamlit.util import calc_hash
from test_app import APP, page_text, run_page

from rectifi import sms_gateway, tailscale, tools

STATUS = json.loads((Path(__file__).resolve().parent / "fixtures" / "tailscale_status.json").read_text(encoding="utf-8"))
NAME = "prag-laptop.tail1a2b3.ts.net"

UNREADABLE = "Rs.500.00 transferred from A/c XX1111 on 10-10-26 to your wallet ref 123"


@pytest.fixture(autouse=True)
def no_tailscale(monkeypatch):
    """By default this laptop has no Tailscale, whatever the machine running the tests has. Tests that want it use fake_tailscale."""
    monkeypatch.setattr(tailscale, "find_cli", lambda *a, **k: None)
    monkeypatch.delenv("RECTIFI_SMS_HOST", raising=False)


def fake_tailscale(monkeypatch, status=STATUS, ports=(443, 8443)):
    monkeypatch.setattr(tailscale, "find_cli", lambda *a, **k: "tailscale")
    monkeypatch.setattr(tailscale, "read_status", lambda cli, runner=None: status)
    monkeypatch.setattr(tailscale, "read_served_ports", lambda cli, runner=None: set(ports))


@pytest.fixture
def gateway_down(monkeypatch):
    monkeypatch.setattr(sms_gateway, "gateway_reachable", lambda *a, **k: False)
    monkeypatch.setenv("RECTIFI_SMS_PORT", "8765")


@pytest.fixture
def gateway_up(monkeypatch):
    monkeypatch.setattr(sms_gateway, "gateway_reachable", lambda *a, **k: True)
    monkeypatch.setattr(sms_gateway, "send_test_sms", lambda token, port=None, timeout=5.0: (200, "ignored"))
    monkeypatch.setenv("RECTIFI_SMS_PORT", "8765")


def codes(at) -> str:
    return "\n".join(c.value for c in at.code)


def button(at, label):
    return next(b for b in at.button if b.label == label)


def pair(at, name="My Android"):
    at.text_input[0].set_value(name)
    button(at, "Create a pairing token").click().run()
    assert not at.exception
    return at


def test_phone_is_after_accounts_in_the_navigation():
    source = Path(APP).read_text(encoding="utf-8")
    order = re.findall(r'^    "(\w+)": st\.Page\(', source, flags=re.M)
    assert order[order.index("accounts") + 1] == "phone"
    assert ':material/smartphone:' in source


def test_phone_page_with_the_gateway_down_says_how_to_turn_it_on(gateway_down):
    at = run_page("phone")
    assert not at.exception
    text, code = page_text(at), codes(at)
    assert "Nothing can read SMS from a phone number" in text
    assert "Off" in text and "127.0.0.1:8765/health" in text                 # status has a word, not just a colour
    assert "RECTIFI_SMS_GATEWAY=1" in code and "RECTIFI_SMS_PORT=8765" in code
    assert "python scripts/dev.py phone" in code
    assert "Private networks only" in text
    assert "campus Wi-Fi" in text.lower() or "Campus Wi-Fi" in text
    assert "Tailscale" in text and "hotspot" in text
    assert "No phone paired yet" in text and "Nothing waiting" in text


def test_phone_page_with_the_gateway_up_says_so(gateway_up):
    at = run_page("phone")
    assert not at.exception
    assert "On" in page_text(at) and "RECTIFI_SMS_GATEWAY=1" not in codes(at)


def test_pairing_shows_everything_to_paste_and_shows_the_token_once(gateway_up):
    at = pair(run_page("phone"))
    code = codes(at)
    token = re.search(r"Bearer (rf_[A-Za-z0-9_-]+)", code).group(1)
    assert tools.authenticate_device(token)["name"] == "My Android"
    assert f'"Authorization": "Bearer {token}"' in code
    assert '{"from": "%from%", "text": "%text%", "sentStamp": %sentStamp%}' in code
    assert "(?i)^[A-Z]{2}-[A-Z0-9]+" in code
    assert r"(?i)(debited|credited|spent|received|withdrawn|Rs\.?|INR)" in code
    assert not re.search(r"http://[\d.]+:8765/sms", code)                  # the default gateway is not on the LAN, so no LAN address
    assert "listens on this laptop only" in " ".join(i.value for i in at.info)
    text = page_text(at)
    assert "tech.bogomolov.incomingsmsgateway" in text and "can't be shown again" in " ".join(w.value for w in at.warning)
    assert any("untested" in e.label.lower() for e in at.expander)               # the iPhone steps are marked untested

    # the test button works and is explained
    button(at, "Send a test message").click().run()
    assert any("It works" in s.value for s in at.success)

    # "Done" hides it, and a brand-new session can't find it anywhere
    button(at, "Done: hide the token").click().run()
    assert token not in codes(at) and token not in page_text(at)
    fresh = run_page("phone")
    assert token not in codes(fresh) + page_text(fresh)
    assert "My Android" in page_text(fresh) and "Active" in page_text(fresh) and "never" in page_text(fresh)
    from rectifi.db import connect

    with connect() as conn:
        assert token not in str(conn.execute("SELECT * FROM phone_devices").fetchall())


def test_the_test_button_is_off_while_the_gateway_is_down(gateway_down):
    at = pair(run_page("phone"))
    assert button(at, "Send a test message").disabled


def test_an_empty_name_is_refused(gateway_down):
    at = run_page("phone")
    button(at, "Create a pairing token").click().run()
    assert any("name" in e.value for e in at.error)
    assert tools.list_devices() == []


def test_revoking_a_phone_stops_its_token(gateway_up):
    device = tools.create_device("Old phone")
    at = run_page("phone")
    assert "Active" in page_text(at)
    button(at, "Revoke").click().run()
    assert not at.exception
    assert tools.authenticate_device(device["token"]) is None
    assert "Revoked" in page_text(at) and not [b for b in at.button if b.label == "Revoke"]


def test_the_needs_review_list_shows_raw_text_and_dismiss_removes_it(gateway_down):
    device = tools.create_device("P")
    assert tools.ingest_sms(device["id"], "AX-HDFCBK", UNREADABLE)["status"] == "needs_review"
    at = run_page("phone")
    assert not at.exception
    assert UNREADABLE in codes(at) and "Needs review (1)" in page_text(at)
    assert {"Add", "Dismiss"} <= {b.label for b in at.button}
    button(at, "Dismiss").click().run()
    assert tools.list_inbox("pending") == [] and "Needs review (0)" in page_text(at)


def prefilled_add_page(item_id):
    at = AppTest.from_file(str(APP), default_timeout=30)
    at._page_hash = calc_hash("add")
    at.session_state["_prefill"] = tools.inbox_prefill(item_id)
    at.run()
    return at


def test_add_page_is_filled_in_from_an_inbox_sms_and_saving_marks_it_added():
    tools.add_account("Savings", "1111")
    device = tools.create_device("P")
    tools.ingest_sms(device["id"], "AX-HDFCBK", UNREADABLE)
    [item] = tools.list_inbox("pending")
    at = prefilled_add_page(item["id"])
    assert not at.exception
    assert UNREADABLE in codes(at) and any("phone sent" in i.value for i in at.info)
    assert at.text_input[0].value == "500.00"                                        # the amount the fixed rules found
    assert next(s for s in at.selectbox if s.label == "Account").value == "Savings"  # last 4 digits matched
    button(at, "Save entry").click().run()
    assert not at.exception
    [t] = tools.list_transactions()
    assert (t["amount"], t["source"], t["account"]) == (Decimal("500.00"), "sms", "Savings")
    assert tools.list_inbox("pending") == [] and len(tools.list_inbox("added")) == 1
    assert "_prefill" not in at.session_state


def test_the_prefill_can_be_forgotten():
    device = tools.create_device("P")
    tools.ingest_sms(device["id"], "AX-HDFCBK", UNREADABLE)
    at = prefilled_add_page(tools.list_inbox("pending")[0]["id"])
    button(at, "Forget this SMS").click().run()
    assert not at.exception and "_prefill" not in at.session_state
    assert at.text_input[0].value == "" and not at.code
    assert len(tools.list_inbox("pending")) == 1                                       # still waiting on the Phone page


def test_prefill_carries_over_direction_date_and_payee_when_the_sms_has_them():
    """An SMS held back only because it mentions an OTP still has a readable amount, direction, date and payee."""
    tools.add_account("Savings", "1111")
    device = tools.create_device("P")
    tools.ingest_sms(device["id"], "AX-HDFCBK", "Rs 700.00 credited to A/c XX1111 on 10-10-26 from RAVI. Never share OTP.")
    [item] = tools.list_inbox("pending")
    at = prefilled_add_page(item["id"])
    assert at.get("button_group")[0].value == "Money in"
    assert at.date_input[0].value == date(2026, 10, 10)
    assert [t.value for t in at.text_input[:2]] == ["700.00", "RAVI"]
    button(at, "Save entry").click().run()
    [t] = tools.list_transactions()
    assert (t["amount"], t["direction"], t["txn_date"], t["payee"], t["source"]) == (
        Decimal("700.00"), "credit", date(2026, 10, 10), "RAVI", "sms")


# ───────────────────────── Tailscale: the HTTPS address comes first ─────────────────────────

def test_the_phone_card_shows_the_https_app_address_and_says_it_is_published(gateway_up, monkeypatch):
    fake_tailscale(monkeypatch)
    at = run_page("phone")
    assert not at.exception
    assert f"https://{NAME}/" in codes(at)
    text = page_text(at)
    assert "Open Rectifi on your phone" in text and "Published" in text and "Add to Home screen" in text


def test_the_phone_card_says_when_serve_is_not_set_up_yet(gateway_up, monkeypatch):
    fake_tailscale(monkeypatch, ports=())
    text = page_text(run_page("phone"))
    assert "Not published yet" in text and "python scripts/dev.py tailscale" in text


def test_the_phone_card_when_tailscale_is_installed_but_signed_out(gateway_up, monkeypatch):
    fake_tailscale(monkeypatch, status={"BackendState": "NeedsLogin", "Self": {}})
    text = page_text(run_page("phone"))
    assert "Tailscale is off" in text and f"https://{NAME}" not in text


def test_the_phone_card_when_tailscale_is_not_installed(gateway_up):
    text = page_text(run_page("phone"))
    assert "Not set up" in text and "Install <b>Tailscale</b>" in text and "Use Rectifi on your phone" in text


def test_pairing_puts_the_tailscale_https_gateway_url_first_and_no_lan_url(gateway_up, monkeypatch):
    fake_tailscale(monkeypatch)
    monkeypatch.setattr(sms_gateway, "private_ipv4s", lambda: [{"ip": "192.168.1.9", "label": "Local network"}])
    at = pair(run_page("phone"))
    code = codes(at)
    assert f"https://{NAME}:8443/sms" in code
    assert "192.168.1.9" not in code and "http://" not in re.sub(r"https://", "", code)       # the default host is 127.0.0.1: no LAN URL
    assert f"https://{NAME}:8443/health" in page_text(at)                                       # the phone-browser check uses https too


def test_the_lan_url_comes_back_only_when_the_host_is_opened_and_it_is_second(gateway_up, monkeypatch):
    fake_tailscale(monkeypatch)
    monkeypatch.setenv("RECTIFI_SMS_HOST", "0.0.0.0")
    monkeypatch.setattr(sms_gateway, "private_ipv4s", lambda: [{"ip": "192.168.1.9", "label": "Local network"}])
    at = pair(run_page("phone"))
    code = codes(at)
    assert code.index(f"https://{NAME}:8443/sms") < code.index("http://192.168.1.9:8765/sms")
    assert any("open to everyone on the same network" in w.value for w in at.warning)


def test_an_unpublished_gateway_is_called_out_on_the_pairing_panel(gateway_up, monkeypatch):
    fake_tailscale(monkeypatch, ports=(443,))
    at = pair(run_page("phone"))
    assert any("not published on your tailnet yet" in w.value for w in at.warning)


def test_an_open_gateway_host_shows_a_warning_next_to_the_status(gateway_up, monkeypatch):
    monkeypatch.setenv("RECTIFI_SMS_HOST", "0.0.0.0")
    at = run_page("phone")
    assert any("open to everyone on the same network" in w.value for w in at.warning)


# --- the one-file setup: "Download setup file for SMS to URL Forwarder" ---

DOWNLOAD_LABEL = "Download setup file for SMS to URL Forwarder"


@pytest.fixture
def downloads(monkeypatch):
    """Record every st.download_button call (AppTest only gives a mock URL, never the bytes), then call the real one."""
    import streamlit as st

    calls = []
    real = st.download_button

    def spy(label, data, *args, **kwargs):
        calls.append({"label": label, "data": data, **kwargs})
        return real(label, data, *args, **kwargs)

    monkeypatch.setattr(st, "download_button", spy)
    return calls


def test_no_download_button_before_pairing(gateway_up, downloads, monkeypatch):
    fake_tailscale(monkeypatch)
    at = run_page("phone")
    assert not at.exception
    assert at.get("download_button") == [] and downloads == []
    assert "rectifi-sms-setup" not in page_text(at) + codes(at)


def test_after_pairing_the_download_holds_a_valid_rule_with_the_new_token(gateway_up, downloads, monkeypatch):
    fake_tailscale(monkeypatch)
    at = pair(run_page("phone"))
    token = re.search(r"Bearer (rf_[A-Za-z0-9_-]+)", codes(at)).group(1)
    [element] = at.get("download_button")
    assert element.proto.label == DOWNLOAD_LABEL and element.proto.type == "primary" and not element.proto.disabled
    call = downloads[-1]
    assert call["file_name"] == "rectifi-sms-setup.json" and call["mime"] == "application/json"
    assert call["type"] == "primary" and call["width"] == "stretch"          # full width, and the primary action
    assert isinstance(call["data"], bytes)                                    # in memory, never a path on the laptop's disk
    [rule] = json.loads(call["data"])
    device = next(d for d in tools.list_devices() if d["name"] == "My Android")
    assert rule["key"] == f"rectifi-{device['id']}"
    assert rule["url"] == f"https://{NAME}:8443/sms" and rule["sender"] == "*"
    assert json.loads(rule["headers"])["Authorization"] == f"Bearer {token}"
    assert tools.authenticate_device(token)["name"] == "My Android"           # it is the real, working token
    assert rule["sms_filter"] == r"(?i)(debited|credited|spent|received|withdrawn|Rs\.?|INR)"
    assert rule["template"] == '{"from": "%from%", "text": "%text%", "sentStamp": %sentStamp%}'


def test_the_four_steps_and_the_delete_reminder_are_shown(gateway_up, monkeypatch):
    fake_tailscale(monkeypatch)
    text = page_text(pair(run_page("phone")))
    assert "Import" in text and "rectifi-sms-setup.json" in text and "Downloads" in text
    assert "Unrestricted" in text and "Send a test message" in text
    assert "Delete rectifi-sms-setup.json from Downloads afterwards. It contains your key." in text
    for n in "1234":
        assert f"{n}. " in text


def test_the_manual_values_stay_in_a_collapsed_expander(gateway_up, monkeypatch):
    fake_tailscale(monkeypatch)
    at = pair(run_page("phone"))
    [hand] = [e for e in at.expander if e.label == "Set it up by hand instead"]
    assert hand.proto.expanded is False
    inside = "\n".join(c.value for c in hand.code)
    token = re.search(r"Bearer (rf_[A-Za-z0-9_-]+)", inside).group(1)
    assert f'"Authorization": "Bearer {token}"' in inside
    assert f"https://{NAME}:8443/sms" in inside
    assert '"sentStamp": %sentStamp%' in inside and "(?i)^[A-Z]{2}-[A-Z0-9]+" in inside and "INR" in inside
    outside = "\n".join(c.value for c in at.code if c not in hand.code)
    assert token not in outside                                               # the token is not shown loose on the page any more


def test_with_no_address_the_download_is_disabled_and_says_how_to_fix_it(gateway_up, downloads):
    at = pair(run_page("phone"))                                               # no Tailscale, host is localhost
    [element] = at.get("download_button")
    assert element.proto.disabled
    assert downloads[-1]["data"] == b""                                       # nothing with a token in it is offered
    assert any("python scripts/dev.py tailscale" in i.value for i in at.info)
    assert tools.list_devices()                                               # the token itself was still created


def test_with_an_opened_host_and_no_tailscale_the_download_uses_the_lan_address(gateway_up, downloads, monkeypatch):
    monkeypatch.setenv("RECTIFI_SMS_HOST", "0.0.0.0")
    monkeypatch.setattr(sms_gateway, "private_ipv4s", lambda: [{"ip": "192.168.1.9", "label": "Local network"}])
    at = pair(run_page("phone"))
    assert not at.get("download_button")[0].proto.disabled
    [rule] = json.loads(downloads[-1]["data"])
    assert rule["url"] == "http://192.168.1.9:8765/sms"


def test_the_download_goes_away_with_the_token(gateway_up, downloads, monkeypatch):
    fake_tailscale(monkeypatch)
    at = pair(run_page("phone"))
    assert at.get("download_button")
    button(at, "Done: hide the token").click().run()
    assert at.get("download_button") == []
    fresh = run_page("phone")
    assert fresh.get("download_button") == []


def test_the_theme_makes_download_buttons_tall_enough_on_a_phone():
    css = (Path(APP).parent / "theme.py").read_text(encoding="utf-8")
    assert re.search(r'max-width: 720px.*?\[data-testid="stDownloadButton"\] button \{\{ min-height: 44px', css, re.S)
