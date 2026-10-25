"""Rectifi helper. One command for every chore, the same on Windows and macOS.

    python scripts/dev.py setup        make .venv, install packages, create .env   (macOS: python3)
    python scripts/dev.py db           start Postgres with Docker
    python scripts/dev.py init         create the tables
    python scripts/dev.py doctor       check that everything is ready, and say what to fix
    python scripts/dev.py app          open the app on http://localhost:8501 (add --streamlit for the old Streamlit app)
    python scripts/dev.py up           db + init + app in one go (skips Docker if your Postgres is already up);
                                       also starts the phone SMS gateway when RECTIFI_SMS_GATEWAY=1 is in .env
    python scripts/dev.py build-web    rebuild the web app in web/dist (needs Node 20.19+; running the app never does)
    python scripts/dev.py web          develop the web app: API on :8501 with reload + Vite on http://127.0.0.1:5173 (needs Node)
    python scripts/dev.py phone        run only the phone SMS gateway (127.0.0.1 only, port RECTIFI_SMS_PORT, default 8765)
    python scripts/dev.py tailscale    put the app (and the gateway, if on) on your phone through Tailscale Serve (HTTPS, your devices only)
    python scripts/dev.py tailscale off   take them off again
    python scripts/dev.py tailscale status   show the https addresses
    python scripts/dev.py demo         app with fake data on http://localhost:8502 (never touches your real data; --streamlit for the old app)
    python scripts/dev.py free-models  list OpenRouter free models that support tool calling (needs internet)
    python scripts/dev.py test         run the tests
    python scripts/dev.py mcp          run the MCP server (Claude Desktop starts this for you)
    python scripts/dev.py mcp-config   print the config to paste into Claude Desktop

This file uses only the Python standard library, so it works before you have installed anything.
It never prints passwords or API keys.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
DEFAULT_URL = "postgresql://rectifi:rectifi_dev@localhost:5432/rectifi"
MIN_PYTHON = (3, 11)             # every pinned package supports 3.11+; 3.13 is the version the tests ran on
DEMO_PORT = "8502"
APP_PORT = "8501"
SMS_DEFAULT_PORT = 8765
# Both the app and the phone gateway listen on this laptop only. The phone reaches them through `dev.py tailscale`
# (Tailscale Serve: HTTPS, only devices signed in to your own tailnet). 127.0.0.1 and not "localhost": on Windows
# "localhost" can mean ::1, and Tailscale Serve proxies to the IPv4 address.
DEFAULT_GATEWAY_HOST = "127.0.0.1"
LOCAL_ONLY = ["--server.address", "127.0.0.1"]


# ───────────────────────── small helpers ─────────────────────────

def say(text: str = "") -> None:
    print(text, flush=True)


def project_python() -> str:
    """The Python that has Rectifi's packages: the .venv one if it exists, otherwise the one running this script."""
    candidate = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(candidate) if candidate.exists() else sys.executable


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"          # Windows: read and write files as UTF-8, not cp1252
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.update(extra or {})
    return env


def run(cmd: list[str], extra_env: dict[str, str] | None = None, check: bool = True) -> int:
    """Run a command (a list, never a shell string) in the project folder."""
    result = subprocess.run(cmd, cwd=ROOT, env=child_env(extra_env))
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result.returncode


def read_env_file() -> dict[str, str]:
    """A tiny .env reader (KEY=value lines). Real environment variables win over the file."""
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name) or read_env_file().get(name) or default


def database_url() -> str:
    return setting("DATABASE_URL", DEFAULT_URL)


def safe_target(url: str) -> str:
    """host:port/dbname, with no user and no password."""
    parts = urlsplit(url)
    return f"{parts.hostname or 'localhost'}:{parts.port or 5432}/{parts.path.lstrip('/')}"


def with_database(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + dbname, parts.query, parts.fragment))


# ───────────────────────── commands ─────────────────────────

def cmd_setup(_args) -> None:
    if sys.version_info < MIN_PYTHON:
        sys.exit(f"Rectifi needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer (3.13 is what it is tested on). "
                 f"You are running {sys.version.split()[0]}. Install Python 3.13 from python.org, then run this again.")
    if not VENV.exists():
        say("Creating the virtual environment in .venv ...")
        run([sys.executable, "-m", "venv", str(VENV)])
    say("Installing packages (this takes a few minutes the first time) ...")
    run([project_python(), "-m", "pip", "install", "--upgrade", "pip"])
    run([project_python(), "-m", "pip", "install", "-r", "requirements.txt"])
    env_file = ROOT / ".env"
    if not env_file.exists():
        shutil.copyfile(ROOT / ".env.example", env_file)
        say("Created .env from .env.example. Open it if you want to change the model or the database.")
    say("")
    say("Setup done. Next:")
    say("  1. python scripts/dev.py db       (starts Postgres; needs Docker Desktop running)")
    say("  2. python scripts/dev.py up       (or just run this, it does db + init + app)")
    say("  Not sure something is right? python scripts/dev.py doctor")


def database_reachable() -> bool:
    code, _ = _quiet([project_python(), "-c", CHECK_DB])
    return code == 0


def cmd_db(_args) -> None:
    if shutil.which("docker") is None:
        sys.exit("Docker is not installed or not on your PATH.\n"
                 "Using your own PostgreSQL 16? On Windows run:  powershell -ExecutionPolicy Bypass -File scripts\\setup_windows.ps1\n"
                 "(macOS/Linux: README, 'Path B'.) Otherwise install Docker Desktop.")
    say("Starting Postgres in Docker ...")
    code = run(["docker", "compose", "up", "-d", "--wait"], check=False)
    if code != 0:
        sys.exit("Docker could not start the database. Is Docker Desktop open and running? "
                 "Run `docker compose logs db` to see why.")
    say(f"Postgres is ready at {safe_target(database_url())}.")


def cmd_init(_args) -> None:
    run([project_python(), "-c", "from rectifi.db import init_db; init_db(); print('Tables are ready.')"])


def streamlit_env() -> dict[str, str]:
    """Extra environment for Streamlit. If Tailscale is running, its https address is added to the origins Streamlit accepts
    (same address, so same-origin already works; this only covers a proxy that rewrites the Host header)."""
    try:
        origin = tailscale_origin()
    except Exception:                       # a Tailscale problem must never stop the app from starting
        origin = None
    return {"STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS": origin} if origin else {}


WEB_DIR = ROOT / "web"
WEB_DIST = WEB_DIR / "dist"
NODE_MIN = (20, 19)               # Vite 7's floor


def api_command(port: str, reload: bool = False) -> list[str]:
    """The web app's server: uvicorn on rectifi.api.app:create_app, this laptop only. A list, never a shell string.
    One worker on purpose: pending uploads and Ask locks live in memory."""
    command = [project_python(), "-m", "uvicorn", "--factory", "rectifi.api.app:create_app",
               "--host", DEFAULT_GATEWAY_HOST, "--port", port, "--log-level", "warning"]
    if reload:
        command += ["--reload", "--reload-dir", "rectifi"]
    return command


def api_env() -> dict[str, str]:
    """RECTIFI_ALLOWED_ORIGINS: if Tailscale is running, its https address is an accepted browser origin for changes."""
    try:
        origin = tailscale_origin()
    except Exception:                       # a Tailscale problem must never stop the app from starting
        origin = None
    return {"RECTIFI_ALLOWED_ORIGINS": origin} if origin else {}


def web_is_built() -> bool:
    return (WEB_DIST / "index.html").is_file()


def run_web_app(port: str, extra_env: dict[str, str] | None = None) -> None:
    if not web_is_built():
        sys.exit("The web app isn't built yet (web/dist/index.html is missing).\n"
                 "  Build it (needs Node 20.19 or newer):  python scripts/dev.py build-web\n"
                 "  Or use the old app:                    python scripts/dev.py up --streamlit")
    say(f"Rectifi on http://localhost:{port}  (this laptop only). Press Ctrl+C to stop.")
    run(api_command(port), {**api_env(), **(extra_env or {})})


def cmd_app(args) -> None:
    if getattr(args, "streamlit", False):
        run([project_python(), "-m", "streamlit", "run", "app/streamlit_app.py", "--server.port", APP_PORT, *LOCAL_ONLY], streamlit_env())
    else:
        run_web_app(APP_PORT)


# ───────────────────────── phone SMS gateway ─────────────────────────

def gateway_enabled() -> bool:
    return setting("RECTIFI_SMS_GATEWAY").strip().lower() in ("1", "true", "yes", "on")


def gateway_port() -> int:
    try:
        port = int(setting("RECTIFI_SMS_PORT", str(SMS_DEFAULT_PORT)).strip())
    except ValueError:
        return SMS_DEFAULT_PORT
    return port if 1 <= port <= 65535 else SMS_DEFAULT_PORT


def gateway_host() -> str:
    """RECTIFI_SMS_HOST, default 127.0.0.1 (this laptop only). Anything that is not an IP address falls back to the default.
    0.0.0.0 opens the gateway to the whole Wi-Fi: an advanced option, protected only by the device token."""
    value = setting("RECTIFI_SMS_HOST", DEFAULT_GATEWAY_HOST).strip() or DEFAULT_GATEWAY_HOST
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return DEFAULT_GATEWAY_HOST
    return value


def gateway_is_open() -> bool:
    return not ipaddress.ip_address(gateway_host()).is_loopback


def gateway_command(port: int) -> list[str]:
    """The command that runs the gateway: uvicorn on rectifi.sms_gateway:create_app. A list, never a shell string."""
    return [project_python(), "-m", "uvicorn", "--factory", "rectifi.sms_gateway:create_app",
            "--host", gateway_host(), "--port", str(port)]


def gateway_listening(port: int) -> bool:
    """Does a Rectifi gateway already answer on this laptop? (GET /health: no login, no data.)"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0) as reply:
            return reply.status == 200 and json.load(reply).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_gateway() -> subprocess.Popen | None:
    """Start the gateway as a child process when RECTIFI_SMS_GATEWAY=1. Returns the process, or None."""
    if not gateway_enabled():
        return None
    port = gateway_port()
    if gateway_listening(port):
        say(f"Phone gateway: already answering on port {port}. (Not starting a second one.)")
        return None
    if gateway_is_open():
        say(f"Phone gateway: starting on {gateway_host()}:{port} (RECTIFI_SMS_GATEWAY=1). WARNING: that is open to everyone on your "
            "Wi-Fi and the device token is the only lock. Safer: remove RECTIFI_SMS_HOST from .env and use `python scripts/dev.py tailscale`.")
    else:
        say(f"Phone gateway: starting on {gateway_host()}:{port} (this laptop only; `python scripts/dev.py tailscale` lets your phone reach it).")
    process = subprocess.Popen(gateway_command(port), cwd=ROOT, env=child_env())
    time.sleep(1.5)
    if process.poll() is not None:
        say(f"Phone gateway: it stopped right away. Is port {port} already in use? Change RECTIFI_SMS_PORT in .env. The app starts without it.")
        return None
    return process


def stop_gateway(process: subprocess.Popen | None) -> None:
    """Stop the child gateway. Safe to call twice, and when Ctrl+C already stopped it (Windows sends Ctrl+C to children too)."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def cmd_phone(_args) -> None:
    port = gateway_port()
    say(f"Phone SMS gateway on {gateway_host()}:{port}. Press Ctrl+C to stop.")
    if gateway_is_open():
        say("WARNING: this address is open to everyone on your network, and the device token is the only lock.")
    else:
        say("It listens on this laptop only. Run `python scripts/dev.py tailscale` so your phone can reach it over HTTPS.")
    say("Pair a phone on the Phone page of the app.")
    sys.exit(run(gateway_command(port), check=False))


def cmd_up(args) -> None:
    if database_reachable():
        say(f"Postgres is already running at {safe_target(database_url())}. (Docker not needed.)")
    else:
        cmd_db(args)
    cmd_init(args)
    gateway = start_gateway()
    try:
        cmd_app(args)
    finally:
        stop_gateway(gateway)       # also runs on Ctrl+C, so the gateway never keeps running after the app is closed


# ───────────────────────── Tailscale Serve: the app and the gateway on your phone ─────────────────────────

def load_tailscale_helpers():
    """rectifi/tailscale.py, loaded by path: it only uses the standard library, so this works before anything is installed."""
    spec = importlib.util.spec_from_file_location("rectifi_tailscale", ROOT / "rectifi" / "tailscale.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rectifi_tailscale"] = module          # dataclass-free module, but keeps `inspect` and tracebacks happy
    spec.loader.exec_module(module)
    return module


def tailscale_origin() -> str | None:
    """https://<this laptop>.<tailnet>.ts.net if Tailscale is installed and running, else None. Quick and quiet:
    used so Streamlit accepts the browser origin of the Tailscale address even if the proxy changes the Host header."""
    ts = load_tailscale_helpers()
    cli = ts.find_cli()
    name = ts.dns_name(ts.read_status(cli)) if cli else None
    return ts.origin(name) if name else None


def cmd_tailscale(args) -> None:
    ts = load_tailscale_helpers()
    action = args.action or "on"
    cli = ts.find_cli()
    if cli is None:
        sys.exit("Tailscale is not installed (looked on PATH and in C:\\Program Files\\Tailscale).\n"
                 "Install it from https://tailscale.com/download on this laptop AND on your phone, sign in to the same account "
                 "on both, then run this again.")
    if action == "status":
        name = ts.dns_name(ts.read_status(cli))
        if name is None:
            sys.exit("Tailscale is installed but not running or not signed in. Open the Tailscale app, sign in, then try again.")
        ports = ts.read_served_ports(cli)
        say(f"This laptop on your tailnet: {name}")
        say(f"  App      {ts.app_url(name)}   {'(published)' if ts.HTTPS_APP_PORT in ports else '(not published: run `python scripts/dev.py tailscale`)'}")
        say(f"  Gateway  {ts.gateway_url(name)}   {'(published)' if ts.HTTPS_GATEWAY_PORT in ports else '(not published)'}")
        return
    gateway_on = gateway_enabled()
    if action == "off":
        failed = False
        for command in ts.off_commands(cli, gateway=True):
            say("$ " + " ".join(command))
            if ts.run_visible(command) != 0:
                failed = True
        if failed:
            say("\nOne of those did not work. To clear every Tailscale Serve rule on this laptop (also ones that are not Rectifi's), run:")
            say("  " + " ".join(ts.reset_command(cli)))
            sys.exit(1)
        say("\nRectifi is no longer published on your tailnet.")
        return
    status = ts.read_status(cli)
    name = ts.dns_name(status)
    if name is None:
        sys.exit("Tailscale is installed but not running or not signed in. Open the Tailscale app, sign in (same account as on "
                 "your phone), then run this again.")
    if not gateway_on:
        say("The phone SMS gateway is off (RECTIFI_SMS_GATEWAY is not 1 in .env), so only the app is published. "
            "If you want bank SMS from your phone: set RECTIFI_SMS_GATEWAY=1, restart `dev.py up`, run this again.")
    commands = ts.serve_commands(cli, int(APP_PORT), gateway_port() if gateway_on else None)
    for command in commands:
        say("$ " + " ".join(command))
        code = ts.run_visible(command)
        if code != 0:
            sys.exit(f"\nThat command failed (exit code {code}). If Tailscale printed a link, open it: HTTPS has to be switched on "
                     "once for your tailnet (admin console, DNS page, 'Enable HTTPS'). Then run this again.")
    say("")
    say("Done. Open these on your phone (Tailscale must be switched on there, signed in to the same account):")
    say(f"  App      {ts.app_url(name)}")
    if gateway_on:
        say(f"  Gateway  {ts.gateway_url(name)}    <- this is the address for the SMS forwarder app")
    say("Only devices signed in to your tailnet can reach these. Take them off again with: python scripts/dev.py tailscale off")
    say("Note: HTTPS certificates are logged publicly with this laptop's name. If the name is private to you, rename the laptop first.")


def cmd_test(args) -> None:
    sys.exit(run([project_python(), "-m", "pytest", "-q", *args.pytest_args], check=False))


def cmd_mcp(_args) -> None:
    sys.exit(run([project_python(), "-m", "rectifi.mcp_server"], check=False))


def cmd_mcp_config(_args) -> None:
    """JSON for claude_desktop_config.json. No password in it: the server reads DATABASE_URL from your .env."""
    config = {
        "mcpServers": {
            "rectifi": {
                "command": project_python(),
                "args": ["-m", "rectifi.mcp_server"],
                "env": {"PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"},
            }
        }
    }
    print(json.dumps(config, indent=2))
    print("Paste the \"rectifi\" entry into the \"mcpServers\" section of claude_desktop_config.json, "
          "then restart Claude Desktop. See README.md for where that file is.", file=sys.stderr)


def cmd_demo(args) -> None:
    """Fake data in a separate database (rectifi_demo), shown on its own port. Your real data is never touched."""
    demo_url = with_database(database_url(), "rectifi_demo")
    env = {"DATABASE_URL": demo_url}
    # Make sure the demo database exists (older Docker volumes were created before init.sql knew about it).
    create = (
        "import psycopg, os\n"
        "base = os.environ['BASE_URL']\n"
        "with psycopg.connect(base, autocommit=True, connect_timeout=3) as c:\n"
        "    if not c.execute(\"SELECT 1 FROM pg_database WHERE datname = 'rectifi_demo'\").fetchone():\n"
        "        c.execute('CREATE DATABASE rectifi_demo')\n"
    )
    run([project_python(), "-c", create], {"BASE_URL": database_url()})
    run([project_python(), "scripts/seed_demo.py"], env)
    say(f"Demo app on http://localhost:{DEMO_PORT}  (database {safe_target(demo_url)}). Press Ctrl+C to stop.")
    if getattr(args, "streamlit", False):
        run([project_python(), "-m", "streamlit", "run", "app/streamlit_app.py", "--server.port", DEMO_PORT, *LOCAL_ONLY], env)
    else:
        run_web_app(DEMO_PORT, env)


# ───────────────────────── the web app: build and develop (Node needed only here) ─────────────────────────

WEB_BINARY_SUFFIXES = {".png", ".ico", ".jpg", ".jpeg", ".woff", ".woff2", ".gif", ".webp"}
WEB_SOURCE_FILES = ["index.html", "package.json", "package-lock.json", "vite.config.ts"]


def web_source_files() -> list[Path]:
    """Every file the web build depends on: web/src/**, web/public/**, index.html, package.json, the lockfile, vite.config.ts
    and tsconfig*.json. Sorted by POSIX path so the hash is the same on every machine."""
    found: list[Path] = []
    for folder in ("src", "public"):
        base = WEB_DIR / folder
        if base.is_dir():
            found += [p for p in base.rglob("*") if p.is_file()]
    found += [WEB_DIR / name for name in WEB_SOURCE_FILES if (WEB_DIR / name).is_file()]
    found += [p for p in WEB_DIR.glob("tsconfig*.json") if p.is_file()]
    return sorted(set(found), key=lambda p: p.relative_to(WEB_DIR).as_posix())


def web_source_hash() -> str:
    """sha256 over the sorted relative paths and file contents. CRLF is read as LF (text files only), so Windows git
    autocrlf can't make the committed web/dist look out of date."""
    digest = hashlib.sha256()
    for path in web_source_files():
        data = path.read_bytes()
        if path.suffix.lower() not in WEB_BINARY_SUFFIXES:
            data = data.replace(b"\r\n", b"\n")
        digest.update(path.relative_to(WEB_DIR).as_posix().encode("utf-8") + b"\0" + data + b"\0")
    return digest.hexdigest()


def node_version() -> tuple[int, int, int] | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        done = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
        parts = done.stdout.strip().lstrip("v").split(".")
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def need_node() -> str:
    """The npm command, after checking that Node is new enough. Exits with advice if not."""
    version = node_version()
    npm = shutil.which("npm")
    if version is None or npm is None:
        sys.exit("Node.js is not installed (or not on your PATH). Only rebuilding the web app needs it; running Rectifi does not.\n"
                 "Install the LTS version from https://nodejs.org (20.19 or newer), open a new terminal, and run this again.")
    if version[:2] < NODE_MIN:
        sys.exit(f"Node {version[0]}.{version[1]}.{version[2]} is too old: the web build needs {NODE_MIN[0]}.{NODE_MIN[1]} or newer. "
                 "Install the LTS version from https://nodejs.org.")
    return npm


def npm_install_needed() -> bool:
    modules = WEB_DIR / "node_modules"
    lock = WEB_DIR / "package-lock.json"
    return not modules.is_dir() or (lock.exists() and modules.stat().st_mtime < lock.stat().st_mtime)


def npm(npm_path: str, *args: str, extra_env: dict[str, str] | None = None) -> int:
    result = subprocess.run([npm_path, *args], cwd=WEB_DIR, env=child_env(extra_env))
    return result.returncode


def cmd_build_web(_args) -> None:
    """npm ci (when needed), the TypeScript build, the web tests, then web/dist/build-info.json. Commit web/dist afterwards."""
    npm_path = need_node()
    version = node_version()
    if npm_install_needed():
        say("Installing the web packages (npm ci) ...")
        if npm(npm_path, "ci") != 0:
            sys.exit("npm ci failed. Check your internet connection, then run this again.")
    say("Building the web app ...")
    if npm(npm_path, "run", "build") != 0:
        sys.exit("The web build failed. The messages above say which file and line.")
    say("Running the web tests ...")
    if npm(npm_path, "test") != 0:
        sys.exit("A web test failed. The messages above say which one.")
    info = {"source_hash": web_source_hash(), "node": ".".join(str(n) for n in version)}
    (WEB_DIST / "build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    say("Done. web/dist is up to date: commit it, so the laptop needs only Python.")


def cmd_web(_args) -> None:
    """Develop the web app: the API on :8501 (restarts when Python changes) and Vite on http://127.0.0.1:5173 (hot reload)."""
    npm_path = need_node()
    if npm_install_needed():
        say("Installing the web packages (npm ci) ...")
        if npm(npm_path, "ci") != 0:
            sys.exit("npm ci failed. Check your internet connection, then run this again.")
    say("API on http://127.0.0.1:8501, web app on http://127.0.0.1:5173 (open this one). Press Ctrl+C to stop both.")
    children = [
        subprocess.Popen(api_command(APP_PORT, reload=True), cwd=ROOT, env=child_env(api_env())),
        subprocess.Popen([npm_path, "run", "dev"], cwd=WEB_DIR, env=child_env()),
    ]
    try:
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
    finally:
        for child in children:
            stop_gateway(child)         # the same "stop a child, tolerate it being gone already" helper


# ───────────────────────── free models ─────────────────────────

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def free_tool_models(payload: dict) -> list[tuple[str, int | None]]:
    """From OpenRouter's /models JSON: (id, context_length) for every ':free' model that supports tool calling.

    The agent cannot work without tool calling, so models without it are left out. Sorted by id."""
    found = []
    for item in payload.get("data", []):
        model_id = str(item.get("id", ""))
        if model_id.endswith(":free") and "tools" in (item.get("supported_parameters") or []):
            found.append((model_id, item.get("context_length")))
    return sorted(found)


def cmd_free_models(_args) -> None:
    say(f"Asking {OPENROUTER_MODELS_URL} ...")
    try:
        request = urllib.request.Request(OPENROUTER_MODELS_URL, headers={"User-Agent": "rectifi-dev"})
        with urllib.request.urlopen(request, timeout=20) as reply:
            payload = json.load(reply)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sys.exit(f"Could not get the model list ({type(exc).__name__}). Check your internet connection and try again. "
                 "You can also browse https://openrouter.ai/models and filter by Tools and a price of 0.")
    models = free_tool_models(payload)
    if not models:
        sys.exit("OpenRouter lists no free models with tool calling right now. Try again later, or use Ollama.")
    say(f"\n{len(models)} free models that support tool calling (the list changes often):\n")
    for model_id, context in models:
        say(f"  {model_id:<60} context {context if context else '?'}")
    say("\nPut one in .env, then restart the app:")
    say("  LLM_PROVIDER=openrouter")
    say("  OPENROUTER_API_KEY=<your key from openrouter.ai/keys>")
    say(f"  OPENROUTER_MODEL={models[0][0]}   <- replace with the id you chose")
    say("Free limits: about 20 requests a minute and 50 a day (1,000 a day after you buy 10 credits).")


# ───────────────────────── doctor ─────────────────────────

CHECK_PACKAGES = """
import re, sys
from importlib.metadata import version, PackageNotFoundError
bad = []
for line in open('requirements.txt', encoding='utf-8'):
    line = line.split('#')[0].strip()
    m = re.fullmatch(r'([A-Za-z0-9_.\\-]+)(?:\\[[^\\]]+\\])?==(\\S+)', line)
    if not m:
        continue
    try:
        have = version(m[1])
    except PackageNotFoundError:
        bad.append(m[1] + ' (missing)')
        continue
    if have != m[2]:
        bad.append(m[1] + ' ' + have + ' (wanted ' + m[2] + ')')
if bad:
    print('; '.join(bad)); sys.exit(1)
"""

CHECK_DB = """
import sys
import psycopg
from rectifi import config
try:
    with psycopg.connect(config.database_url(), connect_timeout=3) as c:
        c.execute('SELECT 1')
except psycopg.Error as exc:
    print(type(exc).__name__); sys.exit(1)
"""


class Report:
    def __init__(self) -> None:
        self.failed = False

    def ok(self, what: str) -> None:
        say(f"  ok    {what}")

    def warn(self, what: str, fix: str) -> None:
        say(f"  WARN  {what}\n        fix: {fix}")

    def fail(self, what: str, fix: str) -> None:
        self.failed = True
        say(f"  FAIL  {what}\n        fix: {fix}")


def _quiet(cmd: list[str], extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    result = subprocess.run(cmd, cwd=ROOT, env=child_env(extra_env), capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.returncode, (result.stdout + result.stderr).strip()


def cmd_doctor(_args) -> None:
    r = Report()
    py = project_python()
    say("Rectifi doctor\n")

    code, out = _quiet([py, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
    version = out.splitlines()[-1] if out else "?"
    if code == 0 and tuple(int(x) for x in version.split(".")[:2]) >= MIN_PYTHON:
        r.ok(f"Python {version}" + ("" if version.startswith("3.13") else " (works, but Rectifi was tested on 3.13)"))
    else:
        r.fail(f"Python {version} (need {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer)",
               "Install Python 3.13 from python.org, then run `python scripts/dev.py setup`.")

    code, out = _quiet([py, "-c", CHECK_PACKAGES])
    if code == 0:
        r.ok("Packages match requirements.txt")
    else:
        r.fail(f"Packages: {out.splitlines()[-1] if out else 'could not check'}", "Run `python scripts/dev.py setup`.")

    if (ROOT / ".env").exists():
        r.ok(".env found")
    else:
        r.warn(".env not found (defaults are used)", "Copy .env.example to .env if you want to change settings.")

    url = database_url()
    code, _ = _quiet([py, "-c", CHECK_DB])
    if code == 0:
        r.ok(f"Database reachable at {safe_target(url)}")
    else:
        r.fail(f"Can't reach the database at {safe_target(url)}", "Run `python scripts/dev.py db` (Docker), or start your own Postgres and check DATABASE_URL in .env.")

    test_url = os.environ.get("TEST_DATABASE_URL") or with_database(url, "rectifi_test")
    code, _ = _quiet([py, "-c", CHECK_DB], {"DATABASE_URL": test_url})
    if code == 0:
        r.ok(f"Test database reachable at {safe_target(test_url)}")
    else:
        r.warn(f"Test database not reachable at {safe_target(test_url)} (only the tests need it)",
               "Run `python scripts/dev.py db`, or run db/init.sql once on your own Postgres (README, 'Path B').")

    provider = setting("LLM_PROVIDER", "ollama").lower()
    if provider == "deepseek":
        if setting("DEEPSEEK_API_KEY"):
            r.ok("LLM: deepseek, API key is set")
        else:
            r.warn("LLM: deepseek, but DEEPSEEK_API_KEY is empty (the Ask page will be off)", "Put your key in .env, or set LLM_PROVIDER=ollama.")
    elif provider == "ollama":
        base = setting("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        model = setting("OLLAMA_MODEL", "qwen2.5:7b")
        try:
            with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as reply:
                names = [m.get("name", "") for m in json.load(reply).get("models", [])]
            if model in names or (":" not in model and model in {n.split(":")[0] for n in names}):
                r.ok(f"LLM: ollama, model {model} is ready")
            else:
                r.warn(f"LLM: ollama is running but {model} is not downloaded (the Ask page will be off)", f"Run `ollama pull {model}`.")
        except (urllib.error.URLError, OSError, ValueError):
            r.warn(f"LLM: ollama is not running at {base} (the Ask page will be off; the rest of Rectifi works)",
                   "Open the Ollama app, or set LLM_PROVIDER=openrouter or deepseek in .env.")
    elif provider in ("openrouter", "huggingface", "openai_compat"):
        names = {"openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", ""),
                 "huggingface": ("HF_TOKEN", "HF_MODEL", "deepseek-ai/DeepSeek-V3.1:cheapest"),
                 "openai_compat": ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL", "")}[provider]
        key_name, model_name, model_default = names
        model = setting(model_name, model_default)
        missing = [n for n, v in ((key_name, setting(key_name)), (model_name, model)) if not v]
        if provider == "openai_compat" and not setting("OPENAI_COMPAT_BASE_URL"):
            missing.append("OPENAI_COMPAT_BASE_URL")
        if missing:
            fix = "Put " + ", ".join(missing) + " in .env."
            if provider == "openrouter":
                fix += " `python scripts/dev.py free-models` lists models you can use."
            r.warn(f"LLM: {provider}, but {', '.join(missing)} is empty (the Ask page will be off)", fix)
        else:
            r.ok(f"LLM: {provider}, model {model}, key is set (not tested: that needs the internet)")
        if provider == "huggingface":
            r.warn("LLM: Hugging Face is not free on a free account ($0 credit)",
                   "You need PRO or purchased credits, or the Ask page fails with HTTP 402. Ollama or OpenRouter :free cost nothing.")
    else:
        r.warn(f"LLM_PROVIDER={provider!r} is not known (the Ask page will be off)",
               "Use ollama, deepseek, openrouter, huggingface or openai_compat in .env.")

    if gateway_enabled() and gateway_is_open():
        r.warn(f"Phone gateway listens on {gateway_host()}, which is open to everyone on your network (only the device token protects it)",
               "Remove RECTIFI_SMS_HOST from .env (the default 127.0.0.1 is this laptop only) and use `python scripts/dev.py tailscale` for your phone.")
    if gateway_enabled():
        port = gateway_port()
        if gateway_listening(port):
            r.ok(f"Phone gateway: on, answering on port {port}")
        else:
            r.warn(f"Phone gateway is turned on in .env but is not answering on port {port} (it starts together with `dev.py up`)",
                   "Run `python scripts/dev.py up` (or `python scripts/dev.py phone` on its own). If the port is busy, change RECTIFI_SMS_PORT in .env.")
    else:
        r.ok("Phone gateway: off (the default). To capture bank SMS from your phone, see README: Auto-capture bank SMS")

    ts = load_tailscale_helpers()
    ts_cli = ts.find_cli()
    if ts_cli is None:
        r.ok("Tailscale: not installed (only needed to use Rectifi on your phone: README, 'Use Rectifi on your phone')")
    elif ts.dns_name(ts.read_status(ts_cli)):
        r.ok("Tailscale: installed and running (`python scripts/dev.py tailscale status` shows your https addresses)")
    else:
        r.warn("Tailscale is installed but not running or not signed in", "Open the Tailscale app and sign in. Only needed for phone access.")

    if web_is_built():
        r.ok("Web app is built (web/dist)")
        try:
            built = json.loads((WEB_DIST / "build-info.json").read_text(encoding="utf-8")).get("source_hash")
        except (OSError, ValueError):
            built = None
        if built == web_source_hash():
            r.ok("Web app is up to date with web/src")
        else:
            r.warn("web/dist is older than web/src (or has no build-info.json)",
                   "If you changed web/src, run `python scripts/dev.py build-web` (needs Node 20.19+). Running the app needs no Node.")
    else:
        r.fail("Web app is not built (web/dist/index.html is missing)",
               "Run `python scripts/dev.py build-web` (needs Node 20.19+), or use the old app: `python scripts/dev.py up --streamlit`.")

    if shutil.which("docker"):
        r.ok("Docker is installed")
    else:
        r.warn("Docker not found", "Fine if you run your own Postgres. Otherwise install Docker Desktop.")

    say("")
    if r.failed:
        say("Not ready yet. Fix the FAIL lines above, then run doctor again.")
        sys.exit(1)
    say("Ready. Run `python scripts/dev.py up` to open the app.")


# ───────────────────────── entry point ─────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(prog="dev.py", description="Rectifi helper commands.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn, help_text in [
        ("setup", cmd_setup, "make .venv, install packages, create .env"),
        ("db", cmd_db, "start Postgres with Docker"),
        ("init", cmd_init, "create the tables"),
        ("doctor", cmd_doctor, "check that everything is ready"),
        ("app", cmd_app, "run the app on port 8501"),
        ("up", cmd_up, "db + init + app (+ the phone gateway if RECTIFI_SMS_GATEWAY=1)"),
        ("phone", cmd_phone, "run the phone SMS gateway on its own"),
        ("demo", cmd_demo, "run the app with fake data on port 8502"),
        ("build-web", cmd_build_web, "rebuild web/dist (needs Node 20.19+)"),
        ("web", cmd_web, "develop the web app: API with reload + Vite (needs Node)"),
        ("free-models", cmd_free_models, "list OpenRouter free models that support tools"),
        ("mcp", cmd_mcp, "run the MCP server"),
        ("mcp-config", cmd_mcp_config, "print the Claude Desktop config"),
    ]:
        p = sub.add_parser(name, help=help_text)
        if name in ("app", "up", "demo"):
            p.add_argument("--streamlit", action="store_true", help="run the old Streamlit app instead of the web app")
        p.set_defaults(func=fn)
    tail = sub.add_parser("tailscale", help="publish the app (and the gateway) on your tailnet with Tailscale Serve")
    tail.add_argument("action", nargs="?", choices=["on", "off", "status"], default="on")
    tail.set_defaults(func=cmd_tailscale)
    test = sub.add_parser("test", help="run the tests")
    test.add_argument("pytest_args", nargs=argparse.REMAINDER, help="extra arguments for pytest")
    test.set_defaults(func=cmd_test)
    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:       # Ctrl+C: `up` has already stopped its children in a finally block
        say("\nStopped.")
        sys.exit(130)


if __name__ == "__main__":
    main()
