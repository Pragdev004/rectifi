"""Helpers for the browser (Playwright) tests of the web app. Not a test file.

The server runs in a thread of the test process, so tests can pin the clock (`tools.today`), script the model, and read the
database directly to prove what the browser did. The browser tests skip themselves when Playwright, Chromium or web/dist is missing.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "web" / "dist"
SHOTS = Path("/tmp/rectifi-web-shots")
PINNED_TODAY = date(2026, 10, 21)          # a Wednesday, day 21 of 31

PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "has_touch": True, "is_mobile": True}
DESKTOP = {"viewport": {"width": 1280, "height": 800}, "device_scale_factor": 1}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self) -> None:
        import uvicorn

        from rectifi.api.app import create_app

        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(create_app(), host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("the test server did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def skip_reason() -> str | None:
    if not (DIST / "index.html").is_file():
        return "web/dist is not built (python scripts/dev.py build-web)"
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return "Playwright is not installed"
    return None


@pytest.fixture(scope="module")
def live_server():
    reason = skip_reason()
    if reason:
        pytest.skip(reason)
    from conftest import TEST_URL

    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", TEST_URL)       # the module-wide server must never see a real database
    server = LiveServer()
    server.start()
    yield server
    server.stop()
    patch.undo()


@pytest.fixture(scope="module")
def browser():
    reason = skip_reason()
    if reason:
        pytest.skip(reason)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            b = p.chromium.launch(args=["--proxy-server=direct://"])
        except Exception as exc:                 # noqa: BLE001 (no browser installed: skip, don't fail)
            pytest.skip(f"Chromium could not start: {exc}")
        yield b
        b.close()


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    from rectifi import tools

    monkeypatch.setattr(tools, "today", lambda: PINNED_TODAY)


def new_page(browser, server, kind: str = "desktop", scheme: str = "light", reduced_motion: str = "no-preference"):
    """A fresh browser context. Returns (page, problems) where `problems` collects console errors and page errors."""
    options = dict(PHONE if kind == "phone" else DESKTOP)
    context = browser.new_context(color_scheme=scheme, reduced_motion=reduced_motion, base_url=server.url, **options)
    context.set_default_timeout(8000)
    page = context.new_page()
    problems: list[str] = []
    page.on("console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    return page, problems


def shot(page, name: str, tall: bool = True) -> None:
    """Save a screenshot when RECTIFI_SCREENSHOTS is set (they are for people to look at, not assertions).
    With `tall` the viewport is stretched to the whole page first, so the shot shows everything (fixed bars sit at the bottom)."""
    if not os.getenv("RECTIFI_SCREENSHOTS"):
        return
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(250)                    # let the fonts and the last transition settle
    size = page.viewport_size
    if tall and size:
        height = page.evaluate("Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)")
        page.set_viewport_size({"width": size["width"], "height": max(size["height"], min(int(height), 3600))})
        page.wait_for_timeout(150)
    page.screenshot(path=str(SHOTS / f"{name}.png"))
    if tall and size:
        page.set_viewport_size(size)


def seed_month() -> None:
    """A month with money in and out, three budgets (one near, one over), and an account with two balances that balance."""
    from rectifi import tools

    tools.add_account("SBI Savings", "4417")
    tools.record_balance("SBI Savings", date(2026, 9, 30), "5000")
    rows = [
        ("credit", "9000", date(2026, 10, 1), "IITM stipend", "Salary & Allowance"),
        ("debit", "420", date(2026, 10, 3), "Swiggy", "Food & Dining"),
        ("debit", "1450", date(2026, 10, 5), "Amazon", "Shopping"),
        ("debit", "310", date(2026, 10, 9), "Uber", "Transport"),
        ("debit", "250", date(2026, 10, 14), "Chai point", "Food & Dining"),
        ("debit", "2399", date(2026, 10, 18), "Campus canteen", "Food & Dining"),
        ("debit", "640", date(2026, 10, 20), "Blinkit", "Groceries"),
    ]
    moved = 0
    for direction, amount, when, payee, category in rows:
        tools.add_transaction(amount, direction, when, payee=payee, category=category, account="SBI Savings", source="manual")
        moved += int(amount) * (1 if direction == "credit" else -1)
    tools.record_balance("SBI Savings", date(2026, 10, 20), str(5000 + moved))
    tools.set_budget("Food & Dining", "3000", "2026-10")
    tools.set_budget("Shopping", "1500", "2026-10")
    tools.set_budget("Transport", "1200", "2026-10")
