"""The import file for SMS to URL Forwarder (Settings > Import). Format taken from the app's source at v2.4.0."""

import inspect
import json
import re
import shutil
import subprocess

import pytest

from rectifi import forwarder_config
from rectifi.forwarder_config import JSON_TEMPLATE, TEXT_REGEX, build_forwarder_rules

URL = "https://prag-laptop.tail1a2b3.ts.net:8443/sms"
TOKEN = "rf_abc-DEF_123"
REQUIRED = ("sender", "url", "template", "headers")          # the app skips a rule without any of these, silently


def rules(url=URL, token=TOKEN, device_id=7, text_filter=TEXT_REGEX):
    return json.loads(build_forwarder_rules(url, token, device_id=device_id, text_filter=text_filter))


def test_the_file_is_an_array_with_exactly_one_rule():
    parsed = rules()
    assert isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict)


def test_the_output_is_pretty_printed():
    assert "\n  " in build_forwarder_rules(URL, TOKEN, device_id=1, text_filter=TEXT_REGEX)


def test_every_field_the_app_needs_is_there_and_a_string():
    rule = rules()[0]
    for key in REQUIRED:
        assert isinstance(rule[key], str) and rule[key], key
    assert rule["key"] == "rectifi-7"
    assert rule["sender"] == "*" and rule["is_sender_regex"] is False
    assert rule["url"] == URL
    assert rule["sms_filter"] == TEXT_REGEX
    assert rule["sim_slot"] == 0 and rule["retries_number"] == 10
    assert rule["ignore_ssl"] is False and rule["chunked_mode"] is False
    assert rule["is_sms_enabled"] is True and rule["store_failed"] is True and rule["local_mode"] is False
    assert rule["sign_hmac_sha256"] is False and rule["sign_hmac_sha256_secret"] == ""


def test_headers_is_a_json_object_encoded_as_a_string_with_the_bearer_token():
    rule = rules()[0]
    assert isinstance(rule["headers"], str)                       # not a nested object: the app would skip the rule
    headers = json.loads(rule["headers"])
    assert headers == {"User-agent": "SMS Forwarder App", "Authorization": f"Bearer {TOKEN}"}


def test_the_template_is_the_one_the_gateway_expects():
    template = rules()[0]["template"]
    assert template == JSON_TEMPLATE
    for placeholder in ("%from%", "%text%", "%sentStamp%"):
        assert placeholder in template
    assert '"sentStamp": %sentStamp%' in template                 # the stamp is a number, so it is not quoted


def test_the_key_is_per_device_so_re_importing_replaces_instead_of_duplicating():
    assert rules(device_id=3)[0]["key"] == "rectifi-3"
    assert rules(device_id=3)[0]["key"] == rules(device_id=3, token="rf_other")[0]["key"]
    assert rules(device_id=3)[0]["key"] != rules(device_id=4)[0]["key"]


def test_a_plain_http_lan_address_goes_through_unchanged():
    assert rules(url="http://192.168.1.9:8765/sms")[0]["url"] == "http://192.168.1.9:8765/sms"


def test_a_missing_url_or_token_is_refused_not_written():
    with pytest.raises(ValueError):
        build_forwarder_rules("", TOKEN, device_id=1, text_filter=TEXT_REGEX)
    with pytest.raises(ValueError):
        build_forwarder_rules(URL, "", device_id=1, text_filter=TEXT_REGEX)


def test_the_module_only_builds_a_string_it_never_touches_the_disk():
    source = inspect.getsource(forwarder_config)
    assert not re.search(r"\bopen\(|write_text|write_bytes|\.write\(|tempfile|import os\b", source)


# --- Java regex compatibility: the Android app uses java.util.regex, not Python's re ---

JAVA_GROUPS = re.compile(r"\(\?(?:[idmsuxU]+(?:-[idmsuxU]+)?[:)]|:|=|!|<=|<!|<[A-Za-z][A-Za-z0-9]*>|>)")


def java_incompatible(pattern: str) -> list[str]:
    """Every '(?...' group in the pattern that Java does not understand. Python-only: (?P<n>..), (?P=n), (?#..), (?a) (?L)."""
    return [m.group(0) for m in re.finditer(r"\(\?[^)]{0,3}", pattern) if not JAVA_GROUPS.match(pattern, m.start())]


def test_the_text_filter_uses_only_constructs_java_supports():
    assert java_incompatible(TEXT_REGEX) == []
    assert re.compile(TEXT_REGEX)                                  # and it is a valid regex in Python too


@pytest.mark.parametrize("bad", [r"(?P<name>x)", r"(?P=name)", r"(?#comment)x", r"(?a)x", r"(?L)x"])
def test_the_java_check_catches_python_only_syntax(bad):
    assert java_incompatible(bad)


@pytest.mark.parametrize("good", [r"(?i)(a|b)", r"(?:a)", r"(?=a)", r"(?!a)", r"(?<=a)b", r"(?<name>a)", r"(?i:a)"])
def test_the_java_check_accepts_java_syntax(good):
    assert java_incompatible(good) == []


def test_the_filter_matches_real_looking_bank_sms_and_not_an_otp():
    pattern = re.compile(TEXT_REGEX)
    assert pattern.search("Rs.500.00 debited from A/c XX1234 on 10-10-26")
    assert pattern.search("INR 1,200 credited to your account")
    assert pattern.search("Your OTP is 482913. Do not share it with anyone.") is None


@pytest.mark.skipif(shutil.which("java") is None, reason="no java on this machine")
def test_java_itself_compiles_and_runs_the_filter(tmp_path):
    """Where a JDK exists, ask java.util.regex directly (java Main.java, JDK 11+)."""
    src = tmp_path / "Main.java"
    src.write_text(
        "import java.util.regex.Pattern;\n"
        "public class Main { public static void main(String[] a) {\n"
        "  Pattern p = Pattern.compile(a[0]);\n"
        "  System.out.println(p.matcher(a[1]).find() + \",\" + p.matcher(a[2]).find());\n"
        "} }\n", encoding="utf-8")
    done = subprocess.run(["java", str(src), TEXT_REGEX, "Rs.500.00 DEBITED from A/c XX1234", "Your OTP is 482913"],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0 and "release version" in done.stderr + done.stdout:
        pytest.skip("this java cannot run a source file directly")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "true,false"
