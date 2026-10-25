"""Import boundaries (CLAUDE.md rules 5, 8, 10, 13): the deterministic modules never load a model library, and importing
rectifi.api never loads langchain. Each check runs in a fresh interpreter, so what other tests imported cannot hide a leak."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PURE = ["rectifi.categorize", "rectifi.textvec", "rectifi.charts.validate", "rectifi.charts.sandbox", "rectifi.charts._child",
        "rectifi.importer", "rectifi.insights", "rectifi.pdf_statement", "rectifi.sms_gateway", "rectifi.tools", "rectifi.db"]
FORBIDDEN = ("langchain", "langchain_core", "langchain_ollama", "langchain_openai", "langchain_deepseek", "langgraph", "ollama",
             "openai", "rectifi.agent", "rectifi.rag", "rectifi.charts.graph")


def exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def leaked(module: str) -> list[str]:
    code = (
        "import sys, importlib\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"importlib.import_module({module!r})\n"
        f"bad = sorted(m for m in sys.modules if any(m == f or m.startswith(f + '.') for f in {FORBIDDEN!r}))\n"
        "print('\\n'.join(bad))\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line]


@pytest.mark.parametrize("module", PURE)
def test_pure_modules_load_no_model_library(module):
    if not exists(module):
        pytest.skip(f"{module} is not written yet")           # a later workstream adds it; the check then switches on by itself
    assert leaked(module) == []


@pytest.mark.parametrize("module", ["rectifi.api", "rectifi.api.app"])
def test_the_api_loads_no_model_library(module):
    assert leaked(module) == []


def test_the_empty_route_modules_exist_and_are_listed():
    from rectifi.api import app

    for name in ("classify", "charts", "chat", "memory", "traces", "models", "auth", "knowledge"):
        module = importlib.import_module(f"rectifi.api.routes.{name}")
        assert isinstance(module.routes, list)
    assert len(app.api_routes()) > 0


def test_the_agent_modules_are_allowed_to_load_langchain():
    # the opposite check: the harness itself must still be importable (a typo in contracts/checkpoint/tracing would show here)
    for name in ("rectifi.agent.contracts", "rectifi.agent.checkpoint", "rectifi.agent.tracing", "rectifi.agent.llm"):
        importlib.import_module(name)
