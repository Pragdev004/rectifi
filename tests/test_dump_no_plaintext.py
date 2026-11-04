"""WS-PROOF: a stolen database dump or backup must not hold a friend's text (docs/MULTIUSER_PLAN.md sections 1 and 8).

Real flows for a friend, each with a unique canary word: an SMS that arrives signed in (the Paste box), SMS that arrive signed out
(gateway: one clear, one Needs review), a statement import, a note, a merchant name of the person's own, a phone label, an alert.
Then the same flows for the owner, who first has plain data (laptop mode), claims user 1, and lets the sweep run.

After each stage the whole database is read (every table, BYTEA and JSONB included; pg_dump when it is installed, else COPY, with
FORCE ROW LEVEL SECURITY switched off on the _test database for the length of the read and always put back). A canary in any form
(plain, upper case, hex, base64) fails the test. So does a password, recovery code, device token or sign-up token, and so does
anything in the captured log. Allow-listed brand names may appear in transactions.merchant_key and nowhere in a sealed column.

Sanity: the same flows in laptop mode (nothing sealed) MUST be found by the same scan, or the scan proves nothing.
"""

from __future__ import annotations

import logging
import os

from support import proof

LONG_BRANDS_MIN = 5          # a 3 or 4 letter brand ('ola', 'uber') would turn up by chance inside base64 text


class Auth:
    """Signing out and in during a flow. In laptop mode there is nobody to sign out."""

    def __init__(self, person=None) -> None:
        self.person = person

    def logout(self) -> None:
        if self.person is not None:
            self.person.logout()

    def login(self) -> None:
        if self.person is not None:
            self.person.login()


def ok(reply, what, *good):
    assert reply.status_code in (good or (200, 201)), f"{what} answered {reply.status_code} {reply.text[:140]}"
    return reply.json() if reply.content else {}


def flows(client, gw, c: proof.Canaries, auth: Auth, on_signed_out=None) -> dict:
    """Everything a friend does that puts text in the database. Returns ids and the device token."""
    from rectifi import alerts

    out: dict = {}
    acct = proof.first_id(ok(client.post("/api/accounts", json={"name": c.new("acct"), "last4": "1234"}), "add account"), "account")
    acct2 = proof.first_id(ok(client.post("/api/accounts", json={"name": c.new("acct2"), "last4": "4417"}), "add account 2"), "account")
    ok(client.post("/api/push/subscriptions", json=proof.push_body(c.new("label"))), "push subscription")
    pattern = c.new("pat")
    ok(client.post("/api/merchants/patterns", json={"pattern": pattern, "merchant": c.new("merchant"), "group": "Other"}), "merchant name")
    ok(client.post("/api/alerts/rules", json={"scope": "merchant", "target": "Swiggy", "period": "week", "cap": "100.00"}), "Swiggy cap")
    ok(client.post("/api/alerts/rules", json={"scope": "merchant", "target": c.items["merchant"][0], "period": "week", "cap": "100.00"}), "custom cap")
    paired = ok(client.post("/api/phone/devices", json={"name": c.new("device")}), "pair a phone")
    token = out["device_token"] = paired["token"]
    ok(client.put("/api/review/commitment", json={"text": c.new("commit"), "week": "2026-10-19"}), "commitment")

    # signed in: the Paste SMS box, a hand-made payment with a note, and a note edited afterwards
    ok(client.post("/api/sms/save", json={"text": proof.sms_debit("61.61", c.new("smssignedin"))}), "paste SMS")
    made = ok(client.post("/api/transactions", json={"amount": "62.62", "direction": "debit", "date": "2026-10-06", "payee": c.new("payee"),
                                                    "note": c.new("note", must_find=False), "account_id": acct}), "add payment")
    ok(client.patch(f"/api/transactions/{proof.first_id(made, 'transaction')}", json={"note": c.new("note2")}), "edit note")
    alerts.tick()                                              # what is here now is history; the payments below are news

    # signed out: only the phone gateway can still write
    auth.logout()
    header = {"Authorization": f"Bearer {token}"}
    reply = gw.post("/sms", headers=header, json={"from": "AX-HDFCBK", "text": proof.sms_debit("63.63", c.new("smsout"))})
    assert reply.json() == {"status": "added"}, f"signed-out SMS: {reply.status_code} {reply.text[:80]}"
    reply = gw.post("/sms", headers=header, json={"from": "AX-HDFCBK", "text": proof.sms_debit("4321.43", "swiggy")})
    assert reply.json() == {"status": "added"}, f"signed-out Swiggy SMS: {reply.status_code} {reply.text[:80]}"
    reply = gw.post("/sms", headers=header, json={"from": "AX-" + c.new("sender").upper(), "text": proof.sms_unclear(c.new("review"))})
    assert reply.json() == {"status": "needs_review"}, f"review SMS: {reply.status_code} {reply.text[:80]}"
    alerts.tick()                                              # the Swiggy cap fires while nobody is signed in
    if on_signed_out:
        on_signed_out()
    auth.login()

    # signed in again: a statement, and a payment to the person's own merchant name
    rows = [("03/10/26", f"UPI-{c.new('stmt1').upper()}-{c.new('stmtvpa')}@icici-ICIC0000103-429183746521-PAYMENT", "0000429183746521", "64.64"),
            ("04/10/26", f"POS 416021XXXXXX4417 {c.new('stmt2').upper()} CHENNAI", "POS0912", "65.65")]
    done = ok(proof.upload_statement(client, f"{c.new('file')}.csv", proof.statement_csv(rows)), "statement upload")
    if done.get("outcome") == "preview":
        ok(proof.commit_preview(client, done, acct2), "statement commit")
    ok(client.post("/api/transactions", json={"amount": "4999.99", "direction": "debit", "date": "2026-10-21", "payee": f"{pattern} cafe",
                                              "account_id": acct}), "payment to the person's own merchant")
    alerts.tick()
    return out


def secrets_of(person: proof.Person | None, extra: dict) -> dict[str, str]:
    found = {}
    if person is not None:
        found["password"] = person.password
        found["recovery code"] = person.recovery_code
        found["recovery code (no dashes)"] = person.recovery_code.replace("-", "").replace(" ", "")
        found["sign-up token"] = person.signup_token
    if extra.get("device_token"):
        found["device token"] = extra["device_token"]
    return {k: v for k, v in found.items() if v and len(v) >= 8}


def brand_words() -> list[str]:
    from rectifi import merchant_allowlist

    words = set()
    for brand in merchant_allowlist.ALLOWED:
        for w in (brand.key, brand.name.lower()):
            if len(w) >= LONG_BRANDS_MIN and w.isalpha():
                words.add(w)
    return sorted(words)


def check(stage: str, dump: proof.Dump, words: dict[str, str], secrets: dict[str, str], uids: list[int]) -> list[str]:
    """Everything that must hold in one dump. Returns the problems, so a run reports all of them at once."""
    from rectifi import merchant_allowlist, sealed_fields

    problems = []
    for label, forms in proof.scan_text(dump.raw, {**words, **{f"SECRET {k}": v for k, v in secrets.items()}}).items():
        problems.append(f"[{stage}] '{label}' is readable in the dump ({', '.join(forms)}; read with {dump.source})")
    sealed = {t: s for t, s in sealed_fields.SEALED.items() if s.enforce and not s.when}
    brands = brand_words()
    allowed_keys = {b.key for b in merchant_allowlist.ALLOWED}
    for uid in uids:
        for table, column, owner, value in dump.cells:
            if owner != uid or value is None or table not in sealed:
                continue
            spec = sealed[table]
            if column in spec.sealed:
                if not value.startswith(("rx1.", "rs1.")):
                    problems.append(f"[{stage}] user {uid}: {table}.{column} holds plain text, not an envelope")
                lowered = value.lower()
                problems += [f"[{stage}] user {uid}: brand '{b}' sits in {table}.{column}" for b in brands if b in lowered]
            elif column in spec.blind and not value.startswith("b1:"):
                problems.append(f"[{stage}] user {uid}: {table}.{column} is not a keyed hash")
        keys = {v for t, c, owner, v in dump.cells if (t, c, owner) == ("transactions", "merchant_key", uid) and v is not None}
        if not keys <= allowed_keys:
            problems.append(f"[{stage}] user {uid}: merchant_key holds a value that is not on the allow-list: {sorted(keys - allowed_keys)}")
    return problems


def count_envelopes(uid: int, prefix: str) -> int:
    from rectifi import sealed_fields

    total = 0
    with proof.raw_conn(uid) as conn:
        for table, column in sealed_fields.all_sealed():
            total += int(conn.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE %s', (prefix + "%",)).fetchone()[0])
    return total


def setup(monkeypatch, caplog, multiuser: bool):
    caplog.set_level(logging.DEBUG)
    if multiuser:
        proof.multiuser_env(monkeypatch)
    proof.pin_clock(monkeypatch)
    proof.fake_push(monkeypatch)
    return proof.make_app(), proof.gateway_client()


def log_problems(caplog, words, secrets) -> list[str]:
    text = caplog.text + "\n" + "\n".join(str(r.getMessage()) + str(r.exc_text or "") for r in caplog.records)
    found = proof.scan_text(text, {**words, **{f"SECRET {k}": v for k, v in secrets.items()}})
    return [f"[log] '{label}' appears in the log ({', '.join(forms)})" for label, forms in found.items()]


# ───────────────────────── the scanner itself ─────────────────────────

def test_the_scanner_sees_every_form_of_a_canary():
    word = "zqxcanarysmoke7k2m"
    import base64

    for label, text in {
        "plain": f"x {word} y", "upper": f"x {word.upper()} y", "hex": f"\\x{word.encode().hex()}",
        "base64": base64.b64encode(f"{word}".encode()).decode(), "base64 shifted": base64.b64encode(f"ab{word}".encode()).decode(),
        "base64 shifted 2": base64.b64encode(f"a{word}z".encode()).decode(), "url-safe": base64.urlsafe_b64encode(f"??>>{word}".encode()).decode(),
    }.items():
        assert proof.scan_text(text, {"c": word}), f"the scan missed the {label} form"
    assert not proof.scan_text("nothing to see: rx1.AAAA rs1.BBBB b1:cccc", {"c": word})


# ───────────────────────── a friend ─────────────────────────

def test_a_friends_text_is_never_readable_in_a_dump(monkeypatch, caplog):
    app, gw = setup(monkeypatch, caplog, True)
    friend = proof.new_friend(app, "carol")
    c = proof.Canaries("f")
    problems: list[str] = []
    snapshots: dict = {}

    def while_signed_out():
        snapshots["dump"] = proof.dump_database()

    extra = flows(friend.client, gw, c, Auth(friend), on_signed_out=while_signed_out)
    words, secrets = c.sealed_words(), secrets_of(friend, extra)
    problems += check("signed out", snapshots["dump"], words, secrets, [friend.uid])
    assert any(owner == friend.uid and v and v.startswith("rs1.") for _, _, owner, v in snapshots["dump"].cells), \
        "a signed-out SMS should be stored as an rs1 envelope (sealed to the person's public key), and none is"
    assert count_envelopes(friend.uid, "rs1.") > 0

    friend.login()          # the one-minute ticker has no data key, so an alert it wrote in the flow is an rs1 envelope until the next sign-in
    for _ in range(60):                                        # sign-in opens the rs1 values (hooks.after_sign_in): give it a moment
        if count_envelopes(friend.uid, "rs1.") == 0:
            break
        proof.time.sleep(0.25)
    else:
        problems.append("after sign-in some rs1 envelopes were never opened into rx1")
    problems += check("signed in again", proof.dump_database(), words, secrets, [friend.uid])
    problems += log_problems(caplog, words, secrets)
    assert not problems, "Plain text or secrets found:\n  " + "\n  ".join(problems)


def test_the_brand_list_is_the_only_plain_merchant_text(monkeypatch, caplog):
    """A payment to a listed brand gets a plain merchant_key (so a cap can fire signed out); a payment to anyone else gets none."""
    app, gw = setup(monkeypatch, caplog, True)
    friend = proof.new_friend(app, "dave")
    token = friend.post("/api/phone/devices", {"name": "dave phone"}).json()["token"]
    friend.post("/api/accounts", {"name": "Main", "last4": "1234"})
    word = "zqxlonepayee" + os.urandom(2).hex()
    friend.logout()
    for text in (proof.sms_debit("71.71", "swiggy"), proof.sms_debit("72.72", word)):
        reply = proof.gateway_client().post("/sms", headers={"Authorization": f"Bearer {token}"}, json={"from": "AX-HDFCBK", "text": text})
        assert reply.json() == {"status": "added"}
    with proof.raw_conn(friend.uid) as conn:
        keys = sorted(r[0] or "" for r in conn.execute("SELECT merchant_key FROM transactions").fetchall())
    assert keys == ["", "swiggy"], f"expected one brand key and one empty, got {keys}"
    problems = check("brand", proof.dump_database(), {"payee": word}, {}, [friend.uid])
    assert not problems, "\n  ".join(problems)


# ───────────────────────── the owner ─────────────────────────

def test_the_owners_old_plain_data_is_gone_from_a_dump_after_the_sweep(monkeypatch, caplog):
    app, gw = setup(monkeypatch, caplog, False)
    before = proof.Canaries("o")
    flows(proof.new_client(app), gw, before, Auth())           # laptop mode: plain, as it has always been
    assert proof.plain_sealed_cells(1) > 0, "the owner's old rows should start out plain (that is the point of the sweep)"

    proof.multiuser_env(monkeypatch)
    app = proof.make_app()
    owner = proof.sign_up(app, proof.make_invite(owner=True), "prag")
    assert owner.uid == 1, "an owner invite must claim row 1, not make a new user"
    proof.wait_for_sweep(1)

    after = proof.Canaries("p")
    extra = flows(owner.client, gw, after, Auth(owner))
    words = {**before.sealed_words(), **after.sealed_words()}
    secrets = secrets_of(owner, extra)
    problems = check("owner after sweep", proof.dump_database(), words, secrets, [1])
    problems += log_problems(caplog, words, secrets)
    assert not problems, "Plain text or secrets found:\n  " + "\n  ".join(problems)


# ───────────────────────── the sanity check ─────────────────────────

def test_the_same_flows_in_laptop_mode_are_found_by_the_same_scan(monkeypatch, caplog):
    """Laptop mode seals nothing. If the scan cannot see these canaries there, a clean result above would mean nothing."""
    app, gw = setup(monkeypatch, caplog, False)
    c = proof.Canaries("l")
    flows(proof.new_client(app), gw, c, Auth())
    dump = proof.dump_database()
    found = proof.scan_text(dump.raw, {label: w for label, w in c.must_find().items()})
    missing = sorted(set(c.must_find()) - set(found))
    assert not missing, f"the scan (read with {dump.source}) cannot find these canaries in a laptop-mode database: {missing}"
    assert len(dump.cells) > 100, "the cell reader returned almost nothing"
