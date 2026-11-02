"""rectifi/users.py on its own: invites, the dev.py invite commands, how keys are stored, the scrypt limit, and "no reset path" (D2)."""

import importlib.util
import inspect
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from rectifi import db, userctx, users, vault
from rectifi.api.app import api_routes

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    users.reset_limiters()
    yield
    users.reset_limiters()


def load_devtool():
    spec = importlib.util.spec_from_file_location("rectifi_devtool_users_auth_test", ROOT / "scripts" / "devtools" / "users.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def raw_users():
    with db.connect_system("test: read users") as conn:
        return conn.execute("SELECT * FROM users ORDER BY id").fetchall()


def signed_up(username="friend", password=PASSWORD):
    code = users.create_invite()["code"]
    token, recovery = users.signup(code, username, password)
    return token, recovery, code


# ───────────────────────── invites ─────────────────────────

def test_an_invite_is_stored_only_as_a_hash():
    made = users.create_invite("friend", 7)
    assert made["code"].count("-") == 4 and made["kind"] == "friend"
    with db.connect_system("test: read invites") as conn:
        row = conn.execute("SELECT * FROM invites WHERE id = %s", (made["id"],)).fetchone()
    assert made["code"] not in json.dumps(row, default=str) and made["code"].replace("-", "") not in json.dumps(row, default=str)
    assert row["code_hash"] == users._code_hash(made["code"]) and len(row["code_hash"]) == 64
    assert users._code_hash(made["code"].lower().replace("-", " ")) == row["code_hash"]          # typing it loosely still works


def test_two_invites_never_share_a_code():
    assert len({users.create_invite()["code"] for _ in range(20)}) == 20


def test_list_invites_has_states_and_never_a_code():
    open_one = users.create_invite("friend", 7)
    owner = users.create_invite("owner", 3)
    revoked = users.create_invite("friend", 7)
    users.revoke_invite(revoked["id"])
    rows = {r["id"]: r for r in users.list_invites()}
    assert rows[open_one["id"]]["state"] == "open" and rows[owner["id"]]["kind"] == "owner" and revoked["id"] not in rows
    assert all("code" not in r for r in rows.values())
    token, _, code = signed_up("someone")
    assert any(r["state"] == "reserved" and r["reserved_by"] == "someone" for r in users.list_invites())
    users.confirm_signup(token)
    assert any(r["state"] == "used" for r in users.list_invites())


def test_an_invite_has_a_limited_life():
    with pytest.raises(ValueError):
        users.create_invite("friend", 0)
    with pytest.raises(ValueError):
        users.create_invite("stranger", 7)


def test_a_used_invite_cannot_be_revoked():
    token, _, _ = signed_up()
    users.confirm_signup(token)
    used = [r for r in users.list_invites() if r["state"] == "used"][0]
    assert users.revoke_invite(used["id"]) is False


# ───────────────────────── dev.py invite, invites, revoke-invite ─────────────────────────

def test_the_invite_commands_print_the_code_once_and_store_only_its_hash(capsys):
    tool = load_devtool()
    assert {"invite", "invites", "revoke-invite"} <= set(tool.COMMANDS)
    tool.invite(["--days", "3"])
    out = capsys.readouterr().out
    code = re.search(r"[A-Z2-9]{4}(?:-[A-Z2-9]{4}){4}", out).group(0)
    assert "cannot be shown again" in out
    tool.invites([])
    listing = capsys.readouterr().out
    assert code not in listing and "open" in listing and "friend" in listing
    with db.connect_system("test: read invites") as conn:
        rows = conn.execute("SELECT * FROM invites").fetchall()
    assert len(rows) == 1 and code not in json.dumps(rows, default=str)
    tool.revoke_invite([str(rows[0]["id"])])
    assert "cancelled" in capsys.readouterr().out
    tool.invites([])
    assert "No invites yet" in capsys.readouterr().out


def test_the_owner_invite_command_and_its_arguments(capsys):
    tool = load_devtool()
    tool.invite(["--owner"])
    out = capsys.readouterr().out
    assert "owner" in out and "user 1" in out
    with db.connect_system("test: read invites") as conn:
        assert conn.execute("SELECT kind FROM invites").fetchone()["kind"] == "owner"
    for bad in (["--days"], ["--days", "x"], ["--days", "0"], ["--days", "100"], ["--nope"]):
        with pytest.raises(SystemExit):
            tool.invite(bad)
    with pytest.raises(SystemExit):
        tool.revoke_invite([])
    with pytest.raises(SystemExit):
        tool.revoke_invite(["abc"])


# ───────────────────────── keys ─────────────────────────

def test_the_stored_keys_hold_no_password_no_data_key_and_no_recovery_code():
    token, recovery, _ = signed_up()
    row = raw_users()[-1]
    blobs = {k: bytes(v) for k, v in row.items() if isinstance(v, (bytes, memoryview))}
    assert {"scrypt_salt", "verifier", "dek_by_password", "dek_by_recovery", "recovery_salt", "public_key", "private_key_wrapped", "ingest_salt"} <= set(blobs)
    assert len(blobs["verifier"]) == 32                                              # sha256 of the scrypt verifier half
    assert len(blobs["dek_by_password"]) == 12 + 32 + 16 and len(blobs["dek_by_recovery"]) == 12 + 32 + 16     # nonce + key + tag
    for blob in blobs.values():
        assert PASSWORD.encode() not in blob and recovery.replace("-", "").encode() not in blob
    # the wrapped data key opens with the password's key, and only with that
    verifier, kek = vault.derive_password_keys(PASSWORD, blobs["scrypt_salt"])
    assert vault.verifier_matches(verifier, blobs["verifier"])
    dek = vault.unwrap_key(kek, blobs["dek_by_password"], "dek|password")
    _, other_kek = vault.derive_password_keys("a different password", blobs["scrypt_salt"])
    with pytest.raises(vault.VaultError):
        vault.unwrap_key(other_kek, blobs["dek_by_password"], "dek|password")
    # the recovery code opens the same data key
    assert vault.unwrap_key(vault.recovery_key(recovery, blobs["recovery_salt"]), blobs["dek_by_recovery"], "dek|recovery") == dek
    # the private key is wrapped by the data key and matches the public key
    private = vault.unwrap_key(dek, blobs["private_key_wrapped"], "privkey")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    derived = X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert derived == blobs["public_key"]


def test_the_session_row_holds_the_data_key_only_wrapped_under_the_cookie_secret():
    token, _, _ = signed_up()
    who = users.confirm_signup(token)
    with db.connect_system("test: read sessions") as conn:
        row = conn.execute("SELECT * FROM user_sessions WHERE id = %s", (who.sid,)).fetchone()
    assert who.ctx.key not in bytes(row["dek_wrapped"]) and not row["pending"]
    assert vault.unwrap_key(who.k, bytes(row["dek_wrapped"]), f"session|{who.sid}") == who.ctx.key
    with pytest.raises(vault.VaultError):
        vault.unwrap_key(b"\x00" * 32, bytes(row["dek_wrapped"]), f"session|{who.sid}")
    with pytest.raises(vault.VaultError):                                            # the wrapped key is bound to its own session id
        vault.unwrap_key(who.k, bytes(row["dek_wrapped"]), "session|another-id")


def test_a_new_friend_is_marked_encrypted_and_has_a_context_with_every_key():
    token, _, _ = signed_up("friend")
    who = users.confirm_signup(token)
    ctx = who.ctx
    assert ctx.uid == who.uid != userctx.OWNER_UID and ctx.strict is True
    assert ctx.key and ctx.public_key and ctx.ingest_salt and ctx.private_key
    assert [r for r in raw_users() if r["id"] == who.uid][0]["encrypted"] is True
    sealed = vault.seal("hello", ctx)
    assert sealed.startswith("rx1.") and vault.open_value(sealed, ctx) == "hello"


def test_the_open_session_check_follows_the_epoch():
    token, _, _ = signed_up()
    who = users.confirm_signup(token)
    k = users._b64(who.k)
    assert users.open_session(who.uid, who.sid, k, who.epoch).uid == who.uid
    assert users.open_session(who.uid, who.sid, k, who.epoch + 1) is None
    assert users.open_session(who.uid + 1, who.sid, k, who.epoch) is None
    assert users.open_session("1", who.sid, k, who.epoch) is None
    users.end_all_sessions(who.uid)
    assert users.open_session(who.uid, who.sid, k, who.epoch) is None


def test_at_most_two_scrypts_run_at_once(monkeypatch):
    running, peak, lock = [0], [0], threading.Lock()

    class SlowScrypt:
        def __init__(self, **_):
            pass

        def derive(self, data):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.15)
            with lock:
                running[0] -= 1
            return b"x" * 64

    monkeypatch.setattr(vault, "Scrypt", SlowScrypt)
    threads = [threading.Thread(target=vault.derive_password_keys, args=("pw", b"s" * 16)) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak[0] == 2


def test_scrypt_uses_the_planned_cost():
    assert (vault.SCRYPT_N, vault.SCRYPT_R, vault.SCRYPT_P) == (2 ** 16, 8, 1)


def test_input_rules():
    assert users.password_problem("short") and users.password_problem(None) and users.password_problem("a" * 2000)
    assert users.password_problem("a" * 12) is None
    for bad in ("ab", "-leading", "has space", "emoji☃", "x" * 40, None, ""):
        assert users.username_problem(bad)
    for good in ("abc", "Prag.s", "friend_1", "a-b-c"):
        assert users.username_problem(good) is None


# ───────────────────────── no reset path (D2) ─────────────────────────

RESET_WORDS = re.compile(r"reset[-_ ]?(user[-_ ]?)?password|password[-_ ]?reset|admin[-_ ]?reset|force[-_ ]?password|set[-_ ]?user[-_ ]?password",
                         re.IGNORECASE)


def source_files(*folders):
    for folder in folders:
        for path in (ROOT / folder).rglob("*"):
            if path.suffix in (".py", ".ps1", ".sh", ".sql") and "__pycache__" not in path.parts:
                yield path


def test_no_script_or_route_can_reset_a_password_without_the_password_or_the_recovery_code():
    offenders = []
    for path in source_files("scripts", "rectifi/api", "deploy", "db"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if RESET_WORDS.search(text) or re.search(r"\b(dek_by_password|dek_by_recovery|verifier)\b", text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    assert not any(re.search(r"(?<![a-z])(reset|admin)", r.path.lower()) for r in api_routes())
    commands = {name for name in load_devtool().COMMANDS}
    assert not any("reset" in c or "password" in c for c in commands)


def test_only_known_functions_write_the_wrapped_keys_and_each_one_needs_a_secret():
    """Every place in users.py that writes dek_by_password is sign-up, change_password or recover. The last two first prove
    the current password (_verify_current) or the recovery code (recovery_key + unwrap_key)."""
    writers = {name for name, fn in inspect.getmembers(users, inspect.isfunction) if "dek_by_password =" in inspect.getsource(fn)}
    assert writers == {"change_password", "recover", "signup", "purge_unconfirmed"}
    assert "dek_by_password = NULL" in inspect.getsource(users.purge_unconfirmed)                   # that one only clears an unconfirmed owner claim
    assert "_verify_current(" in inspect.getsource(users.change_password)
    recover_source = inspect.getsource(users.recover)
    assert "unwrap_key(vault.recovery_key(" in recover_source and recover_source.index("unwrap_key(") < recover_source.index("UPDATE users")
    claims = inspect.getsource(users.signup)
    assert "dek_by_password = %s" in claims and "WHERE id = %s AND verifier IS NULL" in claims          # the owner claim only works on an unclaimed row
    others = [n for n, fn in inspect.getmembers(users, inspect.isfunction) if "dek_by_password" in inspect.getsource(fn)]
    assert set(others) <= {"change_password", "recover", "signup", "purge_unconfirmed", "_check_password", "_can_sign_in", "_signed_in_user", "_open_session_row"}


def test_the_password_routes_refuse_without_the_secret(monkeypatch):
    from starlette.testclient import TestClient

    from rectifi.api.app import create_app

    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "s" * 48)
    client = TestClient(create_app(), base_url="http://127.0.0.1:8501", headers={"X-Rectifi-Request": "1"}, raise_server_exceptions=False)
    token, recovery, _ = signed_up("friend")
    users.confirm_signup(token)
    for body in ({}, {"username": "friend"}, {"username": "friend", "recovery_code": recovery}, {"username": "friend", "new_password": "x" * 20},
                 {"username": "friend", "recovery_code": "", "new_password": "x" * 20}):
        assert client.post("/api/auth/recover", json=body).status_code in (401, 422), body
    row = [r for r in raw_users() if r["username"] == "friend"][0]
    assert bytes(row["verifier"]) == vault.verifier_hash(vault.derive_password_keys(PASSWORD, bytes(row["scrypt_salt"]))[0])      # nothing changed


# ───────────────────────── imports ─────────────────────────

def test_importing_users_loads_no_langchain_and_no_web_layer():
    code = ("import sys, rectifi.users; "
            "assert not any(m.startswith('langchain') for m in sys.modules); "
            "assert not any(m.startswith('rectifi.api') for m in sys.modules)")
    assert subprocess.run([sys.executable, "-c", code], cwd=str(ROOT)).returncode == 0
