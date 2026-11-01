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
def core_after_sign_in(ctx: userctx.UserContext) -> None:
    """Open 'rs1.' values (sealed while signed out) into 'rx1.', and re-run categorise layers 1 to 4 on rows with
    unlocked = false (never touching category_source = 'user')."""
# ═══ WS-CORE: end ═══


# ═══ WS-ALERTS: begin ═══
def alerts_after_sign_in(ctx: userctx.UserContext) -> None:
    """Catch up alerts on custom merchants (their caps are sealed, so they could not fire while signed out)."""
# ═══ WS-ALERTS: end ═══


# ═══ WS-ACCOUNT: begin ═══
def account_after_sign_in(ctx: userctx.UserContext) -> None:
    """Resume the owner's sealing sweep if it stopped halfway."""
# ═══ WS-ACCOUNT: end ═══


# ═══ WS-AI: begin ═══
def ai_after_sign_in(ctx: userctx.UserContext) -> None:
    """Owner only: requeue unfinished classify jobs and refresh the guides index."""
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
