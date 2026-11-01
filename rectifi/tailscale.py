"""Tailscale helpers: use Rectifi from your phone without opening your laptop to the whole Wi-Fi.

Standard library only (no other Rectifi module, no third-party package), because `scripts/dev.py` loads this file by
path before anything is installed. Everything here builds or reads command lines. It never uses a shell.

How remote access works. Streamlit (port 8501) and the SMS gateway (port 8765) listen on 127.0.0.1 only. Tailscale Serve
is a small HTTPS reverse proxy that runs on the laptop, reachable only from devices signed in to YOUR tailnet:

    https://<laptop>.<tailnet>.ts.net/          ->  http://127.0.0.1:8501    (the app)
    https://<laptop>.<tailnet>.ts.net:8443/sms  ->  http://127.0.0.1:8765    (the SMS gateway)

The commands below follow Tailscale's documentation (tailscale.com/kb/1242/tailscale-serve). They have not been run
against a real Tailscale install by the author: see README, "Not verified".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable

APP_PORT = 8501
GATEWAY_PORT = 8765
HTTPS_APP_PORT = 443
HTTPS_GATEWAY_PORT = 8443

WINDOWS_PATHS = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
)


def find_cli(which: Callable[[str], str | None] = shutil.which, exists: Callable[[str], bool] = os.path.exists) -> str | None:
    """The tailscale command: on PATH, else the usual Windows install folders. None if Tailscale is not installed."""
    found = which("tailscale")
    if found:
        return found
    return next((path for path in WINDOWS_PATHS if exists(path)), None)


def serve_commands(cli: str, app_port: int = APP_PORT, gateway_port: int | None = None) -> list[list[str]]:
    """The commands that publish the app (and the gateway, when a port is given) on the tailnet over HTTPS.

    Each is a list for subprocess, never a shell string. `http://127.0.0.1:<port>` is the only form Tailscale accepts
    for a local web server, so that is what is used."""
    commands = [[cli, "serve", "--bg", f"--https={HTTPS_APP_PORT}", f"http://127.0.0.1:{app_port}"]]
    if gateway_port is not None:
        commands.append([cli, "serve", "--bg", f"--https={HTTPS_GATEWAY_PORT}", f"http://127.0.0.1:{gateway_port}"])
    return commands


def off_commands(cli: str, gateway: bool = True) -> list[list[str]]:
    """Stop publishing what serve_commands published, and nothing else you may have set up with Tailscale Serve."""
    commands = [[cli, "serve", f"--https={HTTPS_APP_PORT}", "off"]]
    if gateway:
        commands.append([cli, "serve", f"--https={HTTPS_GATEWAY_PORT}", "off"])
    return commands


def reset_command(cli: str) -> list[str]:
    """Clears every Tailscale Serve rule (also ones that are not Rectifi's). The fallback when `off` does not work."""
    return [cli, "serve", "reset"]


def status_command(cli: str) -> list[str]:
    return [cli, "status", "--json"]


def serve_status_command(cli: str) -> list[str]:
    return [cli, "serve", "status", "--json"]


def dns_name(status: dict | None) -> str | None:
    """This laptop's MagicDNS name from `tailscale status --json` (Self.DNSName, without the trailing dot).
    None when Tailscale is not running or the name is missing."""
    if not isinstance(status, dict):
        return None
    if status.get("BackendState") not in (None, "Running"):
        return None
    name = (status.get("Self") or {}).get("DNSName")
    if not isinstance(name, str):
        return None
    name = name.strip().rstrip(".")
    return name or None


def app_url(name: str) -> str:
    return f"https://{name}/" if HTTPS_APP_PORT == 443 else f"https://{name}:{HTTPS_APP_PORT}/"


def gateway_url(name: str) -> str:
    """The address to type into the phone app (the SMS to URL Forwarder), e.g. https://laptop.tail1234.ts.net:8443/sms"""
    return f"https://{name}:{HTTPS_GATEWAY_PORT}/sms"


def origin(name: str) -> str:
    """The browser origin of the app (scheme and host, no path), for Streamlit's corsAllowedOrigins."""
    return f"https://{name}"


def _run_json(command: list[str], runner: Callable = subprocess.run, timeout: float = 5.0) -> dict | None:
    try:
        done = runner(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def read_status(cli: str | None, runner: Callable = subprocess.run) -> dict | None:
    """`tailscale status --json` as a dict, or None (not installed, not running, or unreadable)."""
    return _run_json(status_command(cli), runner) if cli else None


def served_ports(serve_status: dict | None) -> set[int]:
    """The HTTPS ports `tailscale serve status --json` reports as published ({443, 8443}, say). Reads the "Web" map,
    whose keys look like "laptop.tail1234.ts.net:443"; anything it cannot read is ignored."""
    ports: set[int] = set()
    if not isinstance(serve_status, dict):
        return ports
    web = serve_status.get("Web")
    for key in web if isinstance(web, dict) else ():
        _, _, port = str(key).rpartition(":")
        if port.isdigit():
            ports.add(int(port))
    return ports


def read_served_ports(cli: str | None, runner: Callable = subprocess.run) -> set[int]:
    return served_ports(_run_json(serve_status_command(cli), runner)) if cli else set()


FUNNEL_MESSAGE = "Funnel is on: set RECTIFI_PUBLIC=1 and run `python scripts/dev.py set-password` first"


def funnel_exposes_app(serve_status: dict | None, app_port: int = APP_PORT) -> bool:
    """True when `tailscale serve status --json` shows Funnel (the public internet) switched on for a web address that
    proxies to the app port. Funnel keys are "host:port" in the "AllowFunnel" map; the target is that key's "Web" handler.
    A shape this cannot read counts as "not funnel" (it never guesses), and nothing is logged."""
    if not isinstance(serve_status, dict):
        return False
    allow, web = serve_status.get("AllowFunnel"), serve_status.get("Web")
    if not isinstance(allow, dict) or not isinstance(web, dict):
        return False
    for key, on in allow.items():
        if on is not True:
            continue
        handlers = (web.get(key) or {}).get("Handlers") if isinstance(web.get(key), dict) else None
        for handler in handlers.values() if isinstance(handlers, dict) else ():
            proxy = handler.get("Proxy") if isinstance(handler, dict) else None
            port = proxy.rsplit(":", 1)[-1].split("/", 1)[0] if isinstance(proxy, str) else ""
            if port.isdigit() and int(port) == app_port:
                return True
    return False


def read_funnel_exposes_app(cli: str | None, app_port: int = APP_PORT, runner: Callable = subprocess.run) -> bool:
    """False when Tailscale is not installed, not running or unreadable."""
    return funnel_exposes_app(_run_json(serve_status_command(cli), runner), app_port) if cli else False


def run_visible(command: list[str], runner: Callable = subprocess.run, timeout: float = 60.0) -> int:
    """Run a command with its output going straight to the terminal (Tailscale may print a link to enable HTTPS,
    or ask a question). Returns the exit code, or 127 if the program can't be started, or 124 on a timeout."""
    try:
        return runner(command, timeout=timeout).returncode
    except FileNotFoundError:
        return 127
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 126
