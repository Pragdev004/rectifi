"""The import file for the Android app "SMS to URL Forwarder" (package tech.bogomolov.incomingsmsgateway).

The app's Settings > Import reads a JSON file: an ARRAY of rule objects. This builds a file with exactly one rule that
already holds the gateway address, the device token and the filters, so nobody copies and pastes anything on a phone.

The format was read from the app's source (v2.4.0, ForwardingConfig.importFromJson). It has not been tried on a real phone.
Facts that matter, from that source:
- A rule missing "sender", "url", "template" or "headers" is skipped silently, so all four are always present.
- "headers" is a JSON object ENCODED AS A STRING, not a nested object.
- "key" is the rule id: importing the same key again replaces that rule instead of adding a second one.
- The file holds the plaintext token. It must only ever be built in memory and handed to the browser: never written to disk here.

Standard library only. It does not import the app, a model or langchain.
"""

from __future__ import annotations

import json

# The filters and body template the Phone page has always shown for the by-hand setup. One definition, used by both.
# Java regex (the Android app uses java.util.regex), so only constructs Java supports: (?i) and plain groups are fine.
TEXT_REGEX = r"(?i)(debited|credited|spent|received|withdrawn|Rs\.?|INR)"
SENDER_REGEX = r"(?i)^[A-Z]{2}-[A-Z0-9]+"          # by-hand setup only; the import file takes any sender (see below)
JSON_TEMPLATE = '{"from": "%from%", "text": "%text%", "sentStamp": %sentStamp%}'

FILE_NAME = "rectifi-sms-setup.json"
USER_AGENT = "SMS Forwarder App"
RULE_KEY_PREFIX = "rectifi-"


def build_forwarder_rules(url: str, token: str, *, device_id: int, text_filter: str) -> str:
    """The pretty JSON array (one rule) for SMS to URL Forwarder's Settings > Import.

    The sender is "*" (any) and not a regex: Indian bank sender IDs differ by phone and operator, so the text filter and the
    gateway's own deterministic classifier decide what is a transaction. `url` is the address the phone will POST to."""
    if not url or not token:
        raise ValueError("a URL and a token are both needed")
    rule = {
        "key": f"{RULE_KEY_PREFIX}{device_id}",
        "sender": "*",
        "is_sender_regex": False,
        "sms_filter": text_filter,
        "url": url,
        "sim_slot": 0,
        "template": JSON_TEMPLATE,
        "headers": json.dumps({"User-agent": USER_AGENT, "Authorization": f"Bearer {token}"}),
        "retries_number": 10,
        "ignore_ssl": False,
        "chunked_mode": False,
        "is_sms_enabled": True,
        "sign_hmac_sha256": False,
        "sign_hmac_sha256_secret": "",
        "store_failed": True,
        "local_mode": False,
    }
    return json.dumps([rule], indent=2)
