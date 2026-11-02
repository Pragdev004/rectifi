"""The account pages in a real browser, in multi-user mode: sign up, recovery code, recover, Settings, Privacy, and what a friend sees.

Skipped when Playwright, Chromium or web/dist is missing. With RECTIFI_SCREENSHOTS=1 (and RECTIFI_SHOTS_DIR) it saves screenshots.
The server runs in a thread of this process with RECTIFI_MULTIUSER=1, so the tests can make invites and read the database.
Codes and passwords are chased everywhere the browser could keep them: the address, every request URL, localStorage, sessionStorage.
"""

import json
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import pytest
from test_web_e2e import CHECK_TARGETS
from web_support import LiveServer, browser, new_page, pinned_today, shot, skip_reason  # noqa: F401

from rectifi import account, db, users

SECRET = "e" * 48
PASSWORD = "correct horse battery"
NEW_PASSWORD = "another long passphrase"
WIDTHS = pytest.mark.parametrize("kind", ["phone", "desktop"])
OWNER_ONLY = ["Ask", "Charts", "Library", "Memory", "Traces"]


def tag(kind: str) -> str:
    return "390" if kind == "phone" else "1280"


def at(page, path: str) -> None:
    """Wait until the browser is on this path (wait_for_function would be refused by the page's CSP)."""
    page.wait_for_url(lambda url: urlparse(url).path == path)


def no_sideways(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "the page scrolls sideways"


@pytest.fixture
def mu_server(monkeypatch):
    """A multi-user server on a fresh test database (the autouse fixture in conftest has already cleared it)."""
    reason = skip_reason()
    if reason:
        pytest.skip(reason)
    from conftest import TEST_URL

    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", TEST_URL)
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", SECRET)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")             # no key: the model is off, and nothing here may call one
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    users.reset_limiters()
    server = LiveServer()
    server.start()
    yield server
    server.stop()
    users.reset_limiters()


def watch(page):
    """Record every request URL and every 403, so a test can say what the browser asked for."""
    seen = {"urls": [], "forbidden": []}
    page.on("request", lambda r: seen["urls"].append(r.url))
    page.on("response", lambda r: seen["forbidden"].append(r.url) if r.status == 403 else None)
    return seen


def invite(kind="friend") -> str:
    return users.create_invite(kind)["code"]


def fill_signup(page, code, username, password=PASSWORD):
    page.goto("/signup")
    page.get_by_label("Invite code", exact=True).fill(code)
    page.get_by_label("Username", exact=True).fill(username)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_label("Repeat password", exact=True).fill(password)
    page.get_by_role("button", name="Continue").click()
    page.wait_for_selector("[data-testid=recovery-gate]")


def gate_code(page) -> str:
    return page.locator("[data-testid=recovery-gate] code").inner_text().strip()


def finish_button(page, name):
    return page.get_by_role("button", name=name, exact=True)


def is_off(button) -> bool:
    return button.get_attribute("aria-disabled") == "true"


def sign_up_and_finish(page, username="asha", kind="friend") -> str:
    """The whole sign-up. Returns the recovery code."""
    fill_signup(page, invite(kind), username)
    code = gate_code(page)
    page.get_by_label("I saved it somewhere safe", exact=True).check()
    finish_button(page, "Finish sign-up").click()
    page.wait_for_selector("h1")
    at(page, "/")
    return code


def add_entry(page, payee="Chaayos", amount="120.00"):
    """One entry through the page's own fetch (same origin, same headers as the app)."""
    result = page.evaluate(
        """async ([payee, amount]) => {
          const r = await fetch('/api/transactions', {method: 'POST', credentials: 'same-origin',
            headers: {'X-Rectifi-Request': '1', 'Content-Type': 'application/json'},
            body: JSON.stringify({amount, direction: 'debit', date: '2026-10-20', payee, category: 'Food & Dining'})});
          return r.status;
        }""",
        [payee, amount],
    )
    assert result == 200


def nothing_secret_is_kept(page, seen, secrets):
    """No secret in the address, in any request URL, in localStorage, in sessionStorage or in a script-readable cookie."""
    stored = page.evaluate(
        "JSON.stringify({l: Object.entries(localStorage), s: Object.entries(sessionStorage), c: document.cookie})"
    )
    for secret in secrets:
        assert secret not in page.url, "a secret is in the address"
        assert not any(secret in url for url in seen["urls"]), "a secret was sent in a URL"
        assert secret not in stored, "a secret is in browser storage"


def user_row(username):
    with db.connect_system("test: read a user") as conn:
        return conn.execute("SELECT * FROM users WHERE lower(username) = lower(%s)", (username,)).fetchone()


# ───────────────────────── sign up, the gate, recover ─────────────────────────

@WIDTHS
def test_signup_gate_signout_recover_and_the_data_is_readable(browser, mu_server, kind):
    page, problems = new_page(browser, mu_server, kind)
    seen = watch(page)

    fill_signup(page, invite(), "asha")
    shot(page, f"account-signup-code-{tag(kind)}")
    code = gate_code(page)
    assert code and code not in page.url
    no_sideways(page)
    assert page.evaluate(CHECK_TARGETS) == []

    finish = finish_button(page, "Finish sign-up")
    assert is_off(finish), "Finish must wait for the download or the tick"
    finish.click(force=True)                                    # a click on a disabled button does nothing
    page.wait_for_timeout(300)
    assert page.locator("[data-testid=recovery-gate]").is_visible()
    row = user_row("asha")
    assert row is not None and row["confirmed_at"] is None, "the account must not exist for real before Finish"

    page.get_by_label("I saved it somewhere safe", exact=True).check()
    assert not is_off(finish)
    page.get_by_label("I saved it somewhere safe", exact=True).uncheck()
    assert is_off(finish), "unticking closes the gate again"
    page.get_by_label("I saved it somewhere safe", exact=True).check()
    finish.click()
    at(page, "/")
    page.wait_for_selector("[data-testid=hero-income], h1")
    assert user_row("asha")["confirmed_at"] is not None
    nothing_secret_is_kept(page, seen, [code, PASSWORD])

    add_entry(page)
    page.goto("/ledger")
    page.wait_for_selector("text=Chaayos")

    # sign out (the button is in the sidebar on a laptop and in the More sheet on a phone)
    if kind == "phone":
        page.get_by_role("button", name="More").click()
    page.get_by_role("button", name="Sign out").click()
    at(page, "/login")
    page.get_by_label("Username", exact=True).wait_for()
    shot(page, f"account-login-{tag(kind)}")
    assert page.get_by_role("link", name="Forgot my password").is_visible()

    # recover with the code: a new password, and a NEW code behind the same gate
    page.get_by_role("link", name="Forgot my password").click()
    at(page, "/recover")
    page.get_by_label("Username", exact=True).fill("asha")
    page.get_by_label("Recovery code", exact=True).fill(code)
    page.get_by_label("New password", exact=True).fill(NEW_PASSWORD)
    page.get_by_label("Repeat new password", exact=True).fill(NEW_PASSWORD)
    shot(page, f"account-recover-{tag(kind)}")
    page.get_by_role("button", name="Set a new password").click()
    page.wait_for_selector("[data-testid=recovery-gate]")
    fresh = gate_code(page)
    assert fresh and fresh != code, "the old code is spent; the page shows a new one"
    go = finish_button(page, "Go to sign in")
    assert is_off(go)
    page.get_by_label("I saved it somewhere safe", exact=True).check()
    go.click()
    at(page, "/login")

    page.get_by_label("Username", exact=True).fill("asha")
    page.get_by_label("Password", exact=True).fill(NEW_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    at(page, "/")
    page.goto("/ledger")
    page.wait_for_selector("text=Chaayos")                      # the sealed payee opens again: recovery restored the key

    nothing_secret_is_kept(page, seen, [code, fresh, PASSWORD, NEW_PASSWORD])
    assert problems == [], problems


def test_the_download_opens_the_gate_and_holds_the_code(browser, mu_server, tmp_path):
    page, _ = new_page(browser, mu_server, "phone")
    page.context.set_default_timeout(10000)
    fill_signup(page, invite(), "ravi")
    code = gate_code(page)
    finish = finish_button(page, "Finish sign-up")
    assert is_off(finish)
    with page.expect_download() as info:
        page.get_by_role("button", name="Download the code (.txt)").click()
    download = info.value
    assert download.suggested_filename == "rectifi-recovery-code.txt"
    saved = Path(download.path()).read_text(encoding="utf-8")
    assert code in saved and "ravi" in saved and "only way back" in saved
    assert not is_off(finish), "downloading opens the gate without the tick"
    finish.click()
    at(page, "/")


def test_a_wrong_invite_and_a_wrong_recovery_code_get_one_plain_sentence(browser, mu_server):
    page, _ = new_page(browser, mu_server, "desktop")
    page.goto("/signup")
    page.get_by_label("Invite code", exact=True).fill("NOT-A-REAL-CODE")
    page.get_by_label("Username", exact=True).fill("nobody")
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_label("Repeat password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Continue").click()
    page.get_by_role("alert").first.wait_for()
    assert page.locator("[data-testid=recovery-gate]").count() == 0
    assert user_row("nobody") is None

    page.goto("/recover")
    page.get_by_label("Username", exact=True).fill("nobody")
    page.get_by_label("Recovery code", exact=True).fill("WRONG")
    page.get_by_label("New password", exact=True).fill(NEW_PASSWORD)
    page.get_by_label("Repeat new password", exact=True).fill(NEW_PASSWORD)
    page.get_by_role("button", name="Set a new password").click()
    page.get_by_role("alert").first.wait_for()
    assert page.locator("[data-testid=recovery-gate]").count() == 0


def test_two_passwords_that_differ_are_caught_before_anything_is_sent(browser, mu_server):
    page, _ = new_page(browser, mu_server, "desktop")
    seen = watch(page)
    page.goto("/signup")
    page.get_by_label("Invite code", exact=True).fill(invite())
    page.get_by_label("Username", exact=True).fill("typo")
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_label("Repeat password", exact=True).fill(PASSWORD + "x")
    page.get_by_role("button", name="Continue").click()
    page.get_by_text("The two passwords are not the same.").wait_for()
    assert not any(url.endswith("/api/auth/signup") for url in seen["urls"])
    assert user_row("typo") is None


# ───────────────────────── who sees what ─────────────────────────

def nav_names(page, kind):
    if kind == "phone":
        page.get_by_role("button", name="More").click()
        names = page.locator("dialog[open] nav a").all_inner_texts()
        bottom = page.locator("nav[aria-label=Main] a").all_inner_texts()
        page.keyboard.press("Escape")
        return [n.strip() for n in names + bottom]
    return [n.strip() for n in page.locator("nav[aria-label=Main] a").all_inner_texts()]


@WIDTHS
def test_a_friend_sees_no_owner_pages_and_never_hits_a_403(browser, mu_server, kind):
    page, problems = new_page(browser, mu_server, kind)
    seen = watch(page)
    sign_up_and_finish(page, "asha")
    for path in ("/", "/insights", "/alerts", "/ledger", "/settings"):
        page.goto(path)
        page.wait_for_selector("h1")
    names = nav_names(page, kind)
    joined = " | ".join(names)
    for owner_page in OWNER_ONLY:
        assert not any(n.startswith(owner_page) for n in names), f"a friend sees {owner_page}: {joined}"
    for allowed in ("Ledger", "Settings"):
        assert any(n.startswith(allowed) for n in names), f"{allowed} is missing: {joined}"
    assert page.get_by_role("button", name="Explain this").count() == 0
    assert seen["forbidden"] == [], f"the friend's browser asked for something it may not have: {seen['forbidden']}"
    assert not any("/api/chat/" in url or "/api/models/" in url for url in seen["urls"])

    page.goto("/ask")                                           # typing the address by hand gets a plain sentence, not a wall of errors
    page.get_by_text("This page is for the owner.").wait_for()
    shot(page, f"account-friend-ask-{tag(kind)}")
    assert problems == [] or all("403" in p for p in problems), problems


def test_the_owner_sees_all_of_it(browser, mu_server):
    page, _ = new_page(browser, mu_server, "desktop")
    sign_up_and_finish(page, "prag", kind="owner")
    page.goto("/")
    page.wait_for_selector("h1")
    names = nav_names(page, "desktop")
    for owner_page in OWNER_ONLY + ["Settings", "Ledger"]:
        assert any(n.startswith(owner_page) for n in names), f"the owner is missing {owner_page}: {names}"


# ───────────────────────── Settings ─────────────────────────

def test_settings_change_password_new_code_export_and_delete(browser, mu_server):
    page, problems = new_page(browser, mu_server, "phone")
    page.context.set_default_timeout(12000)
    seen = watch(page)
    first_code = sign_up_and_finish(page, "asha")
    add_entry(page)
    page.goto("/settings")
    page.get_by_test_id("settings-username").wait_for()
    assert page.get_by_test_id("settings-username").inner_text() == "asha"
    shot(page, "account-settings-390")
    no_sideways(page)
    assert page.evaluate(CHECK_TARGETS) == []

    # change password: a wrong current password is refused in words, the right one works
    panel = page.locator("section[aria-labelledby=pw-h]")
    panel.get_by_label("Current password", exact=True).fill("not my password")
    panel.get_by_label("New password", exact=True).fill(NEW_PASSWORD)
    panel.get_by_label("Repeat new password", exact=True).fill(NEW_PASSWORD)
    panel.get_by_role("button", name="Change password").click()
    panel.get_by_role("alert").first.wait_for()
    panel.get_by_label("Current password", exact=True).fill(PASSWORD)
    panel.get_by_role("button", name="Change password").click()
    panel.get_by_text("Password changed.").wait_for()

    # a new recovery code: password required, same gate, the old code stops working
    box = page.locator("section[aria-labelledby=rc-h]")
    box.get_by_label("Your password", exact=True).fill(NEW_PASSWORD)
    box.get_by_role("button", name="Make a new recovery code").click()
    box.locator("[data-testid=recovery-gate]").wait_for()
    second_code = box.locator("code").inner_text().strip()
    assert second_code != first_code
    done = box.get_by_role("button", name="Done", exact=True)
    assert is_off(done)
    box.get_by_label("I saved it somewhere safe", exact=True).check()
    done.click()
    box.get_by_label("Your password", exact=True).wait_for()

    # export: a real ZIP arrives as a download
    with page.expect_download() as info:
        page.get_by_role("button", name="Download my data (.zip)").click()
    export = info.value
    assert export.suggested_filename.endswith(".zip")
    with zipfile.ZipFile(export.path()) as z:
        assert any(name.endswith("transactions.csv") for name in z.namelist())
        assert "120.00" in z.read(next(n for n in z.namelist() if n.endswith("transactions.csv"))).decode("utf-8")

    # delete: the button waits for the typed username, then the account is gone for good
    drop = page.get_by_role("button", name="Delete my account for good")
    assert is_off(drop)
    danger = page.locator("section[aria-labelledby=del-h]")
    danger.get_by_label("Your password", exact=True).fill(NEW_PASSWORD)
    danger.get_by_label("Type your username (asha) to confirm", exact=True).fill("ash")
    assert is_off(drop)
    danger.get_by_label("Type your username (asha) to confirm", exact=True).fill("asha")
    assert not is_off(drop)
    drop.click()
    at(page, "/login")
    assert user_row("asha") is None
    nothing_secret_is_kept(page, seen, [first_code, second_code, PASSWORD, NEW_PASSWORD])
    assert all("401" in p for p in problems), problems          # only the wrong current password on purpose


# ───────────────────────── Privacy (public) ─────────────────────────

@WIDTHS
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_privacy_is_reachable_signed_out_and_says_what_the_registry_says(browser, mu_server, kind, scheme):
    page, problems = new_page(browser, mu_server, kind, scheme)
    seen = watch(page)
    page.goto("/privacy")
    page.wait_for_selector("[data-testid=privacy-facts]")
    assert page.url.endswith("/privacy"), "the Privacy page must not send a signed-out visitor to sign in"
    shot(page, f"account-privacy-{tag(kind)}-{scheme}")
    no_sideways(page)
    assert page.evaluate(CHECK_TARGETS) == []
    asked = {url.split("?")[0].rsplit("/api", 1)[1] for url in seen["urls"] if "/api/" in url}
    assert asked <= {"/privacy", "/auth/status"}, f"the Privacy page asked for private data: {asked}"

    facts = account.privacy_facts()
    api_facts = page.evaluate("fetch('/api/privacy', {headers: {'X-Rectifi-Request': '1'}}).then(r => r.json())")
    assert api_facts == json.loads(json.dumps(facts)), "the public route says what the registry says"
    assert facts["mode"] == "multiuser" and facts["encryption_at_rest"] is True

    text = page.inner_text("[data-testid=privacy-facts]")
    for name in facts["sealed_fields"] + facts["keyed_hash_fields"] + facts["plain_fields"] + facts["unsealed_owner_tables"]:
        assert name in text, f"the Privacy page leaves out {name}"
    for brand in facts["merchant_allowlist"]:
        assert brand["name"] in text, f"the Privacy page leaves out {brand['name']}"
    for row in facts["who_can_read"]:
        for part in (row["who"], row["plain"], row["sealed"]):
            assert part in text
    for line in facts["limits"] + [facts["recovery"]]:
        assert re.sub(r"\s+", " ", line) in re.sub(r"\s+", " ", text), f"the Privacy page leaves out: {line}"
    assert f"{facts['backup_days']} days" in text
    assert ("on this server's own machine" if facts["model_provider"] == "local" else "in the cloud") in text
    assert problems == [], problems


@WIDTHS
def test_the_sign_in_and_sign_up_screens_fit_the_phone_and_the_laptop(browser, mu_server, kind):
    page, problems = new_page(browser, mu_server, kind)
    for path in ("/login", "/signup", "/recover"):
        page.goto(path)
        page.wait_for_selector("h1")
        page.wait_for_selector("form")
        no_sideways(page)
        assert page.evaluate(CHECK_TARGETS) == [], path
    shot(page, f"account-recover-form-{tag(kind)}")
    assert problems == [], problems
