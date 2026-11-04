"""Two (or more) people with keys, for the isolation tests.

    a, b = two_users()                 # user 1 (the owner) and a new user 2, both with data keys; neither is "encrypted" yet
    with as_user(a):                   # everything inside runs as that person: db.connect() sees only their rows
        tools.add_account("SBI", "1234")

`encrypted=True` flips users.encrypted, which turns on the database guard (enforce_sealed) for that person.
Laptop mode is unchanged by any of this: a test that wants sealing calls monkeypatch.setenv("RECTIFI_MULTIUSER", "1") itself.
"""

from __future__ import annotations

from contextlib import contextmanager

from rectifi import db, userctx, vault


def _current_blind_fp() -> str | None:
    """What a real sign-up stores in users.blind_fp; None when a test has removed the server key."""
    try:
        return vault.blind_key_fingerprint()
    except Exception:  # noqa: BLE001 (no key configured: the row looks like one made before the key existed)
        return None


def make_user(username: str | None = None, *, uid: int | None = None, encrypted: bool = False) -> userctx.UserContext:
    """The person with this id (created when it does not exist), seeded with default categories and rules, with fresh keys."""
    public, private = vault.new_keypair()
    ingest = vault.new_ingest_salt()
    with db.connect_system("tests: make a user") as conn:
        if uid is None or conn.execute("SELECT 1 FROM users WHERE id = %s", (uid,)).fetchone() is None:
            row = conn.execute("INSERT INTO users (username) VALUES (%s) RETURNING id", (username or f"user{uid or ''}",)).fetchone()
            uid = row["id"]
        conn.execute("UPDATE users SET public_key = %s, ingest_salt = %s, encrypted = %s, confirmed_at = now(), blind_fp = %s "
                     "WHERE id = %s", (public, ingest, encrypted, _current_blind_fp(), uid))
    db.seed_user(uid)
    return userctx.UserContext(uid=uid, key=vault.new_data_key(), public_key=public, ingest_salt=ingest, strict=True, private_key=private)


def two_users(*, encrypted: bool = False) -> tuple[userctx.UserContext, userctx.UserContext]:
    return make_user("owner", uid=1, encrypted=encrypted), make_user("friend", encrypted=encrypted)


@contextmanager
def as_user(who):
    """Run a block as a person: a UserContext, or just a user id."""
    ctx = who if isinstance(who, userctx.UserContext) else userctx.UserContext(uid=int(who), strict=False)
    with userctx.bound(ctx):
        yield ctx


def count(table: str, who) -> int:
    with as_user(who), db.connect() as conn:
        return conn.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()["n"]


def populate(who, tag: str = "") -> dict:
    """One row in every tenant table for this person (default categories and rules are already there). Returns the useful ids.
    `tag` makes the global keys (device token hash, thread id) differ between callers."""
    ctx = who if isinstance(who, userctx.UserContext) else userctx.UserContext(uid=int(who), strict=False)
    uid = ctx.uid
    key = f"{tag}{uid}"
    with as_user(ctx), db.connect() as c:
        def one(sql, params=()):
            return c.execute(sql, params).fetchone()["id"]

        food = one("SELECT id FROM categories WHERE name = 'Food & Dining'")
        account = one("INSERT INTO accounts (name, last4) VALUES ('SBI', '1234') RETURNING id")
        batch = one("INSERT INTO import_batches (account_id, filename, added) VALUES (%s, 'x.csv', 1) RETURNING id", (account,))
        txn = one("INSERT INTO transactions (account_id, amount, direction, txn_date, payee, upi_ref, category_id, source, import_batch_id) "
                  "VALUES (%s, 250.50, 'debit', '2026-10-05', 'Swiggy', %s, %s, 'statement', %s) RETURNING id", (account, f"ref{key}", food, batch))
        c.execute("INSERT INTO budgets (category_id, month, limit_amount) VALUES (%s, '2026-10-01', 4000)", (food,))
        c.execute("INSERT INTO balance_snapshots (account_id, as_of, balance) VALUES (%s, '2026-10-01', 1000)", (account,))
        c.execute("INSERT INTO weekly_reviews (week_start, commitment) VALUES ('2026-10-26', 'no more than 3 orders')")
        device = one("INSERT INTO phone_devices (name, token_hash) VALUES ('Pixel', %s) RETURNING id", (f"hash{key}",))
        c.execute("INSERT INTO sms_inbox (device_id, sender, raw_text, text_hash) VALUES (%s, 'AX-BANK', 'text', %s)", (device, f"h{key}"))
        c.execute("INSERT INTO sms_seen (text_hash) VALUES (%s)", (f"h{key}",))
        c.execute("INSERT INTO classify_jobs (batch_id, total) VALUES (%s, 1)", (batch,))
        c.execute("INSERT INTO charts (title, prompt, code, kind, attempts) VALUES ('t', 'p', 'c', 'png', 1)")
        doc = one("INSERT INTO rag_documents (corpus, title, source, sha256, content) VALUES ('note', 'n', %s, 'x', 'body') RETURNING id", (f"note{key}",))
        c.execute("INSERT INTO rag_chunks (document_id, corpus, content, content_hash, embed_model, embedding) VALUES (%s, 'note', 'body', 'h', 'm', %s)",
                  (doc, [0.1, 0.2]))
        c.execute("INSERT INTO rag_chunks (txn_id, corpus, content, content_hash, embed_model, embedding) VALUES (%s, 'txn', 'body', 'h', 'm', %s)",
                  (txn, [0.1, 0.2]))
        c.execute("INSERT INTO conversations (thread_id, title) VALUES (%s, 'chat')", (f"thread-{key}",))
        c.execute("INSERT INTO trace_spans (run_id, root_run_id, name, run_type, started_at) "
                  "VALUES (gen_random_uuid(), gen_random_uuid(), 'step', 'chain', now())")
        c.execute("INSERT INTO merchant_patterns (pattern, merchant, grp) VALUES ('chaayos', 'Chaayos', 'Other')")
        rule = one("INSERT INTO alert_rules (scope, target, period, cap) VALUES ('total', 'all', 'week', 100) RETURNING id")
        alert = one("INSERT INTO alerts (dedupe_key, kind, severity, title, detail, push_title, push_body, numbers, rule_id, txn_id) "
                    "VALUES (%s, 'cap', 'info', 'T', 'D', 'pt', 'pb', '{}'::jsonb, %s, %s) RETURNING id", (f"k{key}", rule, txn))
        c.execute("INSERT INTO push_subscriptions (endpoint, p256dh, auth) VALUES (%s, 'p', 'a')", (f"https://push.example/{key}",))
    return {"uid": uid, "account": account, "batch": batch, "txn": txn, "food": food, "device": device, "doc": doc, "rule": rule, "alert": alert}
