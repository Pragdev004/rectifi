"""What happens right after a person signs in (multi-user plan, section 4 "After sign-in").

WS-AUTH calls `after_sign_in(ctx)` once the cookie is set, with the person's context bound. Each workstream owns ONE block
below, between its own begin/end markers, and edits nothing else in this file. Rules for a block:
  * import inside the function (rectifi.api must import without langchain),
  * never raise: a failure in one block is logged and the others still run,
  * work only for `ctx.uid`, and use `userctx.spawn` for anything that runs in the background.
This file is a stub: the blocks are empty until their workstream fills them.
"""

from __future__ import annotations

import logging

from . import userctx

log = logging.getLogger("rectifi.hooks")


# ═══ WS-CORE: begin ═══
CORE_SEALED_TXN = ("payee", "description", "raw_text", "note", "category_reason", "upi_ref_sealed")


def core_after_sign_in(ctx: userctx.UserContext) -> None:
    """Open 'rs1.' values (sealed while signed out) into 'rx1.', and re-run categorise layers 1 to 4 on rows with
    unlocked = false (never touching category_source = 'user'). Then those rows are unlocked: the same as if the SMS had
    arrived while signed in. Safe to run again: a second run finds nothing to do."""
    from . import categorize, db, vault                      # inside the function: rectifi.api must import without langchain

    if ctx.key is None:                                       # no key, nothing can be opened
        return
    with db.connect() as conn:
        # 1. the needs-review inbox: sealed to the public key while signed out, now sealed to the data key
        for row in conn.execute("SELECT id, sender, raw_text, reason FROM sms_inbox "
                                "WHERE left(sender, 4) = 'rs1.' OR left(raw_text, 4) = 'rs1.' OR left(reason, 4) = 'rs1.'").fetchall():
            conn.execute("UPDATE sms_inbox SET sender = %s, raw_text = %s, reason = %s WHERE id = %s",
                         (vault.seal(row["sender"]), vault.seal(row["raw_text"]), vault.seal(row["reason"]), row["id"]))
        # 2. payments that arrived signed out: amount, date, direction, brand and a first category were stored plain
        rows = conn.execute("SELECT id, direction, category_source, " + ", ".join(CORE_SEALED_TXN) +
                            " FROM transactions WHERE NOT unlocked ORDER BY id").fetchall()
        if not rows:
            return
        cat = categorize.load_context(conn)                   # now with the person's learned rules and look-alike index
        for row in rows:
            sets = {col: vault.seal(row[col]) for col in CORE_SEALED_TXN if col != "category_reason"}
            if row["category_source"] != "user":              # the person's own choice is never touched
                why = categorize.decide_with(cat, row["payee"], row["description"], row["direction"])
                suggested = cat.category_ids.get(why.suggested) if why.suggested else None
                sets.update(category_id=cat.category_ids[why.category], category_source=why.source,
                            category_confidence=why.confidence, category_reason=vault.seal(why.reason),
                            suggested_category_id=suggested, suggested_confidence=why.suggested_confidence if suggested else None)
            else:
                sets["category_reason"] = vault.seal(row["category_reason"])
            sets["unlocked"] = True
            names = list(sets)
            conn.execute("UPDATE transactions SET " + ", ".join(f"{n} = %s" for n in names) + " WHERE id = %s AND NOT unlocked",
                         [sets[n] for n in names] + [row["id"]])
# ═══ WS-CORE: end ═══


# ═══ WS-ALERTS: begin ═══
def alerts_after_sign_in(ctx: userctx.UserContext) -> None:
    """Catch up alerts on custom merchants (their caps are sealed, so they could not fire while signed out), and open alert
    texts that were sealed to the public key while the person was signed out. Quick: one read of the ledger. The alerts it
    writes are pushed by the next tick."""
    from . import alerts                                      # inside the function: rectifi.api must import without langchain

    alerts.after_sign_in()
# ═══ WS-ALERTS: end ═══


# ═══ WS-ACCOUNT: begin ═══
def account_after_sign_in(ctx: userctx.UserContext) -> None:
    """Every person: move text that was sealed while they were signed out (rs1.) to their data key (rx1.), in the tables
    WS-CORE's block does not cover. The owner: resume the sealing sweep if the claim left one pending or it stopped halfway
    (a background thread that keeps the owner's context; it is safe to start again)."""
    from . import config, seal_sweep

    if not config.multiuser() or ctx.key is None:
        return
    seal_sweep.drain_rs1(ctx)
    if seal_sweep.should_run(ctx.uid):
        userctx.spawn(seal_sweep.run_in_background, ctx, name="rectifi-seal-sweep")
# ═══ WS-ACCOUNT: end ═══


# ═══ WS-AI: begin ═══
def ai_after_sign_in(ctx: userctx.UserContext) -> None:
    """Owner only: requeue unfinished classify jobs and refresh the guides index. A friend gets nothing here (D7: AI is
    owner-only). Laptop mode never reaches this (the hook runs only in multi-user mode), and the start-up code still does both.
    The work runs in a thread that keeps the owner's context; the reindex skips the 'txn' corpus in multi-user mode (D8)."""
    from . import config

    if not config.multiuser() or ctx.uid != userctx.OWNER_UID or ctx.key is None:
        return
    from .agent import jobs                                   # inside the function: rectifi.api must import without langchain
    from .api.routes import knowledge

    jobs.requeue_unfinished()                                 # puts the owner's unfinished jobs on the worker, each with this context
    knowledge.auto_reindex(("guide", "note", "upload"))       # a thread (userctx.spawn); off when RECTIFI_NO_AUTO_REINDEX=1
# ═══ WS-AI: end ═══


_BLOCKS = (core_after_sign_in, alerts_after_sign_in, account_after_sign_in, ai_after_sign_in)


def after_sign_in(ctx: userctx.UserContext) -> None:
    """Run every workstream's block, in a fixed order, for the person who just signed in."""
    with userctx.bound(ctx):
        for block in _BLOCKS:
            try:
                block(ctx)
            except Exception as exc:                                # noqa: BLE001  (one block must not stop the others)
                log.error("sign-in hook %s failed (%s)", block.__name__, type(exc).__name__)   # type only: no text in logs
