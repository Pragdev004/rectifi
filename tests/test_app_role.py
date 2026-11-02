"""The app logs in as a plain role (decision D11), and row level security really applies to it.

A superuser or a BYPASSRLS role skips RLS silently, so this file proves the role by its attributes AND by what it can see.
The Docker files and setup_windows.ps1 are checked statically: there is no Docker on this laptop."""

import re
from pathlib import Path

import psycopg
import pytest
import yaml
from support.tenants import populate

from rectifi import config, db

ROOT = Path(__file__).resolve().parent.parent


def test_the_app_role_is_neither_superuser_nor_bypassrls():
    row = psycopg.connect(config.database_url()).execute(
        "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    assert row[1] is False and row[2] is False, f"{row[0]} must be a plain role"
    assert db.role_problems() == [] and db.assert_app_role() == []


def test_rls_really_applies_to_the_owner_role_without_the_setting():
    """By behaviour, not attributes: the same login that owns the tables sees zero rows of a table that has rows."""
    populate(1)
    with psycopg.connect(config.database_url()) as raw:                      # no app.user_id
        owns = raw.execute("SELECT pg_get_userbyid(relowner) = current_user FROM pg_class WHERE relname = 'transactions'").fetchone()[0]
        assert owns, "the test login should own the tables (that is the case FORCE row level security exists for)"
        for table in db.TENANT_TABLES:
            assert raw.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0, table
        with pytest.raises(psycopg.Error):
            raw.execute("INSERT INTO accounts (name) VALUES ('x')")
    with psycopg.connect(config.database_url(), options="-c app.user_id=1") as raw:
        assert raw.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
    with psycopg.connect(config.database_url(), options="-c app.user_id=2") as raw:
        assert raw.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


def test_without_force_the_owner_would_see_everything_which_is_why_force_is_on():
    """Proves the claim behind FORCE: turn it off on a scratch copy of the situation and the owner sees the rows."""
    populate(1)
    with db.connect_system("test: toggle force") as c:
        c.execute("ALTER TABLE accounts NO FORCE ROW LEVEL SECURITY")
    try:
        with psycopg.connect(config.database_url()) as raw:
            assert raw.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1       # the owner skips RLS without FORCE
    finally:
        with db.connect_system("test: restore force") as c:
            c.execute("ALTER TABLE accounts FORCE ROW LEVEL SECURITY")
    with psycopg.connect(config.database_url()) as raw:
        assert raw.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0


def test_assert_app_role_refuses_a_superuser_in_multiuser_mode(monkeypatch):
    monkeypatch.setattr(db, "_role_attributes", lambda conn: {"rolname": "rectifi", "rolsuper": True, "rolbypassrls": False})
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    with pytest.raises(db.UnsafeDatabaseRole, match="superuser"):
        db.assert_app_role()
    with pytest.raises(db.UnsafeDatabaseRole):
        db.init_db()                                      # and the server does not start
    monkeypatch.setattr(db, "_role_attributes", lambda conn: {"rolname": "rectifi", "rolsuper": False, "rolbypassrls": True})
    with pytest.raises(db.UnsafeDatabaseRole, match="BYPASSRLS"):
        db.assert_app_role()


def test_assert_app_role_refuses_a_superuser_in_public_mode(monkeypatch):
    monkeypatch.setattr(db, "_role_attributes", lambda conn: {"rolname": "rectifi", "rolsuper": True, "rolbypassrls": False})
    monkeypatch.setenv("RECTIFI_PUBLIC", "1")
    with pytest.raises(db.UnsafeDatabaseRole):
        db.assert_app_role()


def test_laptop_mode_only_warns_about_a_superuser(monkeypatch, caplog):
    monkeypatch.setattr(db, "_role_attributes", lambda conn: {"rolname": "rectifi", "rolsuper": True, "rolbypassrls": False})
    with caplog.at_level("WARNING", logger="rectifi.db"):
        problems = db.assert_app_role()
    assert problems and "superuser" in caplog.text
    db.init_db()                                          # laptop mode still starts


def test_the_test_suite_refuses_to_run_as_a_superuser():
    """conftest.pytest_sessionstart exits on a superuser or BYPASSRLS login."""
    text = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "rolsuper" in text and "rolbypassrls" in text and "pytest.exit" in text


# ── static checks: the Docker files and the Windows set-up ──

COMPOSE_FILES = [ROOT / "docker-compose.yml", ROOT / "deploy" / "compose.oracle.yml"]


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_compose_files_keep_the_superuser_for_setup_and_backups_only(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["services"]["db"]["environment"]["POSTGRES_USER"] == "postgres"
    assert "pg_isready" in " ".join(doc["services"]["db"]["healthcheck"]["test"]) and "-U postgres" in " ".join(doc["services"]["db"]["healthcheck"]["test"])


def test_oracle_app_uses_the_rectifi_login_and_backup_uses_postgres():
    doc = yaml.safe_load((ROOT / "deploy" / "compose.oracle.yml").read_text(encoding="utf-8"))
    assert doc["services"]["app"]["environment"]["DATABASE_URL"].startswith("postgresql://rectifi:${RECTIFI_DB_PASSWORD}@db:5432/rectifi")
    assert "-U postgres" in " ".join(doc["services"]["backup"]["command"]) and "-U rectifi" not in " ".join(doc["services"]["backup"]["command"])
    db_env = doc["services"]["db"]["environment"]
    assert db_env["RECTIFI_DB_PASSWORD"].startswith("${RECTIFI_DB_PASSWORD") and db_env["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD")
    assert any(v.startswith("./db-init:/docker-entrypoint-initdb.d") for v in doc["services"]["db"]["volumes"])


def test_oracle_init_script_makes_a_plain_role_from_the_password_variable():
    script = (ROOT / "deploy" / "db-init" / "10-app-role.sh").read_text(encoding="utf-8")
    assert "NOSUPERUSER" in script and "NOBYPASSRLS" in script and "CREATE ROLE rectifi" in script
    assert ":'app_password'" in script and 'app_password="$RECTIFI_DB_PASSWORD"' in script       # the password is a psql variable, never in the file
    assert "\r" not in script, "a shell script with Windows line endings does not run on the server"
    assert not re.search(r"PASSWORD\s+'[^:]", script)                                            # no literal password


def test_dev_compose_publishes_only_on_loopback_and_the_init_sql_makes_the_plain_role():
    doc = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert doc["services"]["db"]["ports"] == ["127.0.0.1:5432:5432"]
    assert doc["services"]["db"]["environment"]["POSTGRES_DB"] == "postgres"          # so init.sql makes the rectifi database, owned by rectifi
    sql = (ROOT / "db" / "init.sql").read_text(encoding="utf-8")
    assert "NOSUPERUSER NOBYPASSRLS" in sql and "rolsuper OR rolbypassrls" in sql
    assert "OWNER rectifi" in sql


def test_no_file_gives_any_role_bypassrls_or_superuser():
    for path in [ROOT / "db" / "init.sql", ROOT / "deploy" / "db-init" / "10-app-role.sh", *COMPOSE_FILES]:
        for line in path.read_text(encoding="utf-8").splitlines():
            code = line.split("--")[0] if path.suffix == ".sql" else line.split("#")[0]
            assert not re.search(r"(?<!NO)\bBYPASSRLS\b", code), (path.name, line)
            assert not re.search(r"(?<!NO)\bSUPERUSER\b", code), (path.name, line)


def test_setup_windows_checks_the_role_after_set_up():
    text = (ROOT / "scripts" / "setup_windows.ps1").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "rolsuper OR rolbypassrls" in code and "Stop-Setup 'The rectifi login is a superuser or has BYPASSRLS" in code
    assert all(ord(c) < 128 for c in text), "setup_windows.ps1 must stay plain ASCII (Windows PowerShell 5.1)"
    assert "??" not in code and "?." not in code           # no PowerShell 7 operators
