"""The hosting files, checked without a Docker daemon (a build and a real run are not possible here), and dev.py's
set-password / deploy-check. dockerfile-parse, hadolint and docker are optional: those checks skip when missing."""

import importlib.util
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

from rectifi.api import auth

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "deploy" / "Dockerfile"
COMPOSE = ROOT / "deploy" / "compose.oracle.yml"
CLOUD_INIT = ROOT / "deploy" / "cloud-init.yaml"
SECRET_WORDS = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "HASH")


def _load_deploy():
    spec = importlib.util.spec_from_file_location("rectifi_devtools_deploy", ROOT / "scripts" / "devtools" / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = _load_deploy()
COMPOSE_DOC = yaml.safe_load(COMPOSE.read_text())


# ── Dockerfile ──

@pytest.fixture(scope="module")
def parsed():
    dockerfile_parse = pytest.importorskip("dockerfile_parse")
    return dockerfile_parse.DockerfileParser(path=str(DOCKERFILE)).structure


def _instructions(structure, name):
    return [i for i in structure if i["instruction"] == name]


def test_dockerfile_runs_as_a_non_root_user(parsed):
    users = _instructions(parsed, "USER")
    assert users and users[-1]["value"].split(":")[0] not in ("root", "0")


def test_dockerfile_has_a_healthcheck_on_the_health_route(parsed):
    checks = _instructions(parsed, "HEALTHCHECK")
    assert checks and "/api/health" in checks[0]["value"]


def test_dockerfile_starts_one_worker_and_trusts_no_proxy_headers(parsed):
    cmd = _instructions(parsed, "CMD")[-1]["value"]
    assert "--workers" in cmd and '"1"' in cmd and "--factory" in cmd and "rectifi.api.app:create_app" in cmd
    assert '"--no-proxy-headers"' in cmd and "forwarded-allow-ips" not in cmd           # uvicorn's own default would trust 127.0.0.1


def test_dockerfile_bakes_in_no_secret_and_downloads_nothing_with_add(parsed):
    for env in _instructions(parsed, "ENV"):
        for word in SECRET_WORDS:
            assert word not in env["value"].upper().replace("RECTIFI_PUBLIC", ""), env["value"]
    for add in _instructions(parsed, "ADD"):
        assert not add["value"].lower().startswith(("http://", "https://"))
    text = DOCKERFILE.read_text()
    assert "RECTIFI_PUBLIC=1" in text and "RECTIFI_BIND_HOST=0.0.0.0" in text
    assert "curl" not in text.lower().replace("curl-", "")


def test_dockerfile_uses_a_pinned_slim_arm_capable_base(parsed):
    base = _instructions(parsed, "FROM")[0]["value"]
    assert base == "python:3.13-slim-bookworm"


def test_dockerfile_copies_only_what_the_app_needs(parsed):
    copies = [c["value"] for c in _instructions(parsed, "COPY")]
    joined = " ".join(copies)
    assert "rectifi/" in joined and "web/dist/" in joined
    assert ".env" not in joined and "statements" not in joined


def test_hadolint_is_clean():
    exe = shutil.which("hadolint")
    if not exe:
        pytest.skip("hadolint is not installed (pip install hadolint-py, dev only)")
    result = subprocess.run([exe, "--failure-threshold", "warning", str(DOCKERFILE)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# ── .dockerignore ──

def test_dockerignore_keeps_secrets_and_statements_out():
    lines = {ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines()}
    assert ".env" in lines and ".env.*" in lines
    assert {".git", "web/node_modules", "statements"} <= lines
    assert {"*.csv", "*.xlsx", "*.pdf"} <= lines and "!tests/fixtures/**" in lines


# ── compose ──

def test_only_the_app_publishes_a_port_and_only_on_loopback():
    for name, service in COMPOSE_DOC["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port}"
        if name not in ("app", "gateway"):
            assert "ports" not in service, f"{name} must have no published ports"
    assert COMPOSE_DOC["services"]["app"]["ports"] == ["127.0.0.1:8501:8501"]
    assert COMPOSE_DOC["services"]["gateway"]["ports"] == ["127.0.0.1:8765:8765"]
    assert "0.0.0.0:" not in COMPOSE.read_text()      # no port is ever published on every interface
    for service in COMPOSE_DOC["services"].values():
        assert service.get("network_mode") != "host" and not service.get("privileged")


def test_app_is_locked_down():
    app = COMPOSE_DOC["services"]["app"]
    assert app["read_only"] is True and any(t.split(":")[0] == "/tmp" for t in app["tmpfs"])
    assert app["environment"]["RECTIFI_HOME"].startswith("/tmp/")      # the chart sandbox font cache must be inside the writable tmpfs
    assert app["mem_limit"] == "2g" and app["cap_drop"] == ["ALL"] and "no-new-privileges:true" in app["security_opt"]
    assert app["depends_on"]["db"]["condition"] == "service_healthy"
    assert app["env_file"] == "../.env.deploy"
    assert app["build"] == {"context": "..", "dockerfile": "deploy/Dockerfile"}
    env = app["environment"]
    assert "@db:5432/rectifi" in env["DATABASE_URL"] and env["OLLAMA_BASE_URL"] == "http://ollama:11434"
    assert "127.0.0.1" in env["RECTIFI_ALLOWED_HOSTS"], "the container's own health check sends Host: 127.0.0.1"


def test_services_restart_and_the_pull_job_runs_once():
    services = COMPOSE_DOC["services"]
    for name in ("db", "ollama", "app", "backup"):
        assert services[name]["restart"] == "unless-stopped"
    assert services["ollama-pull"]["restart"] == "no"
    pull = " ".join(services["ollama-pull"]["command"])
    assert all(m in pull for m in ("qwen2.5:3b", "qwen2.5-coder:3b", "nomic-embed-text"))
    assert services["ollama-pull"]["environment"]["OLLAMA_HOST"] == "ollama:11434"


def test_db_and_ollama_are_configured_as_planned():
    db, ollama = COMPOSE_DOC["services"]["db"], COMPOSE_DOC["services"]["ollama"]
    assert db["image"] == "postgres:16-bookworm" and "pgdata:/var/lib/postgresql/data" in db["volumes"]
    assert "pg_isready" in " ".join(db["healthcheck"]["test"])
    assert ollama["environment"]["OLLAMA_KEEP_ALIVE"] == "-1" and ollama["environment"]["OLLAMA_NUM_PARALLEL"] == "1"
    assert ollama["environment"]["OLLAMA_MAX_LOADED_MODELS"] == "2" and ollama["mem_limit"] == "8g"
    assert ":latest" not in ollama["image"] and "ollama/ollama:" in ollama["image"]


def test_backup_keeps_seven_dumps():
    backup = COMPOSE_DOC["services"]["backup"]
    script = " ".join(backup["command"])
    assert "pg_dump -h db -U postgres" in script and "-U rectifi" not in script      # a plain role cannot dump forced tables
    assert backup["environment"]["PGPASSWORD"] == "${POSTGRES_PASSWORD}"
    assert "pg_dump" in script and "-Fc" in script and "tail -n +8" in script
    assert any(v.endswith(":/backups") for v in backup["volumes"])


def test_gateway_is_opt_in_locked_down_and_runs_the_gateway_module():
    gw = COMPOSE_DOC["services"]["gateway"]
    assert gw["profiles"] == ["gateway"]                       # off unless asked for (rule 8)
    assert gw["command"] == ["python", "-m", "rectifi.sms_gateway"]
    assert gw["build"] == COMPOSE_DOC["services"]["app"]["build"]
    assert gw["read_only"] is True and gw["cap_drop"] == ["ALL"] and "no-new-privileges:true" in gw["security_opt"]
    assert gw["environment"]["RECTIFI_SMS_PORT"] == "8765"
    assert gw["environment"]["RECTIFI_SMS_HOST"] == "0.0.0.0"   # inside its container only; the host side is 127.0.0.1 (test above)
    assert "@db:5432/rectifi" in gw["environment"]["DATABASE_URL"] and gw["environment"]["DATABASE_URL"].startswith("postgresql://rectifi:")
    assert gw["depends_on"]["db"]["condition"] == "service_healthy"
    assert "/health" in " ".join(gw["healthcheck"]["test"])
    assert gw["restart"] == "unless-stopped"


def test_the_app_never_logs_in_as_the_superuser():
    for name in ("app", "gateway"):
        url = COMPOSE_DOC["services"][name]["environment"]["DATABASE_URL"]
        assert url.startswith("postgresql://rectifi:${RECTIFI_DB_PASSWORD}@"), name
    assert COMPOSE_DOC["services"]["db"]["environment"]["POSTGRES_USER"] == "postgres"


def test_no_literal_secret_in_compose():
    """Every password/secret value in compose is a ${VARIABLE} (or the shell's $$ in the backup script), never a literal."""
    for name, service in COMPOSE_DOC["services"].items():
        for key, value in (service.get("environment") or {}).items():
            if any(w in key.upper() for w in ("PASSWORD", "SECRET", "TOKEN", "KEY")):
                assert str(value).startswith("${"), f"{name}.{key} must come from .env.deploy"


@pytest.fixture
def stand_in_env_deploy():
    """compose reads ROOT/.env.deploy for the app service: make a throwaway one and remove it again."""
    target = ROOT / ".env.deploy"
    if target.exists():
        pytest.skip("a real .env.deploy is here; not touching it")
    target.write_text("RECTIFI_HOSTNAME=r.tail1.ts.net\n")
    try:
        yield target
    finally:
        target.unlink(missing_ok=True)


def test_compose_config_accepts_the_file(tmp_path, stand_in_env_deploy):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("docker is not installed")
    env = tmp_path / "env"
    env.write_text("RECTIFI_HOSTNAME=r.tail1.ts.net\nPOSTGRES_PASSWORD=abc123abc123abc123\nRECTIFI_DB_PASSWORD=def456def456def456\n")
    result = subprocess.run([docker, "compose", "--env-file", str(env), "-f", str(COMPOSE), "config", "--format", "json"],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    import json
    rendered = json.loads(result.stdout)
    app = rendered["services"]["app"]
    assert app["environment"]["DATABASE_URL"] == "postgresql://rectifi:def456def456def456@db:5432/rectifi"     # the app logs in as rectifi, not as postgres
    assert rendered["services"]["db"]["environment"]["POSTGRES_USER"] == "postgres"
    assert app["environment"]["RECTIFI_ALLOWED_HOSTS"] == "r.tail1.ts.net,localhost,127.0.0.1"
    assert [p["host_ip"] for p in app["ports"]] == ["127.0.0.1"]


def test_deploy_check_runs_compose_on_the_default_env_file(stand_in_env_deploy, capsys):
    if not shutil.which("docker"):
        pytest.skip("docker is not installed")
    stand_in_env_deploy.write_text("RECTIFI_HOSTNAME=r.tail1.ts.net\nPOSTGRES_PASSWORD=abc123abc123abc123\nRECTIFI_DB_PASSWORD=def456def456def456\n")
    rep = deploy.Report()
    deploy.check_compose(rep, ROOT, stand_in_env_deploy)
    assert rep.failures == 0, capsys.readouterr().out


def test_compose_config_refuses_without_the_required_settings(tmp_path, stand_in_env_deploy):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("docker is not installed")
    env = tmp_path / "empty"
    env.write_text("")
    result = subprocess.run([docker, "compose", "--env-file", str(env), "-f", str(COMPOSE), "config", "--quiet"],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode != 0 and ("POSTGRES_PASSWORD" in result.stderr or "RECTIFI_HOSTNAME" in result.stderr)


# ── cloud-init ──

def test_cloud_init_is_valid_yaml_and_opens_no_ports():
    text = CLOUD_INIT.read_text()
    assert text.startswith("#cloud-config")
    doc = yaml.safe_load(text)
    commands = " ".join(c if isinstance(c, str) else " ".join(map(str, c)) for c in doc["runcmd"])
    assert "docker-compose-plugin" in commands and "tailscale" in commands and "4G" in commands and "/swapfile" in commands
    assert "unattended-upgrades" in doc["packages"]
    lowered = text.lower()
    for forbidden in ("ufw allow", "iptables", "firewall-cmd", "| sh", "|sh", "| bash", "--accept-risk"):
        assert forbidden not in lowered, forbidden


# ── env example and docs ──

def test_env_deploy_example_has_no_real_values_and_covers_the_compose_variables():
    values = dotenv_values(ROOT / ".env.deploy.example")
    assert values["POSTGRES_PASSWORD"] in ("", None) and values["RECTIFI_PASSWORD_HASH"] in ("", None)
    assert values["RECTIFI_SESSION_SECRET"] in ("", None)
    assert values["RECTIFI_HOSTNAME"].endswith(".ts.net")
    compose_text = COMPOSE.read_text()
    assert values["RECTIFI_DB_PASSWORD"] in ("", None)
    for name in ("RECTIFI_HOSTNAME", "POSTGRES_PASSWORD", "RECTIFI_DB_PASSWORD"):
        assert name in compose_text and name in values
    assert values["RECTIFI_MULTIUSER"] in ("", None)           # off in the example; the guide says when to turn it on
    plain = (ROOT / ".env.example").read_text()
    for name in ("RECTIFI_MULTIUSER", "RECTIFI_SESSION_SECRET", "RECTIFI_SMS_PORT"):
        assert name in plain, name
    assert "RECTIFI_DB_PASSWORD" in plain
    assert not [ln for ln in plain.splitlines() if ln.strip().startswith("RECTIFI_MULTIUSER=") and ln.strip() != "RECTIFI_MULTIUSER="]


def test_hosting_guide_covers_the_steps():
    text = (ROOT / "docs" / "HOSTING.md").read_text()
    for needle in ("tailscale serve --bg 8501", "tailscale funnel --bg 8501", "set-password", "deploy-check", "pg_restore",
                   "Honest limits", "Out of host capacity", "cloud-init", "up -d --build", "git pull"):
        assert needle in text, needle


# ── set-password ──

def _answers(monkeypatch, *typed):
    queue = list(typed)
    monkeypatch.setattr(deploy.getpass, "getpass", lambda prompt="": queue.pop(0))
    return queue


def test_hash_made_by_the_dev_tool_is_accepted_by_the_server_code():
    stored = deploy.hash_password("a long enough password")
    assert deploy.HASH_RE.match(stored) and auth.parse_hash(stored) is not None
    assert auth.verify_password("a long enough password", stored) and not auth.verify_password("another password!!", stored)
    assert auth.hash_password("a long enough password", salt=b"0123456789abcdef") == deploy.hash_password("a long enough password", salt=b"0123456789abcdef")


def test_set_password_writes_env_keeps_other_lines_and_never_prints_the_password(tmp_path, monkeypatch, capsys):
    target = tmp_path / ".env"
    target.write_text("DATABASE_URL=postgresql://x\nRECTIFI_PASSWORD_HASH=old\n# keep me\n")
    _answers(monkeypatch, "short", "tortoise lantern marble", "tortoise lantern marble")
    deploy.set_password(["--file", str(target)])
    out = capsys.readouterr()
    assert "tortoise" not in out.out + out.err
    values = dotenv_values(target)
    assert values["DATABASE_URL"] == "postgresql://x" and "# keep me" in target.read_text()
    assert target.read_text().count("RECTIFI_PASSWORD_HASH=") == 1 and values["RECTIFI_PASSWORD_HASH"] != "old"
    assert auth.verify_password("tortoise lantern marble", values["RECTIFI_PASSWORD_HASH"])
    assert len(values["RECTIFI_SESSION_SECRET"]) >= auth.MIN_SECRET_LENGTH
    assert "tortoise" not in target.read_text()


def test_set_password_creates_a_private_file(tmp_path, monkeypatch):
    target = tmp_path / "new.env"
    _answers(monkeypatch, "tortoise lantern marble", "tortoise lantern marble")
    deploy.set_password(["--file", str(target)])
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_set_password_print_mode_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    _answers(monkeypatch, "tortoise lantern marble", "tortoise lantern marble")
    deploy.set_password(["--print"])
    out = capsys.readouterr().out
    assert "RECTIFI_PASSWORD_HASH='scrypt$16384$8$1$" in out and "RECTIFI_SESSION_SECRET=" in out and "tortoise" not in out
    assert list(tmp_path.iterdir()) == []


def test_set_password_gives_up_after_three_bad_tries(tmp_path, monkeypatch):
    _answers(monkeypatch, "a", "b", "c")
    with pytest.raises(SystemExit):
        deploy.set_password(["--file", str(tmp_path / ".env")])
    assert not (tmp_path / ".env").exists()


def test_a_mismatch_asks_again(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    _answers(monkeypatch, "tortoise lantern marble", "different one", "tortoise lantern marble", "tortoise lantern marble")
    deploy.set_password(["--file", str(target)])
    assert target.exists()


def test_the_written_hash_works_through_the_real_app(tmp_path, monkeypatch):
    """set-password -> .env -> the app: the whole loop, with the file read the way config reads it."""
    from starlette.testclient import TestClient

    from rectifi.api.app import create_app
    from rectifi.api.security import install_session_lock

    target = tmp_path / ".env"
    _answers(monkeypatch, "tortoise lantern marble", "tortoise lantern marble")
    deploy.set_password(["--file", str(target)])
    for k, v in dotenv_values(target).items():
        monkeypatch.setenv(k, v)
    auth.limiter.reset_memory()
    app = create_app()
    install_session_lock(app)
    client = TestClient(app, base_url="http://127.0.0.1:8501", headers={"X-Rectifi-Request": "1"})
    assert client.post("/api/auth/login", json={"password": "tortoise lantern marble"}).status_code == 200
    assert client.get("/api/health").status_code == 200
    auth.limiter.reset_memory()


# ── deploy-check ──

GOOD = {
    "RECTIFI_HOSTNAME": "rectifi.tail1a2b3c.ts.net", "POSTGRES_PASSWORD": "a1b2c3d4e5f6a7b8c9d0e1f2",
    "RECTIFI_DB_PASSWORD": "f9e8d7c6b5a4f3e2d1c0b9a8",
    "RECTIFI_PASSWORD_HASH": f"'{deploy.hash_password('a long enough password')}'", "RECTIFI_SESSION_SECRET": "z" * 48,
}


def _env_file(tmp_path, **override):
    values = {**GOOD, **override}
    path = tmp_path / ".env.deploy"
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    path.chmod(0o600)
    return path


def _run_check(path):
    with pytest.raises(SystemExit) as raised:
        deploy.deploy_check(["--env-file", str(path)])
        raise SystemExit(0)
    return raised.value.code


def test_deploy_check_passes_with_good_settings(tmp_path, capsys):
    path = _env_file(tmp_path)
    assert _run_check(path) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert "scrypt" not in out and GOOD["POSTGRES_PASSWORD"] not in out and "z" * 10 not in out


@pytest.mark.parametrize("override,needle", [
    ({"RECTIFI_HOSTNAME": ""}, "RECTIFI_HOSTNAME"),
    ({"RECTIFI_HOSTNAME": "https://rectifi.x.ts.net/"}, "RECTIFI_HOSTNAME"),
    ({"RECTIFI_HOSTNAME": "rectifi.your-tailnet.ts.net"}, "example"),
    ({"POSTGRES_PASSWORD": ""}, "POSTGRES_PASSWORD"),
    ({"POSTGRES_PASSWORD": "short"}, "POSTGRES_PASSWORD"),
    ({"POSTGRES_PASSWORD": "has/slash/and@at-signs-1234"}, "letters and numbers"),
    ({"RECTIFI_PASSWORD_HASH": ""}, "RECTIFI_PASSWORD_HASH"),
    ({"RECTIFI_PASSWORD_HASH": "plaintextpassword"}, "RECTIFI_PASSWORD_HASH"),
    ({"RECTIFI_SESSION_SECRET": "tooshort"}, "RECTIFI_SESSION_SECRET"),
    ({"RECTIFI_DB_PASSWORD": ""}, "RECTIFI_DB_PASSWORD"),
    ({"RECTIFI_DB_PASSWORD": "short"}, "RECTIFI_DB_PASSWORD"),
    ({"RECTIFI_DB_PASSWORD": "has/slash/and@at-signs-1234"}, "letters and numbers"),
    ({"RECTIFI_DB_PASSWORD": GOOD["POSTGRES_PASSWORD"]}, "different from POSTGRES_PASSWORD"),
])
def test_deploy_check_fails_on_each_bad_setting(tmp_path, capsys, override, needle):
    path = _env_file(tmp_path, **override)
    assert _run_check(path) == 1
    assert needle in capsys.readouterr().out


def test_deploy_check_fails_when_the_env_file_is_missing(tmp_path, capsys):
    assert _run_check(tmp_path / "nope") == 1
    assert "does not exist" in capsys.readouterr().out


def test_multiuser_needs_the_session_secret_but_not_the_password_hash(tmp_path, capsys):
    path = _env_file(tmp_path, RECTIFI_MULTIUSER="1", RECTIFI_PASSWORD_HASH="", RECTIFI_SESSION_SECRET="s" * 40,
                     RECTIFI_BLIND_KEY="b" * 64)
    assert _run_check(path) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and "s" * 30 not in out and "b" * 30 not in out


@pytest.mark.parametrize("blind_key", ["", "b" * 31])
def test_multiuser_fails_without_a_long_enough_blind_key(tmp_path, capsys, blind_key):
    path = _env_file(tmp_path, RECTIFI_MULTIUSER="1", RECTIFI_PASSWORD_HASH="", RECTIFI_SESSION_SECRET="s" * 40,
                     RECTIFI_BLIND_KEY=blind_key)
    assert _run_check(path) == 1
    out = capsys.readouterr().out
    assert "FAIL  RECTIFI_BLIND_KEY" in out and "b" * 30 not in out


def test_multiuser_rejects_a_short_session_secret(tmp_path, capsys):
    path = _env_file(tmp_path, RECTIFI_MULTIUSER="1", RECTIFI_PASSWORD_HASH="", RECTIFI_SESSION_SECRET="s" * 31)
    assert _run_check(path) == 1
    out = capsys.readouterr().out
    assert "RECTIFI_SESSION_SECRET" in out and "openssl rand -hex 32" in out and "s" * 31 not in out


def test_deploy_check_never_prints_either_database_password(tmp_path, capsys):
    path = _env_file(tmp_path, RECTIFI_DB_PASSWORD=GOOD["POSTGRES_PASSWORD"])
    _run_check(path)
    assert GOOD["POSTGRES_PASSWORD"] not in capsys.readouterr().out
    path = _env_file(tmp_path)
    _run_check(path)
    assert GOOD["RECTIFI_DB_PASSWORD"] not in capsys.readouterr().out


def test_deploy_check_refuses_a_compose_port_on_every_interface(tmp_path, monkeypatch, capsys):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "compose.oracle.yml").write_text('services:\n  app:\n    ports:\n      - "8501:8501"\n')
    rep = deploy.Report()
    deploy.check_files(rep, tmp_path)
    assert rep.failures >= 1 and "127.0.0.1 only" in capsys.readouterr().out


def test_deploy_check_warns_about_a_readable_env_file(tmp_path, capsys):
    if os.name != "posix":
        pytest.skip("file modes are a POSIX idea")
    path = _env_file(tmp_path)
    path.chmod(0o644)
    _run_check(path)
    assert "chmod 600" in capsys.readouterr().out


def test_read_env_handles_quotes_and_comments(tmp_path):
    f = tmp_path / "e"
    f.write_text("# c\nA=1\nB='x$y'\nC=\"q\"\nD=v # note\n\nexport E=5\n")
    assert deploy.read_env(f) == {"A": "1", "B": "x$y", "C": "q", "D": "v", "E": "5"}


def test_devpy_lists_both_commands():
    assert set(deploy.COMMANDS) == {"set-password", "deploy-check"}


def test_dev_py_delegates_to_these_commands():
    """dev.py loads scripts/devtools/deploy.py by path; the help line and the command both reach it."""
    dev = [__import__("sys").executable, str(ROOT / "scripts" / "dev.py")]
    helped = subprocess.run(dev + ["--help"], capture_output=True, text=True)
    assert "set-password" in helped.stdout and "deploy-check" in helped.stdout
    checked = subprocess.run(dev + ["deploy-check", "--", "--env-file", "/nonexistent/.env.deploy"], capture_output=True, text=True)
    assert checked.returncode == 1 and "Hosting files" in checked.stdout and "does not exist" in checked.stdout
    direct = subprocess.run([__import__("sys").executable, str(ROOT / "scripts" / "devtools" / "deploy.py"), "deploy-check",
                             "--env-file", "/nonexistent/.env.deploy"], capture_output=True, text=True)
    assert direct.returncode == 1 and "does not exist" in direct.stdout
