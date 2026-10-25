"""Development only: screenshots of every page, at 390 px and 1280 px, light and dark, for an empty database and a demo one.

    python web/scripts/screenshots.py              # everything, into /tmp/rectifi-web-shots/
    python web/scripts/screenshots.py ledger add   # only pages whose name contains one of these words

It builds two scratch databases next to your real one (rectifi_shots_demo, rectifi_shots_empty_demo), fills the first with
scripts/seed_demo.py, drops both when it is done, and needs web/dist (python scripts/dev.py build-web) and Playwright.
Nothing here ships: the app does not import this file.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")]

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

TODAY = date(2026, 10, 21)
PAGES = {
    "overview": "/", "review": "/review", "insights": "/insights", "add": "/add", "add-sms": "/add?tab=sms",
    "add-import": "/add?tab=import", "ledger": "/ledger", "budgets": "/budgets", "accounts": "/accounts",
    "phone": "/phone", "ask": "/ask",
}


def base_url() -> str:
    from conftest import TEST_URL            # the same server as the tests, so no extra configuration

    return TEST_URL


def with_db(url: str, name: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(url)
    info["dbname"] = name
    return psycopg.conninfo.make_conninfo(**info)


def admin(sql: str) -> None:
    with psycopg.connect(with_db(base_url(), "postgres"), autocommit=True) as c:
        c.execute(sql)


def main(words: list[str]) -> int:
    from playwright.sync_api import sync_playwright

    from web_support import DESKTOP, PHONE, SHOTS, LiveServer

    scratch = {"demo": "rectifi_shots_demo", "empty": "rectifi_shots_empty_demo"}
    for name in scratch.values():
        admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin(f'CREATE DATABASE "{name}"')
    SHOTS.mkdir(parents=True, exist_ok=True)
    try:
        from rectifi import db, tools

        tools.today = lambda: TODAY
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--proxy-server=direct://"])
            for kind, name in scratch.items():
                os.environ["DATABASE_URL"] = with_db(base_url(), name)
                db.close_pool()
                db.init_db()
                if kind == "demo":
                    import seed_demo

                    seed_demo.seed(TODAY)
                    seed_extras()
                    scripted_assistant()
                server = LiveServer()
                server.start()
                try:
                    for page_name, path in PAGES.items():
                        if words and not any(w in page_name for w in words):
                            continue
                        for width, size in (("390", PHONE), ("1280", DESKTOP)):
                            for scheme in ("light", "dark"):
                                context = browser.new_context(color_scheme=scheme, base_url=server.url, **size)
                                context.set_default_timeout(8000)
                                page = context.new_page()
                                page.goto(path)
                                page.wait_for_selector("main h1, main h2")
                                page.evaluate("document.fonts.ready")
                                page.wait_for_timeout(700)
                                shot_tall(page, None, size)
                                out = SHOTS / f"{kind}-{page_name}-{width}-{scheme}.png"
                                page.screenshot(path=str(out))
                                print("saved", out)
                                if page_name == "ask" and kind == "demo":
                                    page.get_by_role("textbox", name="Message Rectifi").fill("am I over budget anywhere?")
                                    page.keyboard.press("Enter")
                                    page.wait_for_selector("[data-role=assistant]")
                                    page.get_by_text("What Rectifi did").click()
                                    page.wait_for_timeout(600)
                                    shot_tall(page, SHOTS / f"{kind}-ask-reply-{width}-{scheme}.png", size)
                                context.close()
                finally:
                    server.stop()
            browser.close()
    finally:
        from rectifi import db

        db.close_pool()
        for name in scratch.values():
            admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    return 0


def shot_tall(page, out, size) -> None:
    """Stretch the viewport to the whole page (so fixed bars sit at the bottom), and save when `out` is given."""
    height = page.evaluate("Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)")
    page.set_viewport_size({"width": size["viewport"]["width"], "height": min(max(size["viewport"]["height"], height), 4200)})
    page.wait_for_timeout(250)
    if out is not None:
        page.screenshot(path=str(out))
        print("saved", out)


def seed_extras() -> None:
    """Things the demo seed does not make but some pages show: last month's budgets, a phone, a text it could not read,
    last week's commitment."""
    from rectifi import tools

    tools.set_budget("Food & Dining", "3500", "2026-09")
    tools.save_commitment("No more than 3 delivery orders", date(2026, 10, 12))
    device = tools.create_device("Pixel 8")
    tools._to_inbox(device["id"], "AX-BANK", "Rs.640.00 debited from A/c XX4417 on 20-10-26 to Mystery Shop UPI Ref No 429183746599", None, "shots-1", "demo")
    tools.create_device("Old tablet")
    tools.revoke_device(tools.list_devices()[-1]["id"])


def scripted_assistant() -> None:
    """The Ask page needs a model. Use the test suite's scripted one: a read-only tool call, a reply, then a reply with a figure
    nobody gave it (so the "Not verified" row shows). One pair per browser context below."""
    from langchain_core.messages import AIMessage
    from test_agent import ScriptedModel, call

    os.environ["LLM_PROVIDER"] = "deepseek"
    os.environ["DEEPSEEK_API_KEY"] = "sk-shots"
    script = []
    for _ in range(4):
        script += [call("check_budgets", {"month": "this_month"}), AIMessage("Shopping is **over** its limit this month. Food & Dining is fine: about ₹2,178 is left. You also spent ₹77,777 on travel.")]
    scripted = ScriptedModel(script=script)
    import rectifi.agent.graph as graph

    graph.get_llm = lambda: scripted


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
