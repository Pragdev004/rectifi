"""Helpers for the whole-feature acceptance tests (WS-PROOF). Not a test file.

Written from docs/MULTIUSER_PLAN.md only. Every place where the plan leaves a detail open (the exact JSON keys, how an invite
code is printed, how long a sweep takes) is ONE function in this file, listed in docs/handoffs/ws-proof.md, so the integrator
adapts a helper and never an assertion.

Rules for this file: no import of a not-yet-existing module at module level (imports go inside functions), no secret is ever
printed, and nothing connects to a database whose name does not end in `_test`.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "correct horse battery staple"
SESSION_SECRET = "s" * 48
HEADERS = {"X-Rectifi-Request": "1"}
BASE = "https://127.0.0.1:8501"          # https so a Secure cookie travels; the test client never opens a socket
TODAY = date(2026, 10, 21)


def test_url() -> str:
    from conftest import TEST_URL

    return TEST_URL


# ───────────────────────── mode and clients ─────────────────────────

def multiuser_env(monkeypatch) -> None:
    """RECTIFI_MULTIUSER=1 for this test (fresh_db turns it off again before every test)."""
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.delenv("RECTIFI_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("RECTIFI_PUBLIC", raising=False)
    reset_limiters()


def reset_limiters() -> None:
    """ASSUMPTION A1: the per-account and signup limiters live in memory in rectifi.api.auth (WS-AUTH) or rectifi.users and
    have a reset hook. If there is none, the limiter simply is not reset (the tests make only a handful of sign-ups)."""
    for module_name in ("rectifi.api.auth", "rectifi.users"):
        try:
            module = __import__(module_name, fromlist=["x"])
        except Exception:                                      # noqa: BLE001  (module not on this branch yet)
            continue
        limiter = getattr(module, "limiter", None)
        for owner in (limiter, module):
            for name in ("reset_memory", "reset_limits", "reset_limiter"):
                fn = getattr(owner, name, None) if owner is not None else None
                if callable(fn):
                    fn()


def make_app():
    from rectifi.api.app import create_app

    return create_app()


def new_client(app):
    from starlette.testclient import TestClient

    return TestClient(app, base_url=BASE, headers=HEADERS, raise_server_exceptions=False)


def gateway_client():
    from starlette.testclient import TestClient

    from rectifi import sms_gateway

    return TestClient(sms_gateway.create_app(), raise_server_exceptions=False)


# ───────────────────────── invites and sign-up (the plan's mechanism) ─────────────────────────

def _parse_code(output: str) -> str:
    """ASSUMPTION A2: `dev.py invite` prints the code once, on a line of its own or after a label. We take the best-looking
    token (a label like 'code' on the line, digits, dashes, capitals). Adapt here if the format differs."""
    best = None
    for line in output.splitlines():
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{7,}", line):
            score = (("code" in line.lower()) * 10) + any(ch.isdigit() for ch in token) + ("-" in token) + (token.upper() == token)
            key = (score, len(token))
            if best is None or key > best[0]:
                best = (key, token)
    if best is None:
        raise AssertionError(f"`dev.py invite` printed no code (output had {len(output.splitlines())} lines).")
    return best[1]


def make_invite(*, owner: bool = False, days: int = 7) -> str:
    """A one-time code made the way the plan says: `python scripts/dev.py invite [--days N] [--owner]` (stores only a hash)."""
    args = [sys.executable, str(ROOT / "scripts" / "dev.py"), "invite", "--days", str(days)] + (["--owner"] if owner else [])
    env = {**os.environ, "RECTIFI_MULTIUSER": "1", "DATABASE_URL": test_url()}
    done = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"`dev.py invite` failed with exit code {done.returncode}: {(done.stderr or done.stdout)[-300:]!r}")
    return _parse_code(done.stdout)


@dataclass
class Person:
    username: str
    password: str
    client: object
    uid: int = 0
    recovery_code: str = ""
    signup_token: str = ""
    ids: dict = field(default_factory=dict)
    canaries: dict = field(default_factory=dict)
    amounts: list = field(default_factory=list)
    device_token: str = ""

    def login(self) -> None:
        reply = self.client.post("/api/auth/login", json={"username": self.username, "password": self.password})
        assert reply.status_code == 200, f"login failed for {self.username}: {reply.status_code}"

    def logout(self) -> None:
        reply = self.client.post("/api/auth/logout")
        assert reply.status_code in (200, 204), f"logout failed: {reply.status_code}"

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, body=None, **kw):
        return self.client.post(path, json=body if body is not None else {}, **kw)


def uid_of(username: str) -> int:
    from rectifi import db

    with db.connect_system("proof: look up a user id") as conn:
        row = conn.execute("SELECT id FROM users WHERE lower(username) = lower(%s)", (username,)).fetchone()
    assert row is not None, f"no user {username!r} after sign-up"
    return int(row["id"])


def sign_up(app, invite: str, username: str, password: str = PASSWORD) -> Person:
    """Plan section 4: POST /api/auth/signup -> 201 {signup_token, recovery_code}, then POST /api/auth/signup/confirm {signup_token}
    sets the cookie. ASSUMPTION A3: both keys are at the top level of the JSON; confirm answers 200 or 201."""
    client = new_client(app)
    reply = client.post("/api/auth/signup", json={"invite": invite, "username": username, "password": password})
    assert reply.status_code == 201, f"signup refused for {username}: {reply.status_code} {reply.json() if reply.content else ''}"
    body = reply.json()
    person = Person(username, password, client, recovery_code=str(body["recovery_code"]), signup_token=str(body["signup_token"]))
    confirm = client.post("/api/auth/signup/confirm", json={"signup_token": person.signup_token})
    assert confirm.status_code in (200, 201), f"confirm refused for {username}: {confirm.status_code}"
    person.uid = uid_of(username)
    return person


def new_friend(app, username: str) -> Person:
    return sign_up(app, make_invite(), username)


# ───────────────────────── ids, amounts, canaries ─────────────────────────

def first_id(body, *keys):
    """The numeric id inside a JSON answer. ASSUMPTION A4: it is body['id'] or body[<key>]['id'] for one of `keys`
    (for example body['account']['id'], body['transaction']['id'], body['batch']['id'])."""
    if isinstance(body, dict):
        for key in keys:
            if isinstance(body.get(key), dict) and body[key].get("id") is not None:
                return int(body[key]["id"])
        if body.get("id") is not None:
            return int(body["id"])
        for value in body.values():
            if isinstance(value, dict) and value.get("id") is not None:
                return int(value["id"])
    raise AssertionError(f"no id found in {str(body)[:120]!r} (looked in {keys})")


def items_of(body) -> list:
    """A list answer: a bare list, or {'items': [...]}."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("items", "devices", "alerts", "rules"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def bump_sequences(floor: int) -> None:
    """Make every id this person gets unmistakable (a seven-digit number), so a leaked id cannot be mistaken for a count or an
    amount. Best effort: a sequence that cannot be moved is left alone."""
    try:
        with psycopg.connect(test_url(), autocommit=True) as conn:
            names = [r[0] for r in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'S' AND n.nspname = 'public' AND c.relname NOT IN ('users_id_seq', 'invites_id_seq')").fetchall()]
            for name in names:
                last = conn.execute(f'SELECT last_value FROM "{name}"').fetchone()[0]
                conn.execute("SELECT setval(%s::regclass, %s, false)", (f'"{name}"', max(floor, int(last) + 1)))
    except Exception:                                          # noqa: BLE001
        pass


class Canaries:
    """Unique lower-case marker words. Lower-case letters and digits only, so no parser changes them except by case."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.items: dict[str, tuple[str, bool]] = {}          # label -> (word, must_find_when_nothing_is_sealed)

    def new(self, label: str, must_find: bool = True) -> str:
        word = f"zqx{self.tag}{re.sub('[^a-z0-9]', '', label.lower())}{os.urandom(3).hex()}"
        self.items[label] = (word, must_find)
        return word

    def words(self) -> list[str]:
        return [w for w, _ in self.items.values()]

    def must_find(self) -> dict[str, str]:
        return {label: w for label, (w, must) in self.items.items() if must}


def b64_forms(text: str) -> list[str]:
    """The three alignments of the base64 of `text` (plain and url-safe), minus the edge characters that depend on neighbours."""
    out = []
    raw = text.encode()
    for shift in range(3):
        enc = base64.b64encode(b"\x00" * shift + raw).decode()
        enc = enc[(shift * 4 + 2) // 3:] if shift else enc          # drop the characters that mix in the padding bytes
        enc = enc.rstrip("=")
        if (len(raw) + shift) % 3:
            enc = enc[:-1]                                         # the last character mixes in what would follow
        out += [enc, enc.replace("+", "-").replace("/", "_")]
    return [f for f in dict.fromkeys(out) if len(f) >= 8]


def scan_text(text: str, words: dict[str, str]) -> dict[str, list[str]]:
    """Which words show up in `text`, and in which form: plain (any case), hex, base64. {label: [forms]}."""
    lowered = text.lower()
    found: dict[str, list[str]] = {}
    for label, word in words.items():
        forms = []
        variants = {word, word.upper(), word.capitalize()}
        if word in lowered:
            forms.append("plain")
        if any(v.encode().hex() in lowered for v in variants):
            forms.append("hex")
        if any(f in text for v in variants for f in b64_forms(v)):
            forms.append("base64")
        if forms:
            found[label] = forms
    return found


# ───────────────────────── raw database views ─────────────────────────

def raw_conn(uid: int):
    """A plain psycopg connection that sees exactly one person's rows (RLS applies; the GUC is set for the session). No decrypting
    row factory, so sealed text shows as the stored envelope."""
    return psycopg.connect(test_url(), options=f"-c app.user_id={int(uid)}", autocommit=True)


def tenant_tables() -> list[str]:
    with psycopg.connect(test_url(), autocommit=True) as conn:
        return [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.columns WHERE table_schema = 'public' AND column_name = 'user_id' "
            "AND table_name <> 'users' ORDER BY 1").fetchall()]


def table_checksums(uid: int, tables: list[str] | None = None) -> dict[str, tuple[int, str]]:
    """{table: (row count, md5 of every row's text)} for one person, over every table that has a user_id column."""
    out = {}
    with raw_conn(uid) as conn:
        for table in tables or tenant_tables():
            row = conn.execute(f'SELECT count(*), md5(coalesce(string_agg(t::text, \'|\' ORDER BY t::text), \'\')) FROM "{table}" t').fetchone()
            out[table] = (int(row[0]), row[1])
    return out


def count_rows(uid: int, table: str) -> int:
    with raw_conn(uid) as conn:
        return int(conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def plain_sealed_cells(uid: int) -> int:
    """How many cells in the sealed-column registry still hold plain text (not NULL, not an rx1./rs1. envelope) for this person."""
    from rectifi import sealed_fields

    total = 0
    with raw_conn(uid) as conn:
        for table, spec in sealed_fields.SEALED.items():
            for column in spec.sealed:
                where = f'"{column}" IS NOT NULL AND "{column}" NOT LIKE \'rx1.%\' AND "{column}" NOT LIKE \'rs1.%\''
                if spec.when:
                    where += f' AND "{spec.when[0]}" = \'{spec.when[1]}\''
                try:
                    total += int(conn.execute(f'SELECT count(*) FROM "{table}" WHERE {where}').fetchone()[0])
                except psycopg.errors.UndefinedColumn:
                    pass
    return total


def wait_for_sweep(uid: int, timeout: float = 90.0) -> None:
    """ASSUMPTION A5: the owner sweep (WS-ACCOUNT) runs on its own after the claim is confirmed and is finished when the owner has
    no plain value left in any registry column. We watch exactly that, twice in a row, so the sweep's own state names do not matter."""
    deadline = time.time() + timeout
    quiet = 0
    while time.time() < deadline:
        quiet = quiet + 1 if plain_sealed_cells(uid) == 0 else 0
        if quiet >= 2:
            return
        time.sleep(0.25)
    raise AssertionError(f"the sweep for user {uid} was not finished after {timeout:.0f} s ({plain_sealed_cells(uid)} plain cells left)")


# ───────────────────────── a dump of the whole database ─────────────────────────

def find_pg_dump() -> str | None:
    found = shutil.which("pg_dump")
    if found:
        return found
    for base in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL", Path("/usr/lib/postgresql")):
        if base.is_dir():
            for version in sorted(base.iterdir(), reverse=True):
                for name in ("pg_dump.exe", "pg_dump"):
                    candidate = version / "bin" / name
                    if candidate.is_file():
                        return str(candidate)
    return None


@contextmanager
def no_force_rls():
    """A forced owner cannot dump (plan section 8): turn FORCE off on the test database for the length of the block, and always
    put it back. The database name must end in _test."""
    url = test_url()
    assert psycopg.conninfo.conninfo_to_dict(url)["dbname"].endswith("_test"), "never run on a real database"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SET lock_timeout = '15s'")
        forced = [r[0] for r in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relforcerowsecurity").fetchall()]
        try:
            for table in forced:
                conn.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
            yield
        finally:
            for table in forced:
                conn.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


@dataclass
class Dump:
    raw: str                                   # everything, as text (pg_dump data when it was found, else the COPY text)
    source: str                                # 'pg_dump' or 'copy'
    cells: list[tuple[str, str, int | None, str | None]]    # (table, column, user_id or None, value)


def _unescape(cell: str) -> str:
    return (cell.replace("\\\\", "\x00").replace("\\t", "\t").replace("\\n", "\n").replace("\\r", "\r").replace("\x00", "\\"))


def dump_database() -> Dump:
    """Every row of every table, BYTEA and JSONB included, read with FORCE off (see no_force_rls). `pg_dump` (plain format, which is
    uncompressed) when it is installed, else COPY. The cell list always comes from COPY, so we know table, column and owner."""
    url = test_url()
    info = psycopg.conninfo.conninfo_to_dict(url)
    cells: list[tuple[str, str, int | None, str | None]] = []
    copy_text: list[str] = []
    with no_force_rls():
        with psycopg.connect(url, autocommit=True) as conn:
            tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1").fetchall()]
            for table in tables:
                columns = [d.name for d in conn.execute(f'SELECT * FROM "{table}" LIMIT 0').description]
                chunks = []
                with conn.cursor().copy(f'COPY "{table}" TO STDOUT') as copy:
                    for chunk in copy:
                        chunks.append(bytes(chunk))
                text = b"".join(chunks).decode("utf-8", errors="replace")
                copy_text.append(text)
                uid_at = columns.index("user_id") if "user_id" in columns else None
                for line in text.split("\n"):
                    if not line:
                        continue
                    parts = line.split("\t")
                    if len(parts) != len(columns):
                        continue
                    uid = int(parts[uid_at]) if uid_at is not None and parts[uid_at].isdigit() else None
                    for column, part in zip(columns, parts):
                        cells.append((table, column, uid, None if part == "\\N" else _unescape(part)))
        exe = find_pg_dump()
        if exe:
            env = {**os.environ, "PGPASSWORD": info.get("password", "")}
            args = [exe, "--data-only", "--no-owner", "--no-privileges", "-h", info.get("host", "localhost"),
                    "-p", str(info.get("port", 5432)), "-U", info.get("user", ""), info["dbname"]]
            done = subprocess.run(args, env=env, capture_output=True, timeout=300)
            if done.returncode == 0 and done.stdout:
                return Dump(done.stdout.decode("utf-8", errors="replace"), "pg_dump", cells)
    return Dump("\n".join(copy_text), "copy", cells)


# ───────────────────────── fixed pieces: clock, push, SMS, statements ─────────────────────────

def pin_clock(monkeypatch) -> None:
    """Today is 2026-10-21 for tools and for the alert clock. ASSUMPTION A6: alerts._clock still exists (tests/alerts_support.Clock)."""
    from alerts_support import Clock

    from rectifi import alerts, tools

    monkeypatch.setattr(tools, "today", lambda: TODAY)
    monkeypatch.setattr(alerts, "_clock", Clock(), raising=False)


class PushRecorder:
    """Replaces webpush.send so nothing leaves the machine. Remembers WHICH phone each push went to."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, sub, payload, ttl=3600, urgency="normal", client=None):
        endpoint = sub.get("endpoint") if hasattr(sub, "get") else getattr(sub, "endpoint", repr(sub))
        self.sent.append((str(endpoint), payload.decode() if isinstance(payload, bytes) else str(payload)))
        return "ok"


def fake_push(monkeypatch) -> PushRecorder:
    from rectifi import webpush

    rec = PushRecorder()
    monkeypatch.setattr(webpush, "send", rec)
    return rec


def push_body(label: str) -> dict:
    """A push subscription the validator accepts: an allow-listed host, a 65-byte key and a 16-byte secret."""
    b64u = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")      # noqa: E731
    return {"endpoint": f"https://fcm.googleapis.com/fcm/send/{os.urandom(8).hex()}", "p256dh": b64u(b"\x04" + os.urandom(64)),
            "auth": b64u(os.urandom(16)), "label": label}


def sms_debit(amount: str, payee_word: str, ref: str | None = None, day: str = "21-10-26", last4: str = "1234") -> str:
    ref = ref or str(int.from_bytes(os.urandom(5), "big") % 10**12).zfill(12)
    return f"Rs.{amount} debited from A/c XX{last4} on {day} to VPA {payee_word}@icici UPI Ref No {ref}"


def sms_unclear(word: str, last4: str = "1234") -> str:
    """Money moved, but no direction the fixed rules can prove: goes to the Needs review inbox, never guessed."""
    return f"Rs 500.00 transferred from A/c XX{last4} to your wallet {word}"


def statement_csv(rows: list[tuple[str, str, str, str]], opening: str = "20000.00") -> bytes:
    """An HDFC-style CSV like tests/fixtures/hdfc_style_statement.csv. rows = (dd/mm/yy, narration, ref, withdrawal amount)."""
    from decimal import Decimal

    lines = ["HDFC BANK Ltd.,,,,,,", "Statement of account,,,,,,", "Account No : XXXXXXXX4417,,,,,,", ",,,,,,",
             "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance"]
    balance = Decimal(opening)
    for when, narration, ref, amount in rows:
        balance -= Decimal(amount)
        lines.append(f'{when},{narration},{ref},{when},"{amount}",,{balance:.2f}')
    lines += [",,,,,,", "**Statement summary,,,,,,"]
    return ("\n".join(lines) + "\n").encode()


def upload_statement(client, filename: str, data: bytes, manual: bool = False):
    fields = {"manual": "1"} if manual else {}
    return client.post("/api/imports", files={"file": (filename, data, "text/csv")}, data=fields)


def commit_preview(client, preview: dict, account_id: int):
    """If the server asks us to check the columns, accept its own guess."""
    return client.post("/api/imports/commit", json={"upload_id": preview["upload_id"], "account_id": account_id,
                                                    "mapping": preview.get("mapping") or {}})


# ───────────────────────── seeding a person through the real routes ─────────────────────────

def seed_person(person: Person, gateway, tag: str, floor: int, *, rich: bool = True) -> Person:
    """Everything a friend can do, through the API, with a unique canary in every text field and a distinctive amount in every
    money field. Records what it made in person.ids / person.canaries / person.amounts. Fails with the status if a route says no."""
    bump_sequences(floor)
    c = Canaries(tag)
    person.canaries = c.items
    ids = person.ids
    n = floor // 1_000_000                                    # 7 for the first person, 8 for the second: distinct rupees and paise
    money = lambda whole: f"{whole + n * 11}.{n * 7 + 10:02d}"    # noqa: E731  (e.g. 7422.59)
    person.amounts = [money(7345), money(8123), money(6231), money(5120), money(4410), money(3300), money(9876), money(1234), money(9999)]
    take = iter(person.amounts)

    def ok(reply, what, *good):
        assert reply.status_code in (good or (200, 201)), f"seeding {person.username}: {what} answered {reply.status_code} {reply.text[:160]}"
        return reply.json() if reply.content else {}

    ids["account"] = first_id(ok(person.post("/api/accounts", {"name": c.new("acct"), "last4": "1234"}), "add account"), "account")
    ids["account2"] = first_id(ok(person.post("/api/accounts", {"name": c.new("acct2"), "last4": "4417"}), "add 2nd account"), "account")
    ok(person.post(f"/api/accounts/{ids['account']}/balances", {"as_of": "2026-10-01", "balance": money(7000)}), "balance")
    ids["txn"] = first_id(ok(person.post("/api/transactions", {
        "amount": next(take), "direction": "debit", "date": "2026-10-05", "payee": c.new("payee"), "category": "Food & Dining",
        "note": c.new("note"), "account_id": ids["account"]}), "add transaction"), "transaction")
    ok(person.client.put("/api/budgets", json={"month": "2026-10", "category": "Food & Dining", "limit": money(9876)}), "budget")
    ok(person.client.put("/api/review/commitment", json={"text": c.new("commit"), "week": "2026-10-19"}), "commitment")
    if not rich:
        return person
    paired = ok(person.post("/api/phone/devices", {"name": c.new("device")}), "pair a phone")
    person.device_token = paired["token"]
    ids["device"] = first_id(paired, "device")
    reply = gateway.post("/sms", headers={"Authorization": f"Bearer {person.device_token}"},
                         json={"from": "AX-HDFCBK", "text": sms_debit(next(take), c.new("smsvpa"))})
    assert reply.status_code == 200 and reply.json().get("status") == "added", f"gateway SMS for {person.username}: {reply.status_code} {reply.text[:80]}"
    reply = gateway.post("/sms", headers={"Authorization": f"Bearer {person.device_token}"},
                         json={"from": "AX-" + c.new("sender").upper(), "text": sms_unclear(c.new("review"))})
    assert reply.status_code == 200 and reply.json().get("status") == "needs_review", f"review SMS: {reply.status_code} {reply.text[:80]}"
    inbox = items_of(ok(person.get("/api/phone/inbox"), "inbox"))
    assert inbox, "the Needs review inbox is empty after a needs_review SMS"
    ids["inbox"] = int(inbox[0]["id"])
    ok(person.post("/api/sms/save", {"text": sms_debit(next(take), c.new("pastevpa"))}), "paste SMS")
    rows = [("03/10/26", f"UPI-{c.new('stmt1').upper()}-{c.new('stmtvpa')}@icici-ICIC0000103-429183746521-PAYMENT", "0000429183746521", money(4410)),
            ("04/10/26", f"POS 416021XXXXXX4417 {c.new('stmt2').upper()} CHENNAI", "POS0912", money(3300))]
    file_name = f"{c.new('file')}.csv"
    done = ok(upload_statement(person.client, file_name, statement_csv(rows)), "statement upload")
    if done.get("outcome") == "preview":
        done = ok(commit_preview(person.client, done, ids["account2"]), "statement commit")
    ids["batch"] = first_id(done, "batch")
    other = [("06/10/26", f"UPI-{c.new('prev')}-x@icici-ICIC0000103-429183746599-PAYMENT", "0000429183746599", money(1234))]
    preview = ok(upload_statement(person.client, f"{c.new('file2')}.csv", statement_csv(other, "30000.00"), manual=True), "statement preview")
    ids["upload"] = str(preview.get("upload_id") or "")
    ids["upload_mapping"] = preview.get("mapping") or {}
    ids["pattern"] = first_id(ok(person.post("/api/merchants/patterns", {"pattern": c.new("pat"), "merchant": c.new("merchant"), "group": "Other"}), "pattern"))
    ids["rule"] = first_id(ok(person.post("/api/alerts/rules", {"scope": "total", "target": "all", "period": "week", "cap": money(1234)}), "cap"))
    ok(person.post("/api/push/subscriptions", push_body(c.new("label"))), "push subscription")
    subs = items_of(ok(person.get("/api/push/subscriptions"), "subscriptions"))
    assert subs, "no push subscription listed after adding one"
    ids["sub"] = int(subs[-1]["id"])
    return person


def raise_one_alert(person: Person, monkeypatch=None) -> int:
    """After seed_person: one baseline tick, then a big payment today (the cap is far lower), then a tick. Returns the alert id.
    ASSUMPTION A7: alerts.tick() with nothing bound loops over users itself (plan section 6, WS-ALERTS)."""
    from rectifi import alerts

    alerts.tick()
    reply = person.post("/api/transactions", {"amount": person.amounts[-1], "direction": "debit", "date": "2026-10-21",
                                              "payee": "Chai stall", "account_id": person.ids["account"]})
    assert reply.status_code in (200, 201), f"alert payment refused: {reply.status_code}"
    alerts.tick()
    listed = items_of(person.get("/api/alerts?status=all").json())
    assert listed, f"no alert for {person.username} after the cap was passed"
    person.ids["alert"] = int(listed[0]["id"])
    return person.ids["alert"]


def build_world(monkeypatch, *, alert: bool = True):
    """Mode on, clock pinned, push faked, A (rich, ids 7,000,000+) seeded completely, then B (ids 8,000,000+)."""
    multiuser_env(monkeypatch)
    pin_clock(monkeypatch)
    rec = fake_push(monkeypatch)
    app = make_app()
    gw = gateway_client()
    a = new_friend(app, "alice")
    seed_person(a, gw, "a", 7_000_000)
    if alert:
        raise_one_alert(a)
    b = new_friend(app, "bob")
    seed_person(b, gw, "b", 8_000_000)
    return app, gw, a, b, rec


# ───────────────────────── reading JSON for the Privacy page ─────────────────────────

def flatten(data) -> list[tuple[str, str]]:
    """Every (key, string value) in a JSON document, keys and values both; lists contribute their items under their own key."""
    out: list[tuple[str, str]] = []

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                out.append((str(k), ""))
                walk(v, str(k))
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        else:
            out.append((key, str(node)))

    walk(data)
    return out


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def haystack(data) -> str:
    return norm(" ".join(f"{k} {v}" for k, v in flatten(data)))


class Sampler(threading.Thread):
    """Calls `fn` every 40 ms until stopped and keeps every answer (used to watch a total while a sweep runs)."""

    def __init__(self, fn) -> None:
        super().__init__(daemon=True)
        self.fn, self.stop_flag, self.samples, self.errors = fn, threading.Event(), [], []

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                self.samples.append(self.fn())
            except Exception as exc:                           # noqa: BLE001
                self.errors.append(repr(exc))
            time.sleep(0.04)

    def finish(self) -> list:
        self.stop_flag.set()
        self.join(timeout=10)
        return self.samples
