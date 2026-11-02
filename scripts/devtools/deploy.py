"""dev.py commands for hosting: set-password, deploy-check. Standard library only; dev.py loads this file by path.

    python scripts/dev.py set-password            ask for a password, write RECTIFI_PASSWORD_HASH and RECTIFI_SESSION_SECRET to .env
    python scripts/dev.py set-password --print    print the two lines instead (paste them into .env.deploy)
    python scripts/dev.py set-password --file F   write them into file F
    python scripts/dev.py deploy-check            check the hosting files and .env.deploy before you deploy
    python scripts/dev.py deploy-check --env-file F

Each command is a function(args: list[str]) -> None. Never print a password. No shell=True.
The hash format matches rectifi/api/auth.py (tests/test_deploy_files.py proves the two agree).
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
MIN_SECRET_LENGTH = 32
TRUE_WORDS = ("1", "true", "yes", "on")
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN, SALT_BYTES = 2**14, 8, 1, 32, 16
HASH_RE = re.compile(r"^scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9_-]+\$[A-Za-z0-9_-]+$")
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")

COMPOSE_FILE = "deploy/compose.oracle.yml"
HOSTING_FILES = ("deploy/Dockerfile", COMPOSE_FILE, "deploy/cloud-init.yaml", ".dockerignore", ".env.deploy.example", "docs/HOSTING.md")


# ───────────────────────── hashing (same format as rectifi/api/auth.py) ─────────────────────────

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN,
                            maxmem=128 * SCRYPT_R * (SCRYPT_N + SCRYPT_P + 2) + (1 << 20))
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters. A few random words in a row works well."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Use at most {MAX_PASSWORD_LENGTH} characters."
    return None


def env_lines(password: str) -> dict[str, str]:
    """The two settings, ready to write. The hash is single-quoted because it holds `$` (Docker Compose and shells would
    otherwise try to expand it)."""
    return {"RECTIFI_PASSWORD_HASH": f"'{hash_password(password)}'", "RECTIFI_SESSION_SECRET": secrets.token_urlsafe(48)}


# ───────────────────────── env files ─────────────────────────

def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines. Handles quotes and comments; does not expand anything."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        values[key.strip().removeprefix("export ").strip()] = value
    return values


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=VALUE lines in `path`: replace a line that already sets the key, else append. Other lines are kept.
    The file is written whole and moved into place, and a new file is readable by its owner only."""
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    done: set[str] = set()
    out: list[str] = []
    for line in existing:
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            if key not in done:
                out.append(f"{key}={updates[key]}")
                done.add(key)
            continue
        out.append(line)
    for key, value in updates.items():
        if key not in done:
            out.append(f"{key}={value}")
    mode = (path.stat().st_mode & 0o777) if path.is_file() else 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _option(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
        raise SystemExit(f"{name} needs a value.")
    return None


# ───────────────────────── set-password ─────────────────────────

def set_password(args: list[str]) -> None:
    if "-h" in args or "--help" in args:
        print(__doc__)
        return
    show = "--print" in args
    target = Path(_option(args, "--file") or ROOT / ".env")
    print("Choose the password that protects Rectifi when it is online.")
    print(f"At least {MIN_PASSWORD_LENGTH} characters. Nothing you type is shown, and the password itself is never saved:")
    print("only a scrambled copy (a hash) is.\n")
    for _ in range(3):
        first = getpass.getpass("New password: ")
        problem = password_problem(first)
        if problem:
            print(problem)
            continue
        if getpass.getpass("Type it again: ") != first:
            print("Those two did not match. Try again.")
            continue
        break
    else:
        raise SystemExit("No password was set.")
    values = env_lines(first)
    del first
    if show:
        print("\nPaste these two lines into .env.deploy (replace the empty ones):\n")
        for key, value in values.items():
            print(f"{key}={value}")
        print("\nChanging the password changes the session secret too, so everyone who was signed in is signed out.")
        return
    write_env_values(target, values)
    print(f"\nSaved to {target}. Restart Rectifi for it to take effect.")
    print("From now on every /api call needs a sign-in. Remove the two lines to go back to no password on your laptop.")


# ───────────────────────── deploy-check ─────────────────────────

class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, text: str) -> None:
        print(f"  ok    {text}")

    def warn(self, text: str) -> None:
        self.warnings += 1
        print(f"  WARN  {text}")

    def fail(self, text: str) -> None:
        self.failures += 1
        print(f"  FAIL  {text}")


def check_files(rep: Report, root: Path) -> None:
    print("Hosting files")
    for rel in HOSTING_FILES:
        (rep.ok if (root / rel).is_file() else rep.fail)(f"{rel}" if (root / rel).is_file() else f"{rel} is missing")
    ignore = root / ".dockerignore"
    if ignore.is_file():
        lines = {ln.strip() for ln in ignore.read_text(encoding="utf-8").splitlines()}
        (rep.ok if ".env" in lines or ".env*" in lines else rep.fail)(".dockerignore keeps .env files out of the image"
                                                                      if (".env" in lines or ".env*" in lines) else ".dockerignore does not list .env")
    compose = root / COMPOSE_FILE
    if compose.is_file():
        bad = [ln.strip() for ln in compose.read_text(encoding="utf-8").splitlines()
               if re.match(r"""^\s*-\s*["']?(\d|0\.0\.0\.0|\[?::)""", ln) and "127.0.0.1:" not in ln]
        (rep.fail if bad else rep.ok)(f"a published port is not on 127.0.0.1 only: {bad[0]}" if bad else "every published port is on 127.0.0.1 only")
    if not (root / "web" / "dist" / "index.html").is_file():
        rep.fail("web/dist/index.html is missing: the image would have no web app. Run `python scripts/dev.py build-web` and commit web/dist.")


def check_env(rep: Report, root: Path, env_path: Path) -> dict[str, str] | None:
    print(f"\nSettings in {env_path.name}")
    if not env_path.is_file():
        rep.fail(f"{env_path} does not exist. Copy .env.deploy.example to {env_path.name} and fill it in.")
        return None
    env = read_env(env_path)
    mode = env_path.stat().st_mode & 0o777
    if os.name == "posix":
        (rep.ok if mode & 0o077 == 0 else rep.warn)(
            "only you can read this file" if mode & 0o077 == 0 else f"other users on this machine can read {env_path.name}: run chmod 600 {env_path}")
    host = env.get("RECTIFI_HOSTNAME", "")
    if not host:
        rep.fail("RECTIFI_HOSTNAME is empty. Use the name from `tailscale status`, like rectifi.tail1a2b3c.ts.net")
    elif host.endswith("your-tailnet.ts.net"):
        rep.fail("RECTIFI_HOSTNAME is still the example value.")
    elif not HOST_RE.match(host):
        rep.fail("RECTIFI_HOSTNAME should be just a name like rectifi.tail1a2b3c.ts.net (no https://, no slash, no port).")
    else:
        rep.ok(f"RECTIFI_HOSTNAME = {host}")
    pw = env.get("POSTGRES_PASSWORD", "")
    if not pw:
        rep.fail("POSTGRES_PASSWORD is empty. Make one with: openssl rand -hex 24")
    elif not re.fullmatch(r"[A-Za-z0-9]+", pw):
        rep.fail("POSTGRES_PASSWORD may use letters and numbers only (it goes inside a web address).")
    elif len(pw) < 16:
        rep.fail("POSTGRES_PASSWORD is shorter than 16 characters.")
    else:
        rep.ok("POSTGRES_PASSWORD is set")
    db_pw = env.get("RECTIFI_DB_PASSWORD", "")
    if not db_pw:
        rep.fail("RECTIFI_DB_PASSWORD is empty. It is the app's own database login (not the superuser). Make one with: openssl rand -hex 24")
    elif not re.fullmatch(r"[A-Za-z0-9]+", db_pw):
        rep.fail("RECTIFI_DB_PASSWORD may use letters and numbers only (it goes inside a web address).")
    elif len(db_pw) < 16:
        rep.fail("RECTIFI_DB_PASSWORD is shorter than 16 characters.")
    elif db_pw == pw:
        rep.fail("RECTIFI_DB_PASSWORD must be different from POSTGRES_PASSWORD. The app must not log in as the superuser.")
    else:
        rep.ok("RECTIFI_DB_PASSWORD is set and differs from POSTGRES_PASSWORD")
    multiuser = env.get("RECTIFI_MULTIUSER", "").strip().lower() in TRUE_WORDS
    if multiuser:
        rep.ok("RECTIFI_MULTIUSER is on: invite sign-up, no shared password (RECTIFI_PASSWORD_HASH is ignored)")
    else:
        stored = env.get("RECTIFI_PASSWORD_HASH", "")
        if not HASH_RE.match(stored):
            rep.fail("RECTIFI_PASSWORD_HASH is missing or not valid. Run `python scripts/dev.py set-password --print`.")
        else:
            rep.ok("RECTIFI_PASSWORD_HASH looks right")
    if len(env.get("RECTIFI_SESSION_SECRET", "")) < MIN_SECRET_LENGTH:
        how = "Make one with: openssl rand -hex 32" if multiuser else "`set-password --print` makes one."
        rep.fail(f"RECTIFI_SESSION_SECRET is missing or shorter than {MIN_SECRET_LENGTH} characters. {how}")
    else:
        rep.ok("RECTIFI_SESSION_SECRET is set")
    return env


def check_git_ignore(rep: Report, root: Path, env_path: Path) -> None:
    git = shutil.which("git")
    if not git or not (root / ".git").exists():
        return
    try:
        inside = env_path.resolve().is_relative_to(root.resolve())
    except OSError:
        inside = False
    if not inside:
        return
    result = subprocess.run([git, "check-ignore", "-q", str(env_path)], cwd=root, capture_output=True)
    (rep.ok if result.returncode == 0 else rep.fail)(
        f"git ignores {env_path.name}" if result.returncode == 0
        else f"git does NOT ignore {env_path.name}: a `git add .` would publish your secrets. Add `{env_path.name}` to .gitignore.")


def check_compose(rep: Report, root: Path, env_path: Path) -> None:
    print("\nDocker Compose")
    if env_path.resolve() != (root / ".env.deploy").resolve():
        rep.warn("the compose file always reads .env.deploy in the project folder, so it was not checked against another file.")
        return
    docker = shutil.which("docker")
    if not docker:
        rep.warn("docker is not installed here, so the compose file was not checked. Run this again on the server.")
        return
    result = subprocess.run([docker, "compose", "--env-file", str(env_path), "-f", str(root / COMPOSE_FILE), "config", "--quiet"],
                            cwd=root, capture_output=True, text=True)
    if result.returncode == 0:
        rep.ok("`docker compose config` accepts the file and your settings")
    else:
        last = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
        rep.fail(f"`docker compose config` failed: {last}")


def deploy_check(args: list[str]) -> None:
    if "-h" in args or "--help" in args:
        print(__doc__)
        return
    root = ROOT
    env_path = Path(_option(args, "--env-file") or root / ".env.deploy")
    rep = Report()
    check_files(rep, root)
    env = check_env(rep, root, env_path)
    if env is not None:
        check_git_ignore(rep, root, env_path)
        check_compose(rep, root, env_path)
    print()
    if rep.failures:
        print(f"{rep.failures} problem(s) to fix before you deploy" + (f", {rep.warnings} warning(s)." if rep.warnings else "."))
        sys.exit(1)
    print("Ready to deploy." + (f" ({rep.warnings} warning(s) above.)" if rep.warnings else ""))


# dev.py reads this: command name -> (function, one-line help)
COMMANDS = {
    "set-password": (set_password, "choose the password that protects Rectifi when it is online"),
    "deploy-check": (deploy_check, "check the hosting files and settings before you deploy"),
}


if __name__ == "__main__":
    # `python scripts/devtools/deploy.py set-password --print` also works, without going through dev.py
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit("usage: deploy.py {" + "|".join(COMMANDS) + "} [options]")
    COMMANDS[sys.argv[1]][0](sys.argv[2:])
