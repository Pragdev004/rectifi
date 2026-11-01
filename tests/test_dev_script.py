"""scripts/dev.py is the first thing a new student runs, so it gets tests too."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / "scripts" / "dev.py"


def dev(*args, env=None):
    full_env = {**os.environ, **(env or {})}
    if args and args[0] == "doctor" and "--quick" not in args:
        args = (*args, "--quick")          # no model round trip in tests
    return subprocess.run([sys.executable, str(DEV), *args], cwd=ROOT, env=full_env, capture_output=True, text=True, timeout=60)


def test_mcp_config_is_valid_json_without_secrets():
    result = dev("mcp-config", env={"DATABASE_URL": "postgresql://rectifi:topsecretpw@localhost:5432/rectifi"})
    assert result.returncode == 0
    config = json.loads(result.stdout)          # stdout must be pure JSON
    server = config["mcpServers"]["rectifi"]
    assert server["args"] == ["-m", "rectifi.mcp_server"]
    assert "topsecretpw" not in result.stdout + result.stderr


def test_doctor_passes_when_everything_is_up():
    result = dev("doctor")
    assert result.returncode == 0, result.stdout
    assert "Database reachable" in result.stdout


def test_doctor_fails_clearly_when_database_is_down():
    url = "postgresql://rectifi:topsecretpw@127.0.0.1:9/rectifi"
    result = dev("doctor", env={"DATABASE_URL": url})
    assert result.returncode == 1
    assert "FAIL" in result.stdout and "127.0.0.1:9/rectifi" in result.stdout
    assert "topsecretpw" not in result.stdout + result.stderr


def test_doctor_never_prints_the_api_key():
    result = dev("doctor", env={"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "sk-very-secret-123"})
    assert "sk-very-secret-123" not in result.stdout + result.stderr
    assert "API key is set" in result.stdout


def test_no_shell_true_anywhere():
    offenders = []
    for path in list(ROOT.glob("rectifi/**/*.py")) + list(ROOT.glob("scripts/*.py")) + list(ROOT.glob("app/*.py")):
        if re.search(r"shell\s*=\s*True|os\.system\(", path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert not offenders


# ───────────────────────── free-models, up, doctor for the new providers ─────────────────────────

import importlib.util

import pytest

FIXTURE_MODELS = {"data": [
    {"id": "vendor/alpha:free", "context_length": 131072, "supported_parameters": ["tools", "temperature"]},
    {"id": "vendor/beta:free", "context_length": 32768, "supported_parameters": ["temperature"]},          # no tools
    {"id": "vendor/alpha", "context_length": 131072, "supported_parameters": ["tools"]},                    # paid twin
    {"id": "other/gamma:free", "context_length": None, "supported_parameters": ["tool_choice", "tools"]},
    {"id": "other/delta:free", "context_length": 8000, "supported_parameters": None},                       # null list
    {"id": "other/epsilon:free"},                                                                           # key missing
    {"id": "other/free-sounding", "supported_parameters": ["tools"]},                                       # ":free" must be the suffix
]}


def load_dev():
    spec = importlib.util.spec_from_file_location("rectifi_dev_script", DEV)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_free_models_keeps_only_free_models_with_tools():
    dev_module = load_dev()
    assert dev_module.free_tool_models(FIXTURE_MODELS) == [("other/gamma:free", None), ("vendor/alpha:free", 131072)]
    assert dev_module.free_tool_models({}) == []
    assert dev_module.free_tool_models({"data": []}) == []


def test_free_models_offline_is_friendly():
    result = dev("free-models", env={"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                                      "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"})
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "internet" in result.stderr.lower() and "openrouter.ai/models" in result.stderr


def test_free_models_prints_ids_and_the_env_hint(monkeypatch, capsys):
    import io

    dev_module = load_dev()
    monkeypatch.setattr(dev_module.urllib.request, "urlopen", lambda *a, **k: io.StringIO(json.dumps(FIXTURE_MODELS)))
    dev_module.cmd_free_models(None)
    out = capsys.readouterr().out
    assert "vendor/alpha:free" in out and "131072" in out and "other/gamma:free" in out
    assert "vendor/beta:free" not in out
    assert "OPENROUTER_MODEL=" in out and "50 a day" in out


def test_up_skips_docker_when_postgres_is_already_running(monkeypatch, capsys):
    dev_module = load_dev()
    calls = []
    monkeypatch.setattr(dev_module, "database_reachable", lambda: True)
    monkeypatch.setattr(dev_module, "cmd_db", lambda a: calls.append("db"))
    monkeypatch.setattr(dev_module, "cmd_init", lambda a: calls.append("init"))
    monkeypatch.setattr(dev_module, "cmd_app", lambda a: calls.append("app"))
    monkeypatch.setattr(dev_module, "gateway_enabled", lambda: False)
    dev_module.cmd_up(None)
    assert calls == ["init", "app"]
    assert "already running" in capsys.readouterr().out


def test_up_starts_docker_when_postgres_is_down(monkeypatch):
    dev_module = load_dev()
    calls = []
    monkeypatch.setattr(dev_module, "database_reachable", lambda: False)
    monkeypatch.setattr(dev_module, "cmd_db", lambda a: calls.append("db"))
    monkeypatch.setattr(dev_module, "cmd_init", lambda a: calls.append("init"))
    monkeypatch.setattr(dev_module, "cmd_app", lambda a: calls.append("app"))
    monkeypatch.setattr(dev_module, "gateway_enabled", lambda: False)
    dev_module.cmd_up(None)
    assert calls == ["db", "init", "app"]


def test_db_without_docker_points_windows_users_to_the_setup_script(monkeypatch):
    import pytest

    dev_module = load_dev()
    monkeypatch.setattr(dev_module.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as stop:
        dev_module.cmd_db(None)
    assert "setup_windows.ps1" in str(stop.value)


def test_doctor_openrouter_missing_model_and_never_prints_key():
    result = dev("doctor", env={"LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-very-secret-9",
                                "OPENROUTER_MODEL": ""})
    out = result.stdout + result.stderr
    assert "sk-or-very-secret-9" not in out
    assert "OPENROUTER_MODEL" in out and "free-models" in out


def test_doctor_openrouter_ready_and_huggingface_warns_about_credits():
    ok = dev("doctor", env={"LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-or-x", "OPENROUTER_MODEL": "v/m:free"})
    assert "LLM: openrouter, model v/m:free, key is set" in ok.stdout
    hf = dev("doctor", env={"LLM_PROVIDER": "huggingface", "HF_TOKEN": "hf_very_secret"})
    assert "hf_very_secret" not in hf.stdout + hf.stderr
    assert "not free" in hf.stdout and "$0" in hf.stdout and "DeepSeek-V3.1:cheapest" in hf.stdout


def test_doctor_openai_compat_lists_what_is_missing():
    result = dev("doctor", env={"LLM_PROVIDER": "openai_compat", "OPENAI_COMPAT_BASE_URL": "",
                                "OPENAI_COMPAT_API_KEY": "", "OPENAI_COMPAT_MODEL": ""})
    for name in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL", "OPENAI_COMPAT_BASE_URL"):
        assert name in result.stdout


# ───────────────────────── the phone SMS gateway commands ─────────────────────────

def test_phone_command_builds_the_uvicorn_arguments_as_a_list(monkeypatch):
    dev_module = load_dev()
    monkeypatch.setattr(dev_module, "project_python", lambda: "PY")
    monkeypatch.setattr(dev_module, "read_env_file", lambda: {})                # the default, whatever is in your .env
    monkeypatch.delenv("RECTIFI_SMS_HOST", raising=False)
    assert dev_module.gateway_command(8765) == [
        "PY", "-m", "uvicorn", "--factory", "rectifi.sms_gateway:create_app", "--host", "127.0.0.1", "--port", "8765"]
    assert dev_module.gateway_command(9000)[-2:] == ["--port", "9000"]


def test_phone_command_uses_the_port_from_the_environment(monkeypatch):
    dev_module = load_dev()
    calls = []
    monkeypatch.setattr(dev_module, "project_python", lambda: "PY")
    monkeypatch.setattr(dev_module, "run", lambda cmd, extra_env=None, check=True: calls.append(cmd) or 0)
    monkeypatch.setenv("RECTIFI_SMS_PORT", "9123")
    monkeypatch.setattr(dev_module, "read_env_file", lambda: {})
    monkeypatch.delenv("RECTIFI_SMS_HOST", raising=False)
    with pytest.raises(SystemExit) as stop:
        dev_module.cmd_phone(None)
    assert stop.value.code == 0
    assert calls == [["PY", "-m", "uvicorn", "--factory", "rectifi.sms_gateway:create_app", "--host", "127.0.0.1", "--port", "9123"]]


@pytest.mark.parametrize("value, port", [("", 8765), ("9000", 9000), ("abc", 8765), ("0", 8765), ("70000", 8765), (" 8800 ", 8800)])
def test_gateway_port_falls_back_to_the_default_for_nonsense(monkeypatch, value, port):
    dev_module = load_dev()
    monkeypatch.setenv("RECTIFI_SMS_PORT", value)
    assert dev_module.gateway_port() == port


@pytest.mark.parametrize("value, on", [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("", False), ("no", False)])
def test_gateway_is_off_unless_asked_for(monkeypatch, value, on):
    dev_module = load_dev()
    monkeypatch.setenv("RECTIFI_SMS_GATEWAY", value)
    assert dev_module.gateway_enabled() is on


def test_up_does_not_start_the_gateway_by_default(monkeypatch):
    dev_module = load_dev()
    started = []
    monkeypatch.setattr(dev_module, "database_reachable", lambda: True)
    monkeypatch.setattr(dev_module, "cmd_init", lambda a: None)
    monkeypatch.setattr(dev_module, "cmd_app", lambda a: None)
    monkeypatch.setattr(dev_module.subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.setattr(dev_module, "gateway_enabled", lambda: False)
    dev_module.cmd_up(None)
    assert started == []


class FakeProcess:
    def __init__(self, alive=True):
        self.alive, self.terminated, self.killed = alive, False, False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated, self.alive = True, False

    def kill(self):
        self.killed, self.alive = True, False

    def wait(self, timeout=None):
        return 0


def test_up_starts_the_gateway_as_a_child_and_stops_it_when_the_app_ends(monkeypatch):
    dev_module = load_dev()
    events, popen_calls, child = [], [], FakeProcess()
    monkeypatch.setattr(dev_module, "database_reachable", lambda: True)
    monkeypatch.setattr(dev_module, "cmd_init", lambda a: events.append("init"))
    monkeypatch.setattr(dev_module, "cmd_app", lambda a: events.append("app"))
    monkeypatch.setattr(dev_module, "gateway_enabled", lambda: True)
    monkeypatch.setattr(dev_module, "gateway_port", lambda: 8765)
    monkeypatch.setattr(dev_module, "gateway_listening", lambda port: False)
    monkeypatch.setattr(dev_module, "project_python", lambda: "PY")
    monkeypatch.setattr(dev_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(dev_module.subprocess, "Popen", lambda cmd, **kw: popen_calls.append((cmd, kw)) or child)
    dev_module.cmd_up(None)
    assert events == ["init", "app"]
    [(cmd, kwargs)] = popen_calls
    assert isinstance(cmd, list) and cmd[:3] == ["PY", "-m", "uvicorn"] and not kwargs.get("shell")
    assert child.terminated                                   # stopped when the app ended


def test_ctrl_c_in_the_app_still_stops_the_gateway(monkeypatch):
    dev_module = load_dev()
    child = FakeProcess()
    monkeypatch.setattr(dev_module, "database_reachable", lambda: True)
    monkeypatch.setattr(dev_module, "cmd_init", lambda a: None)
    monkeypatch.setattr(dev_module, "gateway_enabled", lambda: True)
    monkeypatch.setattr(dev_module, "gateway_listening", lambda port: False)
    monkeypatch.setattr(dev_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(dev_module.subprocess, "Popen", lambda cmd, **kw: child)

    def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(dev_module, "cmd_app", interrupted)
    with pytest.raises(KeyboardInterrupt):
        dev_module.cmd_up(None)
    assert child.terminated


def test_a_gateway_that_dies_at_once_does_not_stop_the_app(monkeypatch, capsys):
    dev_module = load_dev()
    events = []
    monkeypatch.setattr(dev_module, "database_reachable", lambda: True)
    monkeypatch.setattr(dev_module, "cmd_init", lambda a: None)
    monkeypatch.setattr(dev_module, "cmd_app", lambda a: events.append("app"))
    monkeypatch.setattr(dev_module, "gateway_enabled", lambda: True)
    monkeypatch.setattr(dev_module, "gateway_listening", lambda port: False)
    monkeypatch.setattr(dev_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(dev_module.subprocess, "Popen", lambda cmd, **kw: FakeProcess(alive=False))
    dev_module.cmd_up(None)
    assert events == ["app"]
    out = capsys.readouterr().out
    assert "stopped right away" in out and "RECTIFI_SMS_PORT" in out


def test_up_does_not_start_a_second_gateway_when_one_already_answers(monkeypatch):
    dev_module = load_dev()
    started = []
    monkeypatch.setattr(dev_module, "gateway_enabled", lambda: True)
    monkeypatch.setattr(dev_module, "gateway_listening", lambda port: True)
    monkeypatch.setattr(dev_module.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert dev_module.start_gateway() is None and started == []


def test_stop_gateway_kills_a_child_that_ignores_terminate_and_survives_none_and_dead_children():
    dev_module = load_dev()
    dev_module.stop_gateway(None)
    dev_module.stop_gateway(FakeProcess(alive=False))

    class Stubborn(FakeProcess):
        def terminate(self):
            self.terminated = True                          # still alive

        def wait(self, timeout=None):
            if not self.killed:
                raise dev_module.subprocess.TimeoutExpired("x", 1)
            return 0

    stubborn = Stubborn()
    dev_module.stop_gateway(stubborn)
    assert stubborn.terminated and stubborn.killed


def test_doctor_reports_the_gateway_off_by_default_and_on_but_not_running():
    off = dev("doctor", env={"RECTIFI_SMS_GATEWAY": "0"})
    assert off.returncode == 0 and "Phone gateway: off" in off.stdout
    on = dev("doctor", env={"RECTIFI_SMS_GATEWAY": "1", "RECTIFI_SMS_PORT": "8799"})
    assert on.returncode == 0                                              # a warning, not a failure
    assert "Phone gateway is turned on" in on.stdout and "8799" in on.stdout


# ───────────────────────── the web app commands ─────────────────────────

def _capture_run(dev_module, monkeypatch):
    calls = []
    monkeypatch.setattr(dev_module, "run", lambda cmd, extra_env=None, check=True: calls.append((cmd, extra_env or {})) or 0)
    return calls


def test_app_runs_the_web_server_on_localhost_8501(monkeypatch, tmp_path):
    dev_module = load_dev()
    (tmp_path / "index.html").write_text("x")
    monkeypatch.setattr(dev_module, "WEB_DIST", tmp_path)
    monkeypatch.setattr(dev_module, "tailscale_origin", lambda: "https://laptop.tail1.ts.net")
    calls = _capture_run(dev_module, monkeypatch)
    dev_module.cmd_app(None)
    (command, env), = calls
    assert command[1:] == ["-m", "uvicorn", "--factory", "rectifi.api.app:create_app", "--host", "127.0.0.1",
                           "--port", "8501", "--log-level", "warning"]
    assert "--workers" not in command and "0.0.0.0" not in command
    assert env == {"RECTIFI_ALLOWED_ORIGINS": "https://laptop.tail1.ts.net"}


def test_app_streamlit_flag_runs_streamlit_unchanged(monkeypatch):
    import argparse

    dev_module = load_dev()
    monkeypatch.setattr(dev_module, "tailscale_origin", lambda: None)
    calls = _capture_run(dev_module, monkeypatch)
    dev_module.cmd_app(argparse.Namespace(streamlit=True))
    (command, env), = calls
    assert command[1:] == ["-m", "streamlit", "run", "app/streamlit_app.py", "--server.port", "8501", "--server.address", "127.0.0.1"]
    assert env == {}


def test_up_with_streamlit_flag_reaches_cmd_app(monkeypatch):
    import argparse

    dev_module = load_dev()
    seen = []
    monkeypatch.setattr(dev_module, "database_reachable", lambda: True)
    monkeypatch.setattr(dev_module, "cmd_init", lambda a: None)
    monkeypatch.setattr(dev_module, "gateway_enabled", lambda: False)
    monkeypatch.setattr(dev_module, "cmd_app", lambda a: seen.append(getattr(a, "streamlit", None)))
    dev_module.cmd_up(argparse.Namespace(streamlit=True))
    assert seen == [True]
    result = dev("up", "--help")
    assert "--streamlit" in result.stdout


def test_a_missing_web_build_exits_1_with_both_ways_forward(monkeypatch, tmp_path):
    import pytest

    dev_module = load_dev()
    monkeypatch.setattr(dev_module, "WEB_DIST", tmp_path / "no-dist")
    calls = _capture_run(dev_module, monkeypatch)
    with pytest.raises(SystemExit) as stop:
        dev_module.cmd_app(None)
    message = str(stop.value)
    assert "build-web" in message and "--streamlit" in message and calls == []


def test_demo_uses_the_web_server_on_8502_with_the_demo_database(monkeypatch, tmp_path):
    dev_module = load_dev()
    (tmp_path / "index.html").write_text("x")
    monkeypatch.setattr(dev_module, "WEB_DIST", tmp_path)
    monkeypatch.setattr(dev_module, "tailscale_origin", lambda: None)
    calls = _capture_run(dev_module, monkeypatch)
    dev_module.cmd_demo(None)
    uvicorn_call = next(c for c in calls if "uvicorn" in c[0])
    assert uvicorn_call[0][uvicorn_call[0].index("--port") + 1] == "8502"
    assert uvicorn_call[1]["DATABASE_URL"].endswith("/rectifi_demo")


def test_web_source_hash_ignores_crlf_and_notices_changes(monkeypatch, tmp_path):
    dev_module = load_dev()
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "public").mkdir()
    (web / "src" / "a.ts").write_bytes(b"line1\nline2\n")
    (web / "public" / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (web / "package.json").write_bytes(b"{}\n")
    (web / "tsconfig.app.json").write_bytes(b"{}\n")
    (web / "node_modules").mkdir()
    (web / "node_modules" / "ignored.js").write_bytes(b"ignored")
    (web / "dist").mkdir()
    (web / "dist" / "index.html").write_bytes(b"ignored")
    monkeypatch.setattr(dev_module, "WEB_DIR", web)
    first = dev_module.web_source_hash()
    assert len(first) == 64
    (web / "src" / "a.ts").write_bytes(b"line1\r\nline2\r\n")
    assert dev_module.web_source_hash() == first                       # Windows autocrlf must not make the build look stale
    (web / "node_modules" / "ignored.js").write_bytes(b"changed")
    (web / "dist" / "index.html").write_bytes(b"changed")
    assert dev_module.web_source_hash() == first
    (web / "src" / "a.ts").write_bytes(b"line1\nline2 changed\n")
    assert dev_module.web_source_hash() != first
    (web / "src" / "a.ts").write_bytes(b"line1\nline2\n")
    (web / "src" / "new.ts").write_bytes(b"x")
    assert dev_module.web_source_hash() != first


def test_build_web_explains_when_node_is_missing_or_old(monkeypatch):
    import pytest

    dev_module = load_dev()
    monkeypatch.setattr(dev_module.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as stop:
        dev_module.cmd_build_web(None)
    assert "nodejs.org" in str(stop.value) and "running Rectifi does not" in str(stop.value)
    monkeypatch.setattr(dev_module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(dev_module, "node_version", lambda: (18, 20, 0))
    with pytest.raises(SystemExit) as stop:
        dev_module.cmd_build_web(None)
    assert "too old" in str(stop.value) and "20.19" in str(stop.value)


def test_build_web_runs_npm_ci_build_and_test_then_writes_build_info(monkeypatch, tmp_path):
    dev_module = load_dev()
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "src" / "a.ts").write_text("x")
    (web / "dist").mkdir()
    monkeypatch.setattr(dev_module, "WEB_DIR", web)
    monkeypatch.setattr(dev_module, "WEB_DIST", web / "dist")
    monkeypatch.setattr(dev_module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(dev_module, "node_version", lambda: (22, 22, 0))
    steps = []
    monkeypatch.setattr(dev_module, "npm", lambda path, *args, extra_env=None: steps.append(args) or 0)
    dev_module.cmd_build_web(None)
    assert steps == [("ci",), ("run", "build"), ("test",)]
    info = json.loads((web / "dist" / "build-info.json").read_text())
    assert info == {"source_hash": dev_module.web_source_hash(), "node": "22.22.0"}


def test_doctor_reports_the_web_build():
    out = dev("doctor").stdout
    assert "Web app" in out


def funnel_dev(monkeypatch, *, funnel, password="", public=""):
    dev_module = load_dev()
    monkeypatch.setenv("RECTIFI_PASSWORD_HASH", password)
    monkeypatch.setenv("RECTIFI_PUBLIC", public)
    monkeypatch.setenv("RECTIFI_BIND_HOST", "127.0.0.1")
    ts = dev_module.load_tailscale_helpers()
    monkeypatch.setattr(dev_module, "load_tailscale_helpers", lambda: ts)
    monkeypatch.setattr(ts, "find_cli", lambda: "tailscale")
    monkeypatch.setattr(ts, "read_funnel_exposes_app", lambda cli, port=8501, runner=None: funnel)
    return dev_module


def test_up_refuses_to_start_when_funnel_is_on_and_there_is_no_login(monkeypatch):
    dev_module = funnel_dev(monkeypatch, funnel=True)
    started = []
    monkeypatch.setattr(dev_module, "database_reachable", lambda: started.append("db") or True)
    monkeypatch.setattr(dev_module, "cmd_init", lambda a: started.append("init"))
    monkeypatch.setattr(dev_module, "cmd_app", lambda a: started.append("app"))
    with pytest.raises(SystemExit) as stop:
        dev_module.cmd_up(None)
    assert "Funnel is on: set RECTIFI_PUBLIC=1 and run `python scripts/dev.py set-password` first" in str(stop.value)
    assert started == []


@pytest.mark.parametrize("kw", [{"funnel": False}, {"funnel": True, "public": "1"}, {"funnel": True, "password": "scrypt$x"}])
def test_up_starts_when_there_is_no_funnel_or_a_login_is_on(monkeypatch, kw):
    dev_module = funnel_dev(monkeypatch, **kw)
    started = []
    monkeypatch.setattr(dev_module, "database_reachable", lambda: True)
    monkeypatch.setattr(dev_module, "cmd_init", lambda a: None)
    monkeypatch.setattr(dev_module, "cmd_app", lambda a: started.append("app"))
    monkeypatch.setattr(dev_module, "gateway_enabled", lambda: False)
    dev_module.cmd_up(None)
    assert started == ["app"]


def test_funnel_check_is_quiet_without_tailscale(monkeypatch):
    dev_module = load_dev()
    monkeypatch.setenv("RECTIFI_PASSWORD_HASH", "")
    monkeypatch.setenv("RECTIFI_PUBLIC", "")
    ts = dev_module.load_tailscale_helpers()
    monkeypatch.setattr(dev_module, "load_tailscale_helpers", lambda: ts)
    monkeypatch.setattr(ts, "find_cli", lambda: None)
    assert dev_module.funnel_without_login() is False


def test_doctor_fails_when_funnel_is_on_without_a_login(monkeypatch, capsys):
    import argparse

    dev_module = funnel_dev(monkeypatch, funnel=True)
    with pytest.raises(SystemExit) as stop:
        dev_module.cmd_doctor(argparse.Namespace(quick=True))
    out = capsys.readouterr().out
    assert stop.value.code == 1 and "FAIL  Tailscale Funnel" in out and "RECTIFI_PUBLIC=1" in out and "set-password" in out
