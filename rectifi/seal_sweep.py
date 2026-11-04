"""The owner sweep: seal what was written in plain text before the owner claimed row 1 (multi-user plan D9, section 6 ACCOUNT).

Laptop mode stores everything plain. When the owner signs up with `dev.py invite --owner`, row 1 gets a password and a data key
but `users.encrypted` stays false and `sweep_state` is 'pending' (rectifi/users.py). This module then, in small batches:

  1. seals every plain value in every column of `sealed_fields.SEALED` (rx1. envelopes under the owner's data key),
  2. re-keys the dedupe columns exactly as WS-CORE writes them today, so an old statement imported again still finds its rows:
        transactions.upi_ref        blind_ingest(old reference)       (upi_ref_sealed = seal(old reference))
        transactions.fingerprint    blind(old fingerprint)
        sms_inbox.text_hash, sms_seen.text_hash   blind_ingest(old digest)   (sms_seen.digest_sealed = seal(old digest))
        merchant_rules / merchant_patterns .pattern_blind   blind(pattern)
        alert_rules.target_blind    blind(target), custom merchants only
  3. fills transactions.merchant_key from the allow-list where it is empty (read while the text is still readable),
     (and the muted / snoozed scope keys in the alert preferences get the same treatment),
  4. drops the library's copies of ledger text (rag_chunks of corpus 'txn'), because decision D8 keeps ledger text out of them,
  5. checks that nothing plain is left, then sets users.encrypted = true (the database trigger takes over from there).

Safe to stop and to run again: the work to do is "rows that still hold a plain value", so a second run finds nothing and changes
nothing. Progress is written to users.sweep_state after each batch ('pending' -> '{"stage": ..., "done": n, "total": t}' ->
'done'). A row is changed by one UPDATE inside its batch's transaction, so a crash never leaves half a row.

A dedupe key can clash while it is re-keyed (an old plain key and a new `b1:` key for the same payment both exist, because a
write landed between the claim and the sweep, or because the server's blind key changed). The clash never stops the work:
see `_apply` (the older copy of a payment is kept, a newer duplicate is dropped and counted; a rule or cap you set later wins).
`rekey_dedupe` does only the dedupe columns, fast, inside the owner's claim, so new writes find old rows at once.
`reblind` makes the ingest hashes again after the server's RECTIFI_BLIND_KEY changed (docs/handoffs/fix-round1.md).

Also here: drain_rs1(), run at every sign-in, which turns text sealed while the person was signed out (rs1.) into rx1.
for the tables WS-CORE's sign-in hook does not cover.

No langchain, no rectifi.api. Nothing is logged except counts and table names.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from typing import Callable

import psycopg

from . import account, config, db, merchant_allowlist, sealed_fields, userctx, vault

log = logging.getLogger("rectifi.seal_sweep")

BATCH = 200
OWNER_UID = userctx.OWNER_UID

# How to find one row to update. Most tables have `id`; two do not.
KEY = {"weekly_reviews": "week_start", "sms_seen": "text_hash"}

# How a keyed-hash column is made: (how, which column holds the readable value; None = the column itself).
INGEST, BLIND = "ingest", "blind"
BLIND_RULES: dict[tuple[str, str], tuple[str, str | None]] = {
    ("transactions", "upi_ref"): (INGEST, None),
    ("transactions", "fingerprint"): (BLIND, None),
    ("merchant_rules", "pattern_blind"): (BLIND, "pattern"),
    ("merchant_patterns", "pattern_blind"): (BLIND, "pattern"),
    ("sms_inbox", "text_hash"): (INGEST, None),
    ("sms_seen", "text_hash"): (INGEST, None),
    ("alert_rules", "target_blind"): (BLIND, "target"),
}

# WS-CORE's sign-in hook (hooks.core_after_sign_in) already drains these two; the generic drain skips them.
DRAINED_BY_CORE = ("transactions", "sms_inbox")

_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


# ───────────────────────── small helpers ─────────────────────────

def _plain(col: str) -> str:
    """SQL: this cell holds readable text (not NULL, not empty, not an envelope)."""
    return f"(\"{col}\" IS NOT NULL AND \"{col}\" <> '' AND \"{col}\" NOT LIKE 'rx1.%%' AND \"{col}\" NOT LIKE 'rs1.%%')"


def _plain_blind(col: str) -> str:
    """SQL: this cell holds something that is not a keyed hash."""
    return f"(\"{col}\" IS NOT NULL AND \"{col}\" <> '' AND \"{col}\" NOT LIKE 'b1:%%')"


def _hash(how: str, value: str) -> str:
    return vault.blind_ingest(value) if how == INGEST else vault.blind(value)


def _is_custom_merchant(target: str | None) -> bool:
    return bool(target) and merchant_allowlist.plain_key(target) is None


def _hide_key(key: str | None) -> str | None:
    """An alert's dedupe_key or scope_key with a custom merchant's name replaced by a keyed hash (plan section 3, item 7).
    'cap:merchant:<name>:...', 'streak:<name>:...', 'merchant:<name>:<period>'. A brand on the allow-list stays readable."""
    if not key:
        return key
    parts = key.split(":")
    at = None
    if parts[0] == "cap" and len(parts) >= 4 and parts[1] == "merchant":
        at = 2
    elif parts[0] == "streak" and len(parts) >= 2:
        at = 1
    elif parts[0] == "merchant" and len(parts) >= 3:
        at = 1
    if at is None or parts[at] == "b1" or not _is_custom_merchant(parts[at]):
        return key
    return ":".join(parts[:at] + [vault.blind(parts[at])] + parts[at + 1:])


def _hide_prefs(conn) -> int:
    """The alert preferences (app_settings 'alerts.prefs') list muted and snoozed scope keys such as 'streak:Chaayos'. A custom
    merchant's name in one is hidden the same way as in alerts.scope_key. Returns 1 when the row changed, else 0 (safe to repeat)."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'alerts.prefs'").fetchone()
    if not row or not isinstance(row["value"], dict):
        return 0
    prefs = dict(row["value"])
    muted = prefs.get("muted_scopes")
    snoozed = prefs.get("snoozed_scopes")
    new_muted = sorted({_hide_key(k) for k in muted}) if isinstance(muted, list) else muted
    new_snoozed = {_hide_key(k): v for k, v in snoozed.items()} if isinstance(snoozed, dict) else snoozed
    if new_muted == muted and new_snoozed == snoozed:
        return 0
    prefs.update({"muted_scopes": new_muted, "snoozed_scopes": new_snoozed})
    conn.execute("UPDATE app_settings SET value = %s::jsonb, updated_at = now() WHERE key = 'alerts.prefs'", (json.dumps(prefs),))
    return 1


# ───────────────────────── what one row needs ─────────────────────────

def _todo_sql(table: str, spec: sealed_fields.Table) -> tuple[str, str]:
    """(flag columns, WHERE). SELECT * plus a flag per sealed ('_p_col') and blind ('_b_col') column, and the rows that may
    have work to do. A few rows can match and need nothing (a cap on a brand): `changes` decides, not the WHERE."""
    flags, tests = [], []
    for col in spec.sealed:
        flags.append(f"{_plain(col)} AS _p_{col}")
        tests.append(_plain(col))
    for col in spec.blind:
        flags.append(f"{_plain_blind(col)} AS _b_{col}")
        tests.append(_plain_blind(col))
    if table == "transactions":                             # an old row has the plain reference but no sealed copy yet
        tests.append("(upi_ref_sealed IS NULL AND upi_ref IS NOT NULL AND upi_ref <> '' AND upi_ref NOT LIKE 'b1:%%')")
    if table == "alerts":                                   # keys may hold a custom merchant's name
        tests.append("(dedupe_key ~ '^(cap:merchant|streak):' OR scope_key ~ '^(streak|merchant):')")
    where = "(" + " OR ".join(tests) + ")"
    if spec.when:
        where += f" AND \"{spec.when[0]}\" = '{spec.when[1]}'"
    return ", ".join(flags), where


def _alert_rule_changes(row: dict) -> dict:
    """Only a cap on a custom merchant is sealed; a brand, group, category or total cap keeps its plain target (D3, D4)."""
    if row["scope"] != "merchant" or not _is_custom_merchant(row["target"]):
        return {}
    out = {}
    if row["_p_target"]:
        out["target"] = vault.seal(row["target"])
    if row["_b_target_blind"]:
        out["target_blind"] = vault.blind(row["target"])
    return out


def changes(table: str, spec: sealed_fields.Table, row: dict) -> dict:
    """{column: new value} for one row (empty when it needs nothing). `row` holds the readable values (the connection opened
    anything already sealed) and the _p_ / _b_ flags that say which cells were plain in the database."""
    if table == "alert_rules":
        return _alert_rule_changes(row)
    out: dict = {}
    for col in spec.sealed:
        if row[f"_p_{col}"]:
            out[col] = vault.seal(row[col])
    for col in spec.blind:
        if row[f"_b_{col}"]:
            how, source = BLIND_RULES[(table, col)]
            out[col] = _hash(how, row[source or col])
    if table == "transactions":
        ref = row["upi_ref"]
        if row["upi_ref_sealed"] is None and ref and not ref.startswith("b1:"):
            out["upi_ref_sealed"] = vault.seal(ref)
        if out and row["merchant_key"] is None:
            brand = merchant_allowlist.plain_key(row["payee"], row["description"])      # read before the text is sealed
            if brand:
                out["merchant_key"] = brand
    if table == "sms_seen" and "text_hash" in out and row["digest_sealed"] is None:
        out["digest_sealed"] = vault.seal(row["text_hash"])         # keep the plain digest, sealed, so the hash can be remade later
    if table == "alerts":
        for col in ("dedupe_key", "scope_key"):
            hidden = _hide_key(row[col])
            if hidden != row[col]:
                out[col] = hidden
    return out


# ───────────────────────── clashes while re-keying ─────────────────────────

# A re-keyed row finds another row that already holds the target key. Both are the same thing (the unique index says so).
# Money and SMS records: keep the OLD row (it carries the person's category and note) and drop the newer duplicate.
# Rules and caps: the newer one is what the person last chose, so it wins and the old copy is dropped.
KEEP_NEWER = ("merchant_rules", "merchant_patterns", "alert_rules")
# Columns a clash is looked for in, and the extra columns that make the unique index (besides user_id).
CLASH_SCOPE = {("transactions", "upi_ref"): ("direction",), ("alert_rules", "target_blind"): ("scope", "period")}


def _others(conn, table: str, key: str, row: dict, col: str, value: str) -> list:
    extra = CLASH_SCOPE.get((table, col), ())
    sql = f'SELECT "{key}" AS k FROM "{table}" WHERE "{col}" = %s AND "{key}" <> %s'
    params: list = [value, row[key]]
    for name in extra:
        sql += f' AND "{name}" = %s'
        params.append(row[name])
    return [r["k"] for r in conn.execute(sql + " FOR UPDATE", params).fetchall()]


def _drop_duplicate(conn, table: str, key: str, kept: dict, dropped_key) -> None:
    """Delete the duplicate row, first handing its category choice to the kept row when the person made one only there."""
    if table == "transactions":
        other = conn.execute("SELECT category_id, category_source, category_confidence FROM transactions WHERE id = %s", (dropped_key,)).fetchone()
        if other and other["category_source"] == "user" and kept["category_source"] != "user":
            conn.execute("UPDATE transactions SET category_id = %s, category_source = 'user', category_confidence = %s WHERE id = %s",
                         (other["category_id"], other["category_confidence"], kept[key]))
    conn.execute(f'DELETE FROM "{table}" WHERE "{key}" = %s', (dropped_key,))


def _apply(conn, table: str, key: str, row: dict, new: dict, spec: sealed_fields.Table) -> tuple[int, int]:
    """Write `new` to one row. Returns (rows changed, duplicates dropped). Never raises on a dedupe clash."""
    merged = 0
    for attempt in (1, 2):
        for col in (c for c in spec.blind if c in new):
            for other in _others(conn, table, key, row, col, new[col]):
                if table in KEEP_NEWER:                          # the already-keyed row is the newer choice: the old row goes
                    conn.execute(f'DELETE FROM "{table}" WHERE "{key}" = %s', (row[key],))
                    return 1, 1
                _drop_duplicate(conn, table, key, row, other)
                merged += 1
        names = list(new)
        try:
            with conn.transaction():                              # a savepoint: a clash here undoes only this row's update
                conn.execute(f'UPDATE "{table}" SET ' + ", ".join(f'"{n}" = %s' for n in names) + f' WHERE "{key}" = %s',
                             [new[n] for n in names] + [row[key]])
            return 1, merged
        except psycopg.errors.UniqueViolation:                   # a write landed after the look: look again, once
            if attempt == 2:
                raise
    return 0, merged                                             # unreachable


# ───────────────────────── running ─────────────────────────

def _specs() -> dict[str, sealed_fields.Table]:
    """Every table in the registry. A keyed-hash column with no rule here is an error, never silently skipped."""
    for table, col in sealed_fields.all_blind():
        if (table, col) not in BLIND_RULES:
            raise NotImplementedError(f"seal_sweep has no rule for the keyed hash {table}.{col}: add it to BLIND_RULES.")
    return dict(sealed_fields.SEALED)


def _fetch(conn, table: str, spec: sealed_fields.Table, size: int, after: int | None, only_dedupe: bool = False) -> list[dict]:
    flags, where = _todo_sql(table, spec)
    if only_dedupe:
        where = _dedupe_where(table, spec)
    key = KEY.get(table, "id")
    sql = f'SELECT *, {flags} FROM "{table}" WHERE {where}'
    params: list = []
    if key == "id" and after is not None:
        sql += " AND id > %s"
        params.append(after)
    sql += f' ORDER BY "{key}" LIMIT {int(size)} FOR UPDATE SKIP LOCKED'
    return conn.execute(sql, params or None).fetchall()


def _write_row(conn, table: str, spec: sealed_fields.Table, key: str, row: dict, new: dict) -> tuple[int, int]:
    """Apply one row's changes. (rows changed, duplicates dropped)."""
    if table == "alerts" and "dedupe_key" in new and conn.execute(
            "SELECT 1 FROM alerts WHERE dedupe_key = %s AND id <> %s", (new["dedupe_key"], row["id"])).fetchone():
        # the sign-in catch-up already wrote this alert under the hidden key: the old copy is history that would only repeat it
        # (and the unique key would refuse the rename)
        conn.execute("DELETE FROM alerts WHERE id = %s", (row["id"],))
        return 1, 0
    return _apply(conn, table, key, row, new, spec)


def _dedupe_where(table: str, spec: sealed_fields.Table) -> str:
    """Rows whose dedupe keys are still plain (or, for alerts, name a custom merchant): what the claim re-keys at once."""
    tests = [_plain_blind(c) for c in spec.blind]
    if table == "transactions":
        tests.append("(upi_ref_sealed IS NULL AND upi_ref IS NOT NULL AND upi_ref <> '' AND upi_ref NOT LIKE 'b1:%%')")
    if table == "alerts":
        tests.append("(dedupe_key ~ '^(cap:merchant|streak):' OR scope_key ~ '^(streak|merchant):')")
    where = "(" + " OR ".join(tests) + ")"
    if spec.when:
        where += f" AND \"{spec.when[0]}\" = '{spec.when[1]}'"
    return where


DEDUPE_SEALED = ("upi_ref_sealed", "digest_sealed")          # the plain value behind a hash, kept sealed so the hash can be remade


@contextmanager
def _scope(conn, ctx):
    """The caller's connection (one transaction, see `rekey_dedupe`), or a fresh one for this person."""
    if conn is None:
        with db.connect() as own:
            yield own
    else:
        with db.tenant_scope(conn, ctx) as shared:
            yield shared


def rekey_dedupe(ctx: userctx.UserContext, *, batch: int = 500, conn=None) -> dict:
    """Make every dedupe key of this person's old rows a keyed hash NOW (HMACs and small seals only, no text work), so a payment
    written before the owner's claim is found by a statement or SMS that arrives after it. Run inside the owner's claim, before
    the first cookie is issued; the slow sealing of text stays with `run`. Safe to run again. A clash drops a duplicate (see `_apply`).
    With `conn` (the claim's own open transaction) everything runs on that one connection and nothing is committed here, so the
    re-key and the confirm stand or fall together; any error is raised to the caller. Returns {"rekeyed": n, "merged": n}."""
    if not config.multiuser() or ctx.key is None:
        return {"rekeyed": 0, "merged": 0}
    done = merged = 0
    with userctx.bound(ctx):
        for table, spec in _specs().items():
            if not spec.blind and table != "alerts":
                continue
            allowed = set(spec.blind) | set(DEDUPE_SEALED) | ({"dedupe_key", "scope_key"} if table == "alerts" else set())
            key, after = KEY.get(table, "id"), None
            while True:
                with _scope(conn, ctx) as cx:
                    rows = _fetch(cx, table, spec, batch, after, only_dedupe=True)
                    changed = 0
                    for row in rows:
                        new = {k: v for k, v in changes(table, spec, row).items() if k in allowed}
                        if new:
                            did, dropped = _write_row(cx, table, spec, key, row, new)
                            changed, merged = changed + did, merged + dropped
                done += changed
                if not rows:
                    break
                if key == "id":
                    after = rows[-1]["id"]
                elif not changed:
                    break
        with _scope(conn, ctx) as cx:
            done += _hide_prefs(cx)
    if merged:
        log.warning("re-keying dedupe keys merged %s duplicate rows", merged)
    return {"rekeyed": done, "merged": merged}


def remaining() -> int:
    """How many rows still hold a plain value (for the current person). 0 means the sweep is complete."""
    total = 0
    for table, spec in _specs().items():
        after = None
        while True:
            with db.connect() as conn:
                rows = _fetch(conn, table, spec, 500, after)
            if not rows:
                break
            total += sum(1 for r in rows if changes(table, spec, r))
            if KEY.get(table, "id") != "id":
                break                                          # no id to page by: any matched row is work, so one page tells
            after = rows[-1]["id"]
    return total


def _count_todo(table: str, spec: sealed_fields.Table) -> int:
    _, where = _todo_sql(table, spec)
    with db.connect() as conn:
        return conn.execute(f'SELECT count(*) AS n FROM "{table}" WHERE {where}').fetchone()["n"]


def _lock_for(uid: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(uid, threading.Lock())


def parse_state(state: str | None) -> dict:
    """{"phase": 'none' | 'pending' | 'running' | 'done', ...progress}"""
    if not state:
        return {"phase": "none"}
    if state in ("pending", "done"):
        return {"phase": state}
    try:
        data = json.loads(state)
    except ValueError:
        return {"phase": "pending"}
    return {"phase": "running", **data} if isinstance(data, dict) else {"phase": "pending"}


def should_run(uid: int) -> bool:
    """Is this the owner, with a claimed row 1 whose sweep has not finished?"""
    if uid != OWNER_UID or not config.multiuser():
        return False
    state, encrypted = account.sweep_state(uid)
    if state == account.DELETED_MARK:                  # the owner account was reset: nobody to seal for until a new claim
        return False
    return not encrypted and parse_state(state)["phase"] != "done"


def status(uid: int = OWNER_UID) -> dict:
    state, encrypted = account.sweep_state(uid)
    return {**parse_state(state), "encrypted": encrypted}


def run(ctx: userctx.UserContext, *, batch: int = BATCH, max_batches: int | None = None,
        on_batch: Callable[[str, int], None] | None = None) -> dict:
    """Seal everything plain for ctx's person. Returns {"finished", "sealed", "busy", "merged", "skipped"?}.
    `merged` counts duplicate rows dropped because a dedupe key clashed (see `_apply`); it is also logged.
    `max_batches` stops early (the next call carries on); `on_batch(table, changed)` is a hook for tests."""
    if not config.multiuser():
        return {"finished": False, "sealed": 0, "busy": False, "skipped": "laptop mode stores plain text on purpose"}
    if ctx.key is None:
        return {"finished": False, "sealed": 0, "busy": False, "skipped": "signed out: the data key is needed"}
    lock = _lock_for(ctx.uid)
    if not lock.acquire(blocking=False):
        return {"finished": False, "sealed": 0, "busy": True}
    try:
        specs = _specs()
        done = batches = merged = 0
        with userctx.bound(ctx):
            total = sum(_count_todo(t, s) for t, s in specs.items())
            with db.connect() as conn:                         # decision D8: no copies of ledger text in the library index
                conn.execute("DELETE FROM rag_chunks WHERE corpus = 'txn'")
            for table, spec in specs.items():
                key, after = KEY.get(table, "id"), None
                while True:
                    with db.connect() as conn:                 # one batch = one transaction
                        rows = _fetch(conn, table, spec, batch, after)
                        changed = 0
                        for row in rows:
                            new = changes(table, spec, row)
                            if new:
                                did, dropped = _write_row(conn, table, spec, key, row, new)
                                changed += did
                                merged += dropped
                    if not rows:
                        break
                    if key == "id":
                        after = rows[-1]["id"]
                    elif not changed:
                        break                                  # no id to page by and nothing moved: stop, never spin
                    done += changed
                    batches += 1
                    if not account.set_sweep_state(ctx.uid, json.dumps({"stage": table, "done": done, "total": total}), ctx.public_key):
                        log.warning("sweep: the account it was sealing is gone (deleted or claimed again); stopped")
                        return {"finished": False, "sealed": done, "busy": False, "merged": merged, "skipped": "the account was deleted"}
                    if on_batch:
                        on_batch(table, changed)
                    if max_batches is not None and batches >= max_batches:
                        return {"finished": False, "sealed": done, "busy": False, "merged": merged}
            with db.connect() as conn:
                done += _hide_prefs(conn)
            if remaining() != 0:                               # never switch the database guard on over plain text
                log.warning("sweep: some rows still hold plain text; encryption is not switched on yet")
                return {"finished": False, "sealed": done, "busy": False, "merged": merged}
            if not account.finish_sweep(ctx.uid, ctx.public_key):
                log.warning("sweep: the account it was sealing is gone (deleted or claimed again); stopped")
                return {"finished": False, "sealed": done, "busy": False, "merged": merged, "skipped": "the account was deleted"}
        log.info("sweep finished: %s rows sealed, %s duplicate rows merged", done, merged)
        return {"finished": True, "sealed": done, "busy": False, "merged": merged}
    finally:
        lock.release()


def run_in_background(ctx: userctx.UserContext) -> None:
    """The thread target the sign-in hook starts. A failure is logged by type only (a database error can quote a row) and
    the next sign-in tries again from where the work stopped."""
    try:
        run(ctx)
    except Exception as exc:                                   # noqa: BLE001
        log.error("sweep stopped (%s); it carries on at the next sign-in", type(exc).__name__)


# ───────────────────────── sign-in: the server blind key changed ─────────────────────────

def _reblind_sources(conn, table: str, after, size: int) -> list[dict]:
    """A page of rows whose ingest hash can be made again from data that is readable now (the connection opens sealed cells)."""
    if table == "transactions":
        return conn.execute("SELECT id, direction, category_source, upi_ref, upi_ref_sealed FROM transactions WHERE upi_ref LIKE 'b1:%%' "
                            "AND upi_ref_sealed IS NOT NULL AND id > %s ORDER BY id LIMIT %s FOR UPDATE", (after or 0, size)).fetchall()
    if table == "sms_inbox":
        return conn.execute("SELECT id, sender, raw_text, sent_at, received_at, text_hash FROM sms_inbox WHERE text_hash LIKE 'b1:%%' "
                            "AND raw_text IS NOT NULL AND id > %s ORDER BY id LIMIT %s FOR UPDATE", (after or 0, size)).fetchall()
    return conn.execute("SELECT text_hash, digest_sealed FROM sms_seen WHERE digest_sealed IS NOT NULL AND text_hash LIKE 'b1:%%' "
                        "AND text_hash > %s ORDER BY text_hash LIMIT %s FOR UPDATE", (after or "", size)).fetchall()


def _reblind_value(table: str, row: dict) -> str | None:
    from . import tools                                         # inside the function: tools imports a lot

    if table == "transactions":
        plain = row["upi_ref_sealed"]
    elif table == "sms_inbox":
        day = row["received_at"].date() if row["received_at"] else None
        plain = tools._sms_digest(row["sender"], row["raw_text"], row["sent_at"], day)        # noqa: SLF001
    else:
        plain = row["digest_sealed"]
    return vault.blind_ingest(plain) if plain and not vault.is_envelope(plain) else None


def reblind(ctx: userctx.UserContext, *, batch: int = 500) -> dict:
    """Make this person's ingest hashes (upi_ref, SMS digests) again under the CURRENT server blind key, from their own
    decrypted data, then remember the key's fingerprint. Needed when RECTIFI_BLIND_KEY was lost or rotated: the stored hashes
    were made with the old key and would no longer match anything new. No row is lost. An old hash that now clashes with one
    written since (a signed-out SMS) is merged like any dedupe clash (`_apply`). Safe to run again. Returns {"rebuilt", "merged"}."""
    if not config.multiuser() or ctx.key is None:
        return {"rebuilt": 0, "merged": 0}
    lock = _lock_for(-ctx.uid)                                   # a lock of its own: the sweep may be running
    if not lock.acquire(blocking=False):
        return {"rebuilt": 0, "merged": 0, "busy": True}
    rebuilt = merged = 0
    try:
        with userctx.bound(ctx):
            for table, col in (("transactions", "upi_ref"), ("sms_inbox", "text_hash"), ("sms_seen", "text_hash")):
                spec, key, after = sealed_fields.SEALED[table], KEY.get(table, "id"), None
                while True:
                    with db.connect() as conn:
                        rows = _reblind_sources(conn, table, after, batch)
                        for row in rows:
                            new = _reblind_value(table, row)
                            if new and new != row[col]:
                                did, dropped = _apply(conn, table, key, row, {col: new}, spec)
                                rebuilt, merged = rebuilt + did, merged + dropped
                    if not rows:
                        break
                    after = rows[-1][key]
        account.set_blind_fp(ctx.uid, vault.blind_key_fingerprint())
    finally:
        lock.release()
    if rebuilt or merged:
        log.info("blind key changed: %s ingest hashes rebuilt, %s duplicate rows merged", rebuilt, merged)
    return {"rebuilt": rebuilt, "merged": merged}


def blind_key_changed(uid: int) -> bool:
    """Was this person's last set of ingest hashes made with a different server blind key (or an unknown one)?"""
    return account.blind_fp(uid) != vault.blind_key_fingerprint()


def reblind_in_background(ctx: userctx.UserContext) -> None:
    try:
        reblind(ctx)
    except Exception as exc:                                    # noqa: BLE001
        log.error("re-keying after a blind key change stopped (%s); it carries on at the next sign-in", type(exc).__name__)


# ───────────────────────── sign-in: rs1. -> rx1. ─────────────────────────

def drain_rs1(ctx: userctx.UserContext) -> int:
    """Text sealed while the person was signed out (rs1., to their public key) becomes rx1. under the data key, in every
    sealed column except the two tables WS-CORE's sign-in hook already handles. Returns how many cells moved. Safe to run again."""
    if not config.multiuser() or ctx.key is None:
        return 0
    moved = 0
    with userctx.bound(ctx):
        for table, spec in sealed_fields.SEALED.items():
            if table in DRAINED_BY_CORE or not spec.sealed:
                continue
            key = KEY.get(table, "id")
            cols = list(spec.sealed)
            flags = ", ".join(f"left(\"{c}\", 4) = 'rs1.' AS _r_{c}" for c in cols)
            test = " OR ".join(f"left(\"{c}\", 4) = 'rs1.'" for c in cols)
            try:
                with db.connect() as conn:                     # the connection opens rs1. values with the private key
                    for row in conn.execute(f'SELECT *, {flags} FROM "{table}" WHERE {test} FOR UPDATE').fetchall():
                        sets = {c: vault.seal(row[c]) for c in cols if row[f"_r_{c}"] and row[c] is not None}
                        if sets:
                            conn.execute(f'UPDATE "{table}" SET ' + ", ".join(f'"{c}" = %s' for c in sets) + f' WHERE "{key}" = %s',
                                         list(sets.values()) + [row[key]])
                            moved += len(sets)
            except vault.VaultError:
                log.warning("drain: a value in %s could not be opened and was left as it is", table)
    return moved
