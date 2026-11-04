"""Use Rectifi from a phone through Tailscale Serve, with the app and the gateway bound to 127.0.0.1.

Nothing here runs Tailscale: the command lines are checked as lists, and Tailscale's answers come from fixture JSON
written from its documentation (tests/fixtures/tailscale_*.json), not captured from a real install.
"""

import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from rectifi import config, tailscale

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / "scripts" / "dev.py"
FIXTURES = ROOT / "tests" / "fixtures"
STATUS = json.loads((FIXTURES / "tailscale_status.json").read_text(encoding="utf-8"))
SERVE = json.loads((FIXTURES / "tailscale_serve_status.json").read_text(encoding="utf-8"))
NAME = "prag-laptop.tail1a2b3.ts.net"


def load_dev():
    spec = importlib.util.spec_from_file_location("rectifi_dev_script_ts", DEV)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ───────────────────────── finding the command ─────────────────────────

def test_the_cli_on_path_wins():
    assert tailscale.find_cli(which=lambda name: "/usr/bin/tailscale", exists=lambda p: True) == "/usr/bin/tailscale"


def test_the_usual_windows_install_folder_is_checked_when_it_is_not_on_path():
    seen = []

    def exists(path):
        seen.append(path)
        return path == r"C:\Program Files\Tailscale\tailscale.exe"

    assert tailscale.find_cli(which=lambda name: None, exists=exists) == r"C:\Program Files\Tailscale\tailscale.exe"
    assert tailscale.find_cli(which=lambda name: None, exists=lambda p: p.startswith(r"C:\Program Files (x86)")) == \
        r"C:\Program Files (x86)\Tailscale\tailscale.exe"


def test_no_tailscale_means_none():
    assert tailscale.find_cli(which=lambda name: None, exists=lambda p: False) is None


# ───────────────────────── the commands ─────────────────────────

def test_serve_commands_are_exact_lists_for_the_app_and_the_gateway():
    cli = r"C:\Program Files\Tailscale\tailscale.exe"
    assert tailscale.serve_commands(cli, 8501, 8765) == [
        [cli, "serve", "--bg", "--https=443", "http://127.0.0.1:8501"],
        [cli, "serve", "--bg", "--https=8443", "http://127.0.0.1:8765"],
    ]


def test_without_a_gateway_only_the_app_is_served_and_ports_follow_the_settings():
    assert tailscale.serve_commands("ts", 8501) == [["ts", "serve", "--bg", "--https=443", "http://127.0.0.1:8501"]]
    assert tailscale.serve_commands("ts", 9000, 9100)[1][-1] == "http://127.0.0.1:9100"


def test_every_command_is_a_list_of_strings_with_no_shell_joining():
    commands = (tailscale.serve_commands("ts", 8501, 8765) + tailscale.off_commands("ts") +
                [tailscale.reset_command("ts"), tailscale.status_command("ts"), tailscale.serve_status_command("ts")])
    for command in commands:
        assert isinstance(command, list) and all(isinstance(part, str) and part for part in command)
        assert not any(" " in part for part in command[1:])               # a flag and its value are one item, never "a b"
        assert not any(c in "".join(command[1:]) for c in ";&|`$<>")


def test_off_commands_only_touch_rectifis_two_ports_and_reset_is_the_separate_fallback():
    assert tailscale.off_commands("ts") == [["ts", "serve", "--https=443", "off"], ["ts", "serve", "--https=8443", "off"]]
    assert tailscale.off_commands("ts", gateway=False) == [["ts", "serve", "--https=443", "off"]]
    assert tailscale.reset_command("ts") == ["ts", "serve", "reset"]
    assert tailscale.status_command("ts") == ["ts", "status", "--json"]
    assert tailscale.serve_status_command("ts") == ["ts", "serve", "status", "--json"]


# ───────────────────────── reading Tailscale's answers ─────────────────────────

def test_the_dns_name_comes_from_self_without_the_trailing_dot():
    assert tailscale.dns_name(STATUS) == NAME


@pytest.mark.parametrize("status", [
    None, {}, [], "x", {"BackendState": "Running"}, {"BackendState": "Running", "Self": {}},
    {"BackendState": "Running", "Self": {"DNSName": ""}}, {"BackendState": "Running", "Self": {"DNSName": 5}},
    {"BackendState": "Stopped", "Self": {"DNSName": "a.ts.net."}}, {"BackendState": "NeedsLogin", "Self": {"DNSName": "a.ts.net."}},
])
def test_no_name_when_tailscale_is_off_signed_out_or_the_answer_is_odd(status):
    assert tailscale.dns_name(status) is None


def test_urls_for_the_phone():
    assert tailscale.app_url(NAME) == f"https://{NAME}/"
    assert tailscale.gateway_url(NAME) == f"https://{NAME}:8443/sms"
    assert tailscale.origin(NAME) == f"https://{NAME}"


def test_published_ports_are_read_from_the_web_map():
    assert tailscale.served_ports(SERVE) == {443, 8443}
    assert tailscale.served_ports({"Web": {f"{NAME}:443": {}}}) == {443}
    for junk in (None, {}, {"Web": None}, {"Web": []}, {"Web": {"nonsense": {}}}, "x"):
        assert tailscale.served_ports(junk) == set()


def fake_runner(stdout="", returncode=0, error=None):
    def run(command, **kwargs):
        run.calls.append((command, kwargs))
        if error:
            raise error
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    run.calls = []
    return run


def test_status_is_read_through_a_runner_without_a_shell_and_with_a_timeout():
    runner = fake_runner(json.dumps(STATUS))
    assert tailscale.read_status("ts", runner) == STATUS
    command, kwargs = runner.calls[0]
    assert command == ["ts", "status", "--json"] and not kwargs.get("shell") and kwargs["timeout"] > 0
    assert kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("runner", [
    fake_runner("{}", returncode=1), fake_runner("not json"), fake_runner("[1, 2]"),
    fake_runner(error=OSError("cannot start")), fake_runner(error=subprocess.TimeoutExpired("ts", 5)),
], ids=["fails", "not json", "not an object", "cannot start", "times out"])
def test_a_broken_tailscale_gives_none_not_a_crash(runner):
    assert tailscale.read_status("ts", runner) is None


def test_no_cli_means_nothing_is_run():
    runner = fake_runner(json.dumps(STATUS))
    assert tailscale.read_status(None, runner) is None and tailscale.read_served_ports(None, runner) == set()
    assert runner.calls == []


def test_served_ports_are_read_with_the_serve_status_command():
    runner = fake_runner(json.dumps(SERVE))
    assert tailscale.read_served_ports("ts", runner) == {443, 8443}
    assert runner.calls[0][0] == ["ts", "serve", "status", "--json"]


def test_run_visible_returns_the_exit_code_and_never_raises():
    assert tailscale.run_visible(["ts"], runner=lambda c, timeout: SimpleNamespace(returncode=3)) == 3
    assert tailscale.run_visible(["ts"], runner=fake_runner(error=FileNotFoundError())) == 127
    assert tailscale.run_visible(["ts"], runner=fake_runner(error=subprocess.TimeoutExpired("ts", 1))) == 124
    assert tailscale.run_visible(["ts"], runner=fake_runner(error=PermissionError())) == 126


def test_the_helper_module_uses_only_the_standard_library_and_no_shell():
    source = Path(tailscale.__file__).read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from|import) (\w+)", source, flags=re.M))
    assert imports <= {"__future__", "json", "os", "shutil", "subprocess", "collections"}, imports
    assert "shell=True" not in source and "rectifi" not in source.replace("Rectifi", "")


# ───────────────────────── bind to localhost ─────────────────────────

def test_the_gateway_host_defaults_to_this_laptop_only(monkeypatch):
    monkeypatch.delenv("RECTIFI_SMS_HOST", raising=False)
    assert config.sms_host() == "127.0.0.1" and not config.sms_host_is_open()


@pytest.mark.parametrize("value, host, is_open", [
    ("0.0.0.0", "0.0.0.0", True), ("192.168.1.9", "192.168.1.9", True), ("127.0.0.1", "127.0.0.1", False),
    ("::1", "::1", False), (" 127.0.0.1 ", "127.0.0.1", False), ("", "127.0.0.1", False),
    ("localhost", "127.0.0.1", False), ("all", "127.0.0.1", False), ("0.0.0.0; rm -rf", "127.0.0.1", False),
])
def test_the_gateway_host_setting_is_validated(monkeypatch, value, host, is_open):
    monkeypatch.setenv("RECTIFI_SMS_HOST", value)
    assert config.sms_host() == host and config.sms_host_is_open() is is_open


def test_the_gateway_process_listens_where_the_setting_says(monkeypatch):
    import uvicorn

    from rectifi import sms_gateway

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.delenv("RECTIFI_SMS_HOST", raising=False)
    sms_gateway.main()
    assert seen["host"] == "127.0.0.1"
    monkeypatch.setenv("RECTIFI_SMS_HOST", "0.0.0.0")
    sms_gateway.main()
    assert seen["host"] == "0.0.0.0"
    assert not hasattr(sms_gateway, "HOST")                                 # the old hard-coded 0.0.0.0 is gone


def test_dev_py_starts_the_gateway_and_the_app_on_127_0_0_1(monkeypatch):
    dev = load_dev()
    monkeypatch.setattr(dev, "project_python", lambda: "PY")
    monkeypatch.delenv("RECTIFI_SMS_HOST", raising=False)
    monkeypatch.setattr(dev, "read_env_file", lambda: {})
    assert dev.gateway_host() == "127.0.0.1" and not dev.gateway_is_open()
    assert dev.gateway_command(8765)[-5:] == ["--host", "127.0.0.1", "--port", "8765", "--no-proxy-headers"]
    assert dev.LOCAL_ONLY == ["--server.address", "127.0.0.1"]
    monkeypatch.setenv("RECTIFI_SMS_HOST", "0.0.0.0")
    assert dev.gateway_is_open() and "0.0.0.0" in dev.gateway_command(8765)
    monkeypatch.setenv("RECTIFI_SMS_HOST", "everyone")
    assert dev.gateway_host() == "127.0.0.1"


def test_dev_py_app_runs_streamlit_on_127_0_0_1_and_adds_the_tailscale_origin(monkeypatch):
    import argparse

    streamlit_flag = argparse.Namespace(streamlit=True)         # the web app is the default now; Streamlit is --streamlit
    dev = load_dev()
    calls = []
    monkeypatch.setattr(dev, "project_python", lambda: "PY")
    monkeypatch.setattr(dev, "run", lambda cmd, extra_env=None, check=True: calls.append((cmd, extra_env)) or 0)
    monkeypatch.setattr(dev, "tailscale_origin", lambda: f"https://{NAME}")
    dev.cmd_app(streamlit_flag)
    command, env = calls[0]
    assert command[command.index("--server.address") + 1] == "127.0.0.1"
    assert env == {"STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS": f"https://{NAME}"}
    monkeypatch.setattr(dev, "tailscale_origin", lambda: None)
    dev.cmd_app(streamlit_flag)
    assert calls[1][1] == {}
    monkeypatch.setattr(dev, "tailscale_origin", lambda: 1 / 0)               # a Tailscale problem must not stop the app
    dev.cmd_app(streamlit_flag)
    assert calls[2][1] == {}


def test_the_streamlit_config_binds_locally_and_keeps_xsrf_and_cors_on():
    server = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))["server"]
    assert server["address"] == "127.0.0.1"
    assert server["enableXsrfProtection"] is True and server["enableCORS"] is True
    assert "*.ts.net" in server["allowedHosts"] and "localhost" in server["allowedHosts"]


# ───────────────────────── dev.py tailscale ─────────────────────────

def fake_helpers(monkeypatch, dev, *, cli="ts", status=STATUS, served=(), exit_codes=None):
    """The real helper module with Tailscale itself replaced: no program is started."""
    helpers = dev.load_tailscale_helpers()
    ran = []
    codes = list(exit_codes or [])
    monkeypatch.setattr(helpers, "find_cli", lambda *a, **k: cli)
    monkeypatch.setattr(helpers, "read_status", lambda c, runner=None: status)
    monkeypatch.setattr(helpers, "read_served_ports", lambda c, runner=None: set(served))
    monkeypatch.setattr(helpers, "run_visible", lambda command, *a, **k: ran.append(command) or (codes.pop(0) if codes else 0))
    monkeypatch.setattr(dev, "load_tailscale_helpers", lambda: helpers)
    monkeypatch.setattr(dev, "gateway_port", lambda: 8765)
    return ran


def test_tailscale_on_publishes_the_app_and_prints_the_address(monkeypatch, capsys):
    dev = load_dev()
    ran = fake_helpers(monkeypatch, dev)
    monkeypatch.setattr(dev, "gateway_enabled", lambda: False)
    dev.cmd_tailscale(SimpleNamespace(action=None))
    assert ran == [["ts", "serve", "--bg", "--https=443", "http://127.0.0.1:8501"]]
    out = capsys.readouterr().out
    assert f"https://{NAME}/" in out and ":8443" not in out.split("Done.")[1] and "gateway is off" in out


def test_tailscale_on_with_the_gateway_publishes_both_and_prints_the_sms_address(monkeypatch, capsys):
    dev = load_dev()
    ran = fake_helpers(monkeypatch, dev)
    monkeypatch.setattr(dev, "gateway_enabled", lambda: True)
    dev.cmd_tailscale(SimpleNamespace(action="on"))
    assert ran == [["ts", "serve", "--bg", "--https=443", "http://127.0.0.1:8501"],
                   ["ts", "serve", "--bg", "--https=8443", "http://127.0.0.1:8765"]]
    out = capsys.readouterr().out
    assert f"https://{NAME}/" in out and f"https://{NAME}:8443/sms" in out and "certificates are logged publicly" in out


def test_tailscale_on_stops_and_explains_when_a_serve_command_fails(monkeypatch):
    dev = load_dev()
    ran = fake_helpers(monkeypatch, dev, exit_codes=[1])
    monkeypatch.setattr(dev, "gateway_enabled", lambda: True)
    with pytest.raises(SystemExit) as stop:
        dev.cmd_tailscale(SimpleNamespace(action="on"))
    assert "Enable HTTPS" in str(stop.value) and len(ran) == 1                 # it did not carry on with the second command


@pytest.mark.parametrize("kwargs, words", [
    ({"cli": None}, "not installed"),
    ({"status": {"BackendState": "NeedsLogin", "Self": {}}}, "not running or not signed in"),
])
def test_tailscale_on_says_what_is_wrong_when_it_cannot_run(monkeypatch, kwargs, words):
    dev = load_dev()
    ran = fake_helpers(monkeypatch, dev, **kwargs)
    with pytest.raises(SystemExit) as stop:
        dev.cmd_tailscale(SimpleNamespace(action="on"))
    assert words in str(stop.value) and ran == []


def test_tailscale_off_removes_both_and_falls_back_to_reset_in_the_message(monkeypatch, capsys):
    dev = load_dev()
    ran = fake_helpers(monkeypatch, dev)
    dev.cmd_tailscale(SimpleNamespace(action="off"))
    assert ran == [["ts", "serve", "--https=443", "off"], ["ts", "serve", "--https=8443", "off"]]
    assert "no longer published" in capsys.readouterr().out

    ran = fake_helpers(monkeypatch, dev, exit_codes=[1, 0])
    with pytest.raises(SystemExit):
        dev.cmd_tailscale(SimpleNamespace(action="off"))
    assert "ts serve reset" in capsys.readouterr().out and len(ran) == 2


def test_tailscale_status_shows_the_addresses_and_what_is_published(monkeypatch, capsys):
    dev = load_dev()
    fake_helpers(monkeypatch, dev, served=(443,))
    dev.cmd_tailscale(SimpleNamespace(action="status"))
    out = capsys.readouterr().out
    assert f"https://{NAME}/" in out and "(published)" in out and "(not published)" in out


def test_tailscale_subcommand_exists_and_accepts_on_off_status():
    result = subprocess.run([sys.executable, str(DEV), "tailscale", "--help"], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0 and "{on,off,status}" in result.stdout
    bad = subprocess.run([sys.executable, str(DEV), "tailscale", "frobnicate"], capture_output=True, text=True, cwd=ROOT)
    assert bad.returncode == 2


def test_tailscale_origin_is_none_without_tailscale(monkeypatch):
    dev = load_dev()
    helpers = dev.load_tailscale_helpers()
    monkeypatch.setattr(helpers, "find_cli", lambda *a, **k: None)
    monkeypatch.setattr(dev, "load_tailscale_helpers", lambda: helpers)
    assert dev.tailscale_origin() is None
    monkeypatch.setattr(helpers, "find_cli", lambda *a, **k: "ts")
    monkeypatch.setattr(helpers, "read_status", lambda c, runner=None: STATUS)
    assert dev.tailscale_origin() == f"https://{NAME}"


def test_doctor_mentions_tailscale_and_warns_about_an_open_gateway(monkeypatch):
    env = {"RECTIFI_SMS_GATEWAY": "1", "RECTIFI_SMS_HOST": "0.0.0.0", "RECTIFI_SMS_PORT": "8799"}
    result = subprocess.run([sys.executable, str(DEV), "doctor"], cwd=ROOT, capture_output=True, text=True, timeout=60,
                            env={**__import__("os").environ, **env})
    assert "Tailscale:" in result.stdout or "Tailscale is installed" in result.stdout
    assert "open to everyone on your network" in result.stdout


# ───────────── Funnel without a login ─────────────

def funnel_status(allow=None, proxy="http://127.0.0.1:8501"):
    return {"TCP": {"443": {"HTTPS": True}}, "Web": {"laptop.tail1a2b3.ts.net:443": {"Handlers": {"/": {"Proxy": proxy}}}},
            "AllowFunnel": {"laptop.tail1a2b3.ts.net:443": True} if allow is None else allow}


def test_funnel_on_for_the_app_is_seen():
    assert tailscale.funnel_exposes_app(funnel_status()) is True
    assert tailscale.funnel_exposes_app(funnel_status(proxy="http://localhost:8501/")) is True


def test_serve_alone_funnel_off_or_another_port_is_not_flagged():
    assert tailscale.funnel_exposes_app(SERVE) is False                                       # Serve, no AllowFunnel
    assert tailscale.funnel_exposes_app(funnel_status(allow={"laptop.tail1a2b3.ts.net:443": False})) is False
    assert tailscale.funnel_exposes_app(funnel_status(proxy="http://127.0.0.1:8765")) is False   # the gateway has its own token
    assert tailscale.funnel_exposes_app(funnel_status(allow={"other.ts.net:8443": True})) is False


@pytest.mark.parametrize("odd", [None, [], "x", {}, {"AllowFunnel": []}, {"AllowFunnel": {"a:443": True}},
                                 {"AllowFunnel": {"a:443": True}, "Web": {"a:443": "oops"}},
                                 {"AllowFunnel": {"a:443": True}, "Web": {"a:443": {"Handlers": ["x"]}}},
                                 {"AllowFunnel": {"a:443": True}, "Web": {"a:443": {"Handlers": {"/": {"Proxy": 7}}}}}])
def test_a_shape_it_cannot_read_is_treated_as_not_funnel(odd):
    assert tailscale.funnel_exposes_app(odd) is False


def test_reading_funnel_is_harmless_without_tailscale_or_with_bad_output():
    assert tailscale.read_funnel_exposes_app(None) is False

    class Done:
        def __init__(self, code, out):
            self.returncode, self.stdout = code, out

    assert tailscale.read_funnel_exposes_app("ts", runner=lambda *a, **k: Done(0, json.dumps(funnel_status()))) is True
    assert tailscale.read_funnel_exposes_app("ts", runner=lambda *a, **k: Done(1, "")) is False
    assert tailscale.read_funnel_exposes_app("ts", runner=lambda *a, **k: Done(0, "not json")) is False

    def broken(*a, **k):
        raise FileNotFoundError

    assert tailscale.read_funnel_exposes_app("ts", runner=broken) is False
    assert "RECTIFI_PUBLIC=1" in tailscale.FUNNEL_MESSAGE and "set-password" in tailscale.FUNNEL_MESSAGE
