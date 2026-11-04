"""Review round 3, N8 (MED): uvicorn believes X-Forwarded-For from 127.0.0.1 by default and rewrites the client address itself, so
`RECTIFI_TRUSTED_PROXIES` being empty did not mean "header ignored". Every launch path now turns uvicorn's proxy headers off, and
Rectifi's own `auth.client_address` is the only code that reads the header."""

import importlib.util
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEV = ROOT / "scripts" / "dev.py"
DOCKERFILE = ROOT / "deploy" / "Dockerfile"
COMPOSE = ROOT / "deploy" / "compose.oracle.yml"
# every file that starts uvicorn (outside tests/): a new one must be added here AND carry the switch
LAUNCHERS = {"scripts/dev.py", "rectifi/sms_gateway.py", "deploy/Dockerfile", "rectifi/api/app.py"}
LAUNCH_PATTERN = re.compile(r'-m", "uvicorn"|-m uvicorn|uvicorn\.run\(|uvicorn\.Config\(|uvicorn\.Server\(|uvicorn\s+--factory')


def load_dev():
    spec = importlib.util.spec_from_file_location("rectifi_dev_script_proxy", DEV)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parsed_options(command: list[str]) -> dict:
    """What uvicorn's own command line makes of these arguments (the same parser `python -m uvicorn` uses)."""
    cli = importlib.import_module("uvicorn.main").main            # (the package attribute `uvicorn.main` is the same command)
    args = command[command.index("uvicorn") + 1:]
    return cli.make_context("uvicorn", list(args), resilient_parsing=False).params


def dockerfile_cmd() -> list[str]:
    line = next(ln for ln in DOCKERFILE.read_text(encoding="utf-8").splitlines() if ln.startswith("CMD ") and "uvicorn" in ln)
    return json.loads(line[len("CMD "):])


def test_the_uvicorn_default_is_what_the_review_found():
    assert parsed_options(["uvicorn", "--factory", "x:y"])["proxy_headers"] is True                  # on by default ...


@pytest.mark.parametrize("reload", [False, True])
def test_dev_py_app_command_turns_proxy_headers_off(reload):
    dev = load_dev()
    command = dev.api_command("8501", reload=reload)
    assert parsed_options(command)["proxy_headers"] is False


def test_dev_py_gateway_command_turns_proxy_headers_off():
    dev = load_dev()
    assert parsed_options(dev.gateway_command(8765))["proxy_headers"] is False


def test_the_dockerfile_command_turns_proxy_headers_off():
    assert parsed_options(dockerfile_cmd())["proxy_headers"] is False


def test_the_gateway_process_turns_proxy_headers_off(monkeypatch):
    import uvicorn

    from rectifi import sms_gateway

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    sms_gateway.main()
    assert seen["proxy_headers"] is False


def test_compose_runs_uvicorn_only_through_the_image_command_or_the_gateway_main():
    text = COMPOSE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "uvicorn" in line and not line.lstrip().startswith("#"):
            assert "--no-proxy-headers" in line, line
    assert 'command: ["python", "-m", "rectifi.sms_gateway"]' in text                 # that path is main(), tested above


def test_every_launch_path_in_the_repo_is_known_and_carries_the_switch():
    found = set()
    for path in list(ROOT.glob("rectifi/**/*.py")) + list(ROOT.glob("scripts/**/*.py")) + list(ROOT.glob("deploy/**/*")) + [ROOT / "app" / "streamlit_app.py"]:
        if not path.is_file() or path.suffix in {".pyc", ".pem"} or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if LAUNCH_PATTERN.search(text):
            found.add(path.relative_to(ROOT).as_posix())
            assert "no-proxy-headers" in text or "proxy_headers=False" in text, f"{path} starts uvicorn without turning proxy headers off"
    assert found <= LAUNCHERS, f"a new place starts uvicorn: {sorted(found - LAUNCHERS)}; add it to LAUNCHERS and test it"


# ------------------------- live: a real uvicorn server -------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(options: dict):
    import uvicorn

    from rectifi.api.app import create_app

    port = free_port()
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="off",
                            proxy_headers=options["proxy_headers"], forwarded_allow_ips=options["forwarded_allow_ips"])
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        assert time.time() < deadline, "the test server did not start"
        time.sleep(0.05)
    return server, thread, port


def forged_login(port: int) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/auth/login", data=json.dumps({"username": "nobody", "password": "wrong password!"}).encode(),
        headers={"Content-Type": "application/json", "X-Rectifi-Request": "1", "X-Forwarded-For": "203.0.113.5"}, method="POST")
    try:
        urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError:
        pass                                                                           # a 401 is the expected answer


@pytest.fixture
def multiuser(monkeypatch):
    from rectifi import users

    for name in ("RECTIFI_PASSWORD_HASH", "RECTIFI_PUBLIC", "RECTIFI_BIND_HOST", "RECTIFI_TAILSCALE_USER", "RECTIFI_TRUSTED_PROXIES",
                 "FORWARDED_ALLOW_IPS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "s" * 48)
    users.reset_limiters()
    yield users
    users.reset_limiters()


def counted_clients(users) -> set[str]:
    return {pair[1] for pair in users.login_throttle._pairs}


def test_live_with_no_trusted_proxies_a_forwarded_address_from_loopback_is_counted_as_loopback(multiuser):
    options = parsed_options(load_dev().api_command("8501"))          # the options the real launch command gives uvicorn
    assert options["proxy_headers"] is False
    server, thread, port = serve(options)
    try:
        forged_login(port)
    finally:
        server.should_exit = True
        thread.join(10)
    assert counted_clients(multiuser) == {"ip:127.0.0.1"}


def test_live_control_uvicorns_default_would_have_counted_the_forged_address(multiuser):
    server, thread, port = serve(parsed_options(["uvicorn", "--factory", "x:y"]))      # uvicorn's defaults, no switch
    try:
        forged_login(port)
    finally:
        server.should_exit = True
        thread.join(10)
    assert counted_clients(multiuser) == {"ip:203.0.113.5"}                           # the bug the review found
