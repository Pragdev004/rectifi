"""scripts/setup_windows.ps1 cannot be run on Linux CI, so these tests check the things that matter by reading it.

They are a safety net, not a replacement for running it on Windows. If pwsh (PowerShell 7) is installed,
the script is also parsed with the real PowerShell parser.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "setup_windows.ps1"
TEXT = SCRIPT.read_text(encoding="utf-8")


def code_only() -> str:
    """The script without comment lines, so a word in a comment cannot fake a pass."""
    return "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))


def test_script_is_plain_ascii():
    # Windows PowerShell 5.1 reads a file with no BOM as ANSI, so one curly quote or dash can break parsing.
    bad = [(n, line) for n, line in enumerate(TEXT.splitlines(), 1) if any(ord(c) > 127 for c in line)]
    assert not bad, f"Non-ASCII characters in {SCRIPT.name}: {bad[:3]}"


def test_passwords_are_read_as_secure_strings_and_never_echoed():
    code = code_only()
    assert code.count("-AsSecureString") >= 1
    assert re.search(r"Read-Host[^\n]*-AsSecureString", code)
    # nothing may print a variable that holds a secret
    for secret in ("postgresPassword", "$key", "$token", "$plain"):
        assert not re.search(rf"Write-(Host|Ok|Bad|Info)[^\n]*{re.escape(secret)}\b", code), secret


def test_pgpassword_is_set_briefly_and_cleared_in_finally():
    code = code_only()
    assert "$env:PGPASSWORD = " in code
    finals = re.findall(r"finally\s*\{(.*?)\n\s*\}", code, flags=re.S)
    assert any(re.search(r"Remove-Item\s+Env:\\?PGPASSWORD", block) for block in finals), "PGPASSWORD must be removed in a finally block"
    # every place that sets it is inside Invoke-Psql, whose finally clears it
    assert code.count("$env:PGPASSWORD = ") == 1


def test_runs_the_database_bootstrap_with_stop_on_error():
    assert r"db\init.sql" in TEXT
    assert "ON_ERROR_STOP=1" in TEXT
    assert "'-U', 'postgres'" in TEXT and "localhost" in TEXT
    assert (ROOT / "db" / "init.sql").exists()


def test_does_not_change_execution_policy_or_need_admin():
    code = code_only()
    assert "Set-ExecutionPolicy" not in code
    assert "RunAs" not in code and "-Verb" not in code
    assert "ExecutionPolicy Bypass" in TEXT          # the documented way to run it (in the help text)


def test_has_the_documented_parameters_and_choices():
    assert re.search(r"\[int\]\$Port\s*=\s*5432", TEXT)
    assert re.search(r"\$Llm\s*=\s*''", TEXT)
    for choice in ("ollama", "openrouter", "huggingface", "skip"):
        assert choice in TEXT
    for prefix in ("qwen2.5", "qwen3", "llama3.1", "llama3.2", "mistral-nemo"):
        assert f"'{prefix}'" in TEXT
    assert "qwen2.5:7b" in TEXT and "qwen2.5:3b" in TEXT and "4.7 GB" in TEXT
    assert "postgresql-x64-16" in TEXT and "Start-Service" in TEXT
    assert "free-models" in TEXT
    assert "$0" in TEXT and "NOT free" in TEXT         # the Hugging Face warning


def test_python_preference_order_and_next_command():
    assert "@('3.13', '3.12', '3.11')" in TEXT         # tried in this order, through the py launcher
    assert r".venv\Scripts\python.exe scripts\dev.py up" in TEXT
    assert "doctor" in TEXT and "'init'" in TEXT


def test_env_file_is_written_without_a_bom_and_never_overwrites_user_values():
    assert "UTF8Encoding($false)" in TEXT              # no BOM
    assert "Set-Content" not in code_only() and "Out-File" not in code_only()    # those add a BOM in PowerShell 5.1
    assert "TemplateValue" in TEXT                     # only template defaults are replaced


def test_no_powershell7_only_syntax():
    code = code_only()
    assert "??" not in code and "&&" not in code and "||" not in code
    assert "-AsPlainText" not in code and "-Parallel" not in code
    assert not re.search(r"\$\w+\s*\?\s", code)        # ternary
    assert "#requires -Version 5.1" in TEXT


def test_readme_documents_the_one_command():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "powershell -ExecutionPolicy Bypass -File scripts\\setup_windows.ps1" in readme
    assert readme.index("Windows quick start") < readme.index("## Install")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh (PowerShell 7) is not installed")
def test_parses_with_the_real_powershell_parser():
    command = ("$e=$null;$t=$null;"
               f"[void][System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',[ref]$t,[ref]$e);"
               "if ($e.Count) { $e | ForEach-Object { $_.ToString() }; exit 1 }")
    result = subprocess.run(["pwsh", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
