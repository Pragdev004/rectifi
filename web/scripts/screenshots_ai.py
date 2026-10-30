"""Development only: screenshots of the AI-phase pages, at 390 px and 1280 px, light and dark, into /tmp/rectifi-ai-shots/.

    python web/scripts/screenshots_ai.py              # Add (import result with categories), Ledger, Charts, Library, Ask, Memory, Traces, Login
    python web/scripts/screenshots_ai.py ledger ask   # only pages whose name contains one of these words

Needs web/dist, Playwright with Chromium, and Postgres. It makes one scratch database next to your real one (rectifi_aishots_demo),
fills it with scripts/seed_demo.py, and drops it at the end. Every model is a scripted fake and the embeddings are a toy, so the
pictures show the layout, never the quality of a real Ollama. Nothing here ships: the app does not import this file.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")]

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

OUT = Path("/tmp/rectifi-ai-shots")
TODAY = date(2026, 10, 21)
SCRATCH = "rectifi_aishots_demo"

STATEMENT = """HDFC BANK Ltd.,,,,,,
Statement of account,,,,,,
Account No : XXXXXXXX4417,,,,,,
,,,,,,
Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance
02/10/26,UPI-SWIGGY-swiggy@icici-ICIC0000103-429183746521-PAYMENT,0000429183746521,02/10/26,312.00,,40000.00
03/10/26,UPI-MURUGAN AGENCIES-murugan@sbi-SBIN0001234-429183746522-PAY,0000429183746522,03/10/26,640.00,,39360.00
04/10/26,UPI-KANNAN STORES-kannan@okaxis-UTIB0000100-429183746523-PAY,0000429183746523,04/10/26,486.50,,38873.50
05/10/26,UPI-UBER INDIA-uber@axisbank-UTIB0000100-429900112233-RIDE,0000429900112233,05/10/26,218.00,,38655.50
06/10/26,UPI-BLUE LOTUS-bluelotus@ybl-YESB0000001-429183746524-PAY,0000429183746524,06/10/26,780.00,,37875.50
07/10/26,UPI-RAHUL SHARMA-rahul@okhdfc-HDFC0000001-429183746525-PAY,0000429183746525,07/10/26,1500.00,,36375.50
08/10/26,ATM CASH WITHDRAWAL CHENNAI,ATM0811,08/10/26,2000.00,,34375.50
09/10/26,POS 416021XXXXXX4417 DMART AVENUE CHENNAI,POS0912,09/10/26,1325.00,,33050.50
10/10/26,UPI-PRIYA R-priya@oksbi-SBIN0001234-429183746526-PAY,0000429183746526,10/10/26,90.00,,32960.50
11/10/26,UPI-NETFLIX-netflix@hdfcbank-HDFC0000001-430012345678-SUBSCRIPTION,0000430012345678,11/10/26,199.00,,32761.50
12/10/26,UPI-APOLLO PHARMACY-apollo@icici-ICIC0000103-430012345679-PAY,0000430012345679,12/10/26,420.00,,32341.50
13/10/26,NEFT CR-SBIN0001234-IITM ALLOWANCE-STIPEND OCT,N123456789,13/10/26,,8000.00,40341.50
"""
CODE_MONTHLY = '''import matplotlib.pyplot as plt

food = df[(df.direction == "debit") & (df.category == "Food & Dining")]
paise = food.groupby("month")["amount_paise"].sum()
fig, ax = plt.subplots(figsize=(10, 6))
ax.bar(paise.index, paise.values / 100, color=PALETTE["accent"])
ax.bar_label(ax.containers[0], labels=[inr(p) for p in paise.values], padding=3)
ax.set_title("Food & Dining spend by month")
ax.set_xlabel("Month")
ax.set_ylabel("Rupees")
'''
CODE_CATEGORIES = '''import matplotlib.pyplot as plt

spend = df[df.direction == "debit"]
paise = spend.groupby("category")["amount_paise"].sum().sort_values()
fig, ax = plt.subplots(figsize=(10, 6))
ax.barh(paise.index, paise.values / 100, color=PALETTE["accent"])
ax.bar_label(ax.containers[0], labels=[inr(p) for p in paise.values], padding=3)
ax.set_title("Spending by category")
ax.set_xlabel("Rupees")
ax.set_ylabel("Category")
'''
ANSWER = "Keep three to six months of basic expenses aside, in an account you can reach the same day [1]. Start with one month and build up [2]."
NOTE = "# My own rules\n\nI keep one month of rent and mess fees in the savings account before I put anything into a SIP.\n"


def base_url() -> str:
    from conftest import TEST_URL

    return TEST_URL


def with_db(url: str, name: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(url)
    info["dbname"] = name
    return psycopg.conninfo.make_conninfo(**info)


def admin(sql: str) -> None:
    with psycopg.connect(with_db(base_url(), "postgres"), autocommit=True) as c:
        c.execute(sql)


def install_fakes() -> None:
    """Scripted models for every role, toy embeddings, and a model status that says 'ready'."""
    from langchain_core.messages import AIMessage
    from rag_support import RuleModel
    from support.fakes import BowEmbeddings, ScriptedModel
    from test_agent import call

    import rectifi.agent.graph as agent_graph
    import rectifi.agent.llm as llm

    os.environ["LLM_PROVIDER"] = "deepseek"
    os.environ["DEEPSEEK_API_KEY"] = "sk-shots"
    os.environ["RECTIFI_EMBED_MODEL"] = "bow"
    os.environ["RECTIFI_NO_AUTO_REINDEX"] = "1"

    def label(prompt: str) -> dict:
        out = []
        for line in prompt.splitlines():
            head = line.split(".", 1)[0]
            if not head.strip().isdigit() or " | payee:" not in line:
                continue
            payee = line.split("payee:", 1)[1].split("|")[0].strip().lower()
            if "murugan" in payee:
                out.append((int(head), "Shopping", 0.91))
            elif "kannan" in payee:
                out.append((int(head), "Groceries", 0.88))
            elif "blue lotus" in payee:
                out.append((int(head), "Food & Dining", 0.64))
            else:
                out.append((int(head), "Miscellaneous", 0.2))
        return {"labels": [{"i": i, "category": c, "confidence": p, "why": "name looks like a shop"} for i, c, p in out]}

    classify = RuleModel(structured={"BatchLabels": label})
    grade = RuleModel(
        structured={"DocGrade": lambda t: {"relevance": "yes", "reason": "about emergency funds"},
                    "GroundCheck": lambda t: {"supported": "fully", "unsupported_claims": []},
                    "UseCheck": lambda t: {"useful": True, "score": 5},
                    "RouteDecision": lambda t: {"route": "knowledge", "reason": "a guide question"},
                    "QueryList": lambda t: {"queries": ["emergency fund months"]}},
        write=lambda t: "emergency fund size in months")
    chat = RuleModel(write=lambda t: ANSWER)
    codes = ScriptedModel(script=[AIMessage(f"Here is the chart.\n```python\n{CODE_CATEGORIES}```"), AIMessage(f"Here is the chart.\n```python\n{CODE_MONTHLY}```"),
                                  AIMessage(f"Here is the chart.\n```python\n{CODE_MONTHLY}```")])
    agent = ScriptedModel(script=[call("check_budgets", {"month": "this_month"}),
                                  AIMessage("Shopping is over its limit this month. Food & Dining is still inside its limit."),
                                  AIMessage("Done.")] * 3)
    models = {"chat": chat, "classify": classify, "code": codes, "grade": grade}
    llm.get_llm = lambda role="chat": models[role]
    agent_graph.get_llm = lambda role="chat": agent
    embeddings = BowEmbeddings()
    llm.get_embeddings = lambda: embeddings
    return embeddings


def prepare(embeddings) -> None:
    """Everything the pages show that the plain demo seed does not: charts, library, a chat, memory."""
    import seed_demo
    from api_support import make_client

    from rectifi import config, db, tools
    from rectifi.agent import memory as mem
    from rectifi.agent.checkpoint import get_memory
    from rectifi.rag import index

    seed_demo.seed(TODAY)
    tools.add_account("HDFC Savings", "4417")
    index.reindex(embeddings, embed_model=config.embed_model())
    client = make_client()
    client.post("/api/knowledge/docs", files={"file": ("my-rules.md", NOTE.encode(), "text/markdown")})
    index.reindex(embeddings, embed_model=config.embed_model())
    for request, period in (("spending by category, last 3 months", "last-3-months"), ("food spending by month", "last-3-months")):
        done = client.post("/api/charts/draw", json={"request": request, "period": period, "review": False})
        assert done.status_code == 200, done.text
    first = tools.list_charts(limit=5)
    if first:
        client.post(f"/api/charts/{first[-1]['id']}/pin", json={"pinned": True})
    store = get_memory().store
    for kind, text in (("preference", "I prefer short answers"), ("fact", "I am a second-year student and my rent is paid by my parents"),
                       ("correction", "Cult Fit is Health, not Entertainment")):
        mem.add_memory(store, "owner", kind, text)
    thread = client.post("/api/chat/threads", json={}).json()["thread_id"]
    OPEN_THREAD.append(thread)                      # the Ask page opens the thread whose id is in localStorage
    for text, extra in (("am I over budget anywhere?", {}), ("what is an emergency fund, and how many months should it cover?", {"mode": "full"}),
                        ("/chart food spending by month", {})):
        r = client.post(f"/api/chat/threads/{thread}/messages", json={"text": text, **extra})
        assert r.status_code == 200 and '"type": "error"' not in r.text, r.text[:400]
    client.post("/api/chat/threads", json={"title": "Rent and mess fees"})
    db.close_pool() if False else None


OPEN_THREAD: list[str] = []


def _forget_import() -> None:
    """Put the scratch database back to before the statement was imported, so each screenshot shows a fresh import."""
    from rectifi.api import uploads
    from rectifi.db import connect

    uploads.reset()
    with connect() as conn:
        conn.execute("DELETE FROM transactions WHERE import_batch_id IS NOT NULL")
        conn.execute("DELETE FROM import_batches")


def main(words: list[str]) -> int:
    from playwright.sync_api import sync_playwright
    from web_support import DESKTOP, PHONE, LiveServer

    admin(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)')
    admin(f'CREATE DATABASE "{SCRATCH}"')
    OUT.mkdir(parents=True, exist_ok=True)
    statement = Path(tempfile.mkdtemp()) / "hdfc-october.csv"
    statement.write_text(STATEMENT, encoding="utf-8")
    try:
        from rectifi import db, tools

        tools.today = lambda: TODAY
        os.environ["DATABASE_URL"] = with_db(base_url(), SCRATCH)
        db.close_pool()
        db.init_db()
        embeddings = install_fakes()
        prepare(embeddings)
        from rectifi.api import ask

        ask.reset()
        server = LiveServer()
        server.start()
        login_server = None
        pages = {
            "add-import": "/add?tab=import", "ledger": "/ledger", "ledger-model": "/ledger?source=llm", "charts": "/charts",
            "library": "/library", "ask": "/ask", "memory": "/memory", "traces": "/traces",
        }
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--proxy-server=direct://"])
                for width, size in (("1280", DESKTOP), ("390", PHONE)):
                    for scheme in ("light", "dark"):
                        for name, path in pages.items():
                            if words and not any(w in name for w in words):
                                continue
                            context = browser.new_context(color_scheme=scheme, base_url=server.url, **size)
                            context.set_default_timeout(15000)
                            if OPEN_THREAD:
                                context.add_init_script(f"try {{ localStorage.setItem('rectifi.chat.thread', '{OPEN_THREAD[0]}') }} catch (e) {{}}")
                            page = context.new_page()
                            page.goto(path)
                            if name == "add-import":
                                _forget_import()                  # every shot is a first import of the file
                                page.set_input_files("input[type=file]", str(statement))
                                page.wait_for_selector("[data-testid=import-slip]")
                                page.get_by_text("by the local model", exact=False).first.wait_for()
                            else:
                                page.wait_for_selector("main h1, main h2")
                            if name == "ask":
                                page.wait_for_selector("[data-role=assistant]")
                            if name == "traces":
                                page.get_by_text("chat · Looked it up").first.click()      # open one run so the step tree shows
                                page.wait_for_timeout(500)
                            page.evaluate("document.fonts.ready")
                            page.wait_for_timeout(900)
                            shot(page, size, OUT / f"{name}-{width}-{scheme}.png")
                            context.close()
                    # the sign-in page needs a server with a password set
                if not words or any(w in "login" for w in words):
                    from rectifi.api.auth import hash_password

                    os.environ["RECTIFI_PASSWORD_HASH"] = hash_password("shots-password")
                    os.environ["RECTIFI_SESSION_SECRET"] = "s" * 48
                    login_server = LiveServer()
                    login_server.start()
                    for width, size in (("1280", DESKTOP), ("390", PHONE)):
                        for scheme in ("light", "dark"):
                            context = browser.new_context(color_scheme=scheme, base_url=login_server.url, **size)
                            page = context.new_page()
                            page.goto("/ledger")                      # not signed in: the app sends you to /login?next=...
                            page.wait_for_selector("form")
                            page.wait_for_timeout(600)
                            shot(page, size, OUT / f"login-{width}-{scheme}.png")
                            context.close()
                browser.close()
        finally:
            server.stop()
            if login_server:
                login_server.stop()
    finally:
        db.close_pool()
        admin(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)')
    return 0


def shot(page, size, out: Path) -> None:
    """Stretch the viewport to the whole page (so fixed bars sit at the bottom) and save it."""
    height = page.evaluate("Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)")
    page.set_viewport_size({"width": size["viewport"]["width"], "height": min(max(size["viewport"]["height"], height), 4200)})
    page.wait_for_timeout(250)
    page.screenshot(path=str(out))
    print("saved", out)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
