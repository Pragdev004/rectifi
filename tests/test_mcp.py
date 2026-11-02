"""The MCP server is the second front door. It must stay read-only, mask numbers, and print percentages as percentages."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

from rectifi import tools
from rectifi.mcp_server import server

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = {"spending_summary", "budget_status", "recent_transactions", "list_accounts", "reconciliation"}


def _run(coro):
    return asyncio.run(coro)


async def _call(name, args=None):
    async with Client(server) as client:
        result = await client.call_tool(name, args or {})
    return result.content[0].text


async def _list():
    async with Client(server) as client:
        return (await client.list_tools()).tools


def test_every_tool_is_read_only():
    listed = _run(_list())
    names = {t.name for t in listed}
    assert EXPECTED_TOOLS <= names
    for t in listed:
        assert t.annotations is not None, t.name
        assert t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False, t.name
        assert t.annotations.idempotent_hint is True, t.name
        assert t.annotations.open_world_hint is False, t.name
        assert not t.name.startswith(("add_", "delete_", "set_", "log_", "update_", "save_")), t.name


def test_percentages_are_not_rupees():
    tools.add_transaction("1000", "credit", category="Salary & Allowance")
    tools.set_budget("Food & Dining", "1000")
    tools.add_transaction("275", "debit", category="Food & Dining")

    summary = json.loads(_run(_call("spending_summary")))
    assert summary["savings_rate"] == "72.5%"
    assert summary["by_category"][0]["share"] == "100.0%"
    assert summary["spending"] == "₹275.00"           # money is still rupees

    budget = json.loads(_run(_call("budget_status")))
    food = budget["categories"][0]
    assert food["percent"] == "27.5%"
    assert budget["pace"].endswith("%") and "₹" not in budget["pace"]
    assert food["spent"] == "₹275.00" and food["limit"] == "₹1,000.00"


def test_card_numbers_are_masked():
    tools.add_transaction("300", "debit", payee="POS 416021XXXXXX4417 DMART", source="statement")
    tools.add_transaction("120", "debit", payee="ref 429183746521 chai", source="statement")
    text = _run(_call("recent_transactions"))
    assert "416021XXXXXX4417" not in text and "429183746521" not in text
    assert "4417" in text and "6521" in text


def test_list_accounts():
    tools.add_account("SBI Savings", "1234")
    accounts = json.loads(_run(_call("list_accounts")))
    assert accounts[0]["name"] == "SBI Savings" and accounts[0]["last4"] == "1234"


def test_stdio_server_starts_and_lists_tools():
    async def go():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "rectifi.mcp_server"], cwd=str(ROOT),
            env={"DATABASE_URL": os.environ["DATABASE_URL"], "PYTHONUTF8": "1"},  # URL set by fresh_db
        )
        async with Client(params) as client:
            return {t.name for t in (await client.list_tools()).tools}

    names = _run(go())
    assert len(names) >= 5 and EXPECTED_TOOLS <= names


def test_weekly_review_tool_is_read_only_and_formats_numbers():
    tools.add_transaction("5000", "credit", category="Salary & Allowance")
    tools.add_transaction("1250", "debit", payee="Amazon", source="chat")
    tools.save_commitment("Cook at home")
    review = json.loads(_run(_call("weekly_review")))
    assert review["spending"] == "₹1,250.00" and review["savings_rate"] == "75.0%"
    assert review["this_weeks_commitment"] == "Cook at home"
    assert "weekly_review" in {t.name for t in _run(_list())}


# ── multi-user mode (plan D1): the MCP server refuses, laptop mode runs as before ──

def test_main_refuses_in_multiuser_mode_without_touching_stdout(monkeypatch, capsys):
    import rectifi.mcp_server as mcp_server

    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    started = []
    monkeypatch.setattr(mcp_server.server, "run", lambda *a, **k: started.append(a))
    assert mcp_server.main() == 2
    out = capsys.readouterr()
    assert out.out == "" and "laptop mode" in out.err and "RECTIFI_MULTIUSER" in out.err
    assert started == []


def test_main_serves_over_stdio_in_laptop_mode(monkeypatch, capsys):
    import rectifi.mcp_server as mcp_server

    monkeypatch.delenv("RECTIFI_MULTIUSER", raising=False)
    started = []
    monkeypatch.setattr(mcp_server.server, "run", lambda *a, **k: started.append(a))
    assert mcp_server.main() == 0
    assert started == [("stdio",)]
    assert capsys.readouterr().out == ""


def test_the_module_refuses_as_a_real_process_in_multiuser_mode():
    import subprocess

    env = {**os.environ, "RECTIFI_MULTIUSER": "1"}
    done = subprocess.run([sys.executable, "-m", "rectifi.mcp_server"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and done.stdout == ""
    assert "laptop mode" in done.stderr and "Traceback" not in done.stderr
