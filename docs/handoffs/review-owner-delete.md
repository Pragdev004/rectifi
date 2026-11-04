# Review: owner account delete (ws/owner-delete, 8f73ef8 vs master 08ab92a)

Reviewer did not write the change. Read-only on code. Probes are in the session scratchpad (`ownerdel/test_probe_ownerdel.py`), not the repo.
Ran, one process at a time, on `rectifi_fix_test`: `tests/test_owner_delete_complete.py` 5 passed; `tests/test_account.py` 22 passed;
`tests/test_rls.py` 16 passed; 3 probes passed (output quoted below).

## Q2: can a non-owner write to the library tables? No. So the whole-table delete is not a cross-person loss.
Every writer of `checkpoints*` / `store*` is an AI path: `api/ask.py:84,102` (Ask), `api/routes/charts.py:68`, `api/routes/memory.py:30`,
`agent/graph.py:237` (Streamlit, which refuses multi-user, `app/streamlit_app.py:42`). None is in `api/security.py:257-280 _FRIEND_RULES`.
Classify jobs, alerts, the gateway, rag indexing and Explain use no checkpointer or store. The MCP server refuses multi-user.
So the wipe is safe because of rule 20, not because of the friend check (see LOW-1).

## Findings

**[MED] An owner request still running during the delete leaves owner rows behind.** `rectifi/account.py:244-252`.
The wipe (one transaction) commits first; the owner's sessions are deleted and `session_epoch` bumped only afterwards, in a second
transaction (`:246`), and `seed_user` is a third. An Ask turn streaming at that moment (or a second tab) keeps writing checkpoints and a
`conversations` row for user 1. Probe (write between wipe and reset): `sessions alive between wipe and reset: 2`,
`library after delete: checkpoints 4, checkpoint_blobs 4, checkpoint_writes 5`, `conversations after delete: 1`. The next owner of row 1
would see that thread in the chat list: the "server changes hands" case PROGRESS.md was worried about.
Fix: in one `connect_system` transaction, lock (`SELECT ... FROM users WHERE id = 1 FOR UPDATE`, see LOW-1), revoke sessions and bump the epoch
FIRST, then wipe (via `db.tenant_scope(conn, owner_ctx)`), reset the row and the invite, commit. Also drain or refuse the owner's Ask locks
(`api/ask.py`) before the wipe, or wipe the library tables a second time after the reset. Add a test that writes between the steps.

**[LOW-1] The friend refusal is a check, not a lock, and runs in its own transaction.** `rectifi/account.py:240-244`.
`_friend_count` commits before the wipe starts; nothing stops `users.signup` inserting a friend row in between. Probe (signup injected
before the wipe): `users after owner delete: [{id 1, 'owner', unclaimed}, {id 2, 'late-friend'}]`. No data is lost (friend tenant rows are
RLS-scoped, friends never write library tables), but PRIVACY.md:118's precondition does not hold and `_wipe_owner_rows` itself has no guard.
Fix: `LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE` (blocks INSERT) plus the count, in the same transaction as the wipe; or re-check the
count inside `_wipe_owner_rows` and raise.

**[LOW-2] Not one transaction end to end.** `rectifi/account.py:213` says "in ONE transaction"; true only for the wipe. If the reset at
`:245-251` fails, the data is gone but the account is still claimed with live sessions. Retrying works (the wipe is idempotent), so low.
Same fix as MED. The savepoints are fine: only `UndefinedTable` is swallowed (`:228`); probe raising `InsufficientPrivilege` on
`DELETE FROM "store"` gave `rollback tenant equal: True`, library counts unchanged, `owner still claimed: True`.

**[LOW-3] PRIVACY.md backups line is not exact for the owner.** `docs/PRIVACY.md:119-120` says backups keep "your encrypted text"; the owner's
AI tables are plain this phase (`account.py` LIMITS, rule 20 / D8), so a backup keeps the owner's chat threads and memories readable for
7 days. `web/src/pages/Settings.tsx:138` ("Backups can keep sealed copies") has the same gap for the owner.
Fix: one sentence: "For the owner, backups also keep the chat, charts, library and traces as plain text for up to 7 days."
Also `:118`'s "so those tables hold nobody else's rows" should give the real reason: friends cannot use the AI (rule 20).

**[LOW-4] The scan skips custom categories.** `tests/test_owner_delete_complete.py:171` skips the RESEEDED tables, then checks only
`app_settings` and `merchant_rules` (`:178-180`); a custom owner category left in `categories` would pass. `tests/test_account.py`'s
order-covers-TENANT_TABLES check catches removing `categories` from the list, so the risk is small. Fix: assert every `categories` row is a
default (by name set from `schema.sql`).

## What holds
- Q3 completeness: the scan walks every `pg_tables` public table and fails on an unplaced one; tenant tables, all 5 library data tables,
  `user_sessions`, the owner's `invites` row and `blind_fp` / keys / salts on row 1 are cleared. Open friend invites stay (operator-made
  via `dev.py`, not owner rows). `*_migrations` hold versions only.
- Q4: the end-to-end test seeds a listed AND an unlisted real LangGraph thread plus a store item, and would catch a missed library table
  or tenant table. The direct `_wipe_owner_rows` test proves RLS scoping for tenant rows only; it would not notice that the function
  empties library tables with a friend present (by design, see Q2).
- PROGRESS.md gap line is updated truthfully.

Verdict: MERGE (no blocker; fix MED and LOW-3 before a server ever changes hands).

## Round 2 (8f73ef8..4918589, HEAD 4918589)
On `rectifi_fix_test`, one at a time: `test_owner_delete_complete.py` 12 passed, `test_web_dist.py` 6 passed 2 skipped, 5 probes (`ownerdel2/`).

| Round 1 | Status | Evidence |
|---|---|---|
| MED in-flight write | PARTIAL | The between-steps gap is closed: lock, friend check, session revoke, epoch bump, wipe and reset are one `connect_system` transaction (`account.py:248-267`); an old cookie gets 401 (test). A write that lands AFTER `_sweep_owner_stragglers` still survives, see BLOCKER. |
| LOW-1 friend check | RESOLVED | `LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE` + count in the same transaction; `_friend_count` counts unconfirmed rows too. Test shows the sign-up queued behind the lock. |
| LOW-2 one transaction | RESOLVED | Failed-reset test keeps data and sessions. Probe (`InsufficientPrivilege` on `store`): `rollback tenant equal: True library equal: True`, `owner still claimed: True sessions: 2`. |
| LOW-3 backups text | RESOLVED | PRIVACY.md:121-123, `LIMITS`, Settings.tsx:138-139 name the owner's plain AI tables. web/dist rebuilt. |
| LOW-4 categories | RESOLVED | Test asserts the category names equal `DEFAULT_CATEGORIES` and a seeded custom one is gone. |

**[BLOCKER] A write after the last sweep is inherited by the next owner.** The sweep runs once, right after `drain`. Then nothing
wipes row 1 again: `users.confirm_signup` for the owner keeps every row-1 row on purpose (`_rekey_owner_dedupe`, the single-user
migration path). Writers that can still land after the sweep:
1. A Charts-page run (`api/routes/charts.py:_stream`, its own `_busy` lock and worker) is never drained. It writes checkpoints, a
   `charts` row (`charts/graph.py:172`) and trace spans. A local model run takes tens of seconds; this is the widest window.
2. An Ask turn that does not reach a stop point within 15 s (`drain_turns` gives up silently; stops fire only at a model start or
   token, never inside a tool or a slow first token). Probe: `delete returned ok after 0.47 s; turn still running: True`, then
   `conversations 1 charts 1`.
3. A stop set while a turn is in `prepare` is erased: `stream_turn` calls `turn.cancelled.clear()` AFTER `prepare` (`ask.py:209`).
   Probe: `stop flag set by drain during prepare: True | flag seen by the turn's work: False`; the turn then runs to the end.
4. A request that passed the session check just before the commit and reaches `turn_for` after drain's snapshot (narrow).
Probe (late write after `delete_account` returned, then a new owner claims row 1): `new owner's chat list: ['late-after-sweep']`,
`chart gallery: ['late-after-sweep']`, library `checkpoints 4`. That is a cross-person read, and window 1 is plausible (two tabs).
**Smallest fix:** make the claim clean row 1 when it was emptied by a delete. Mark it in `_OWNER_RESET` (for example
`sweep_state = 'deleted'`, and make `users._reset_owner_claim` keep that value instead of NULL), and in `confirm_signup`'s owner
branch, in the same transaction and before `_rekey_owner_dedupe`, run `account._wipe_owner_rows_on` under `db.tenant_scope` plus
`db._migrate_categories` when marked (confirm then sets `sweep_state = 'pending'` as today). Test: delete, late conversation +
chart + checkpoint, claim, the new owner sees none. Also: drain the Charts run, clear `cancelled` before `prepare`, log a drain timeout.

**[LOW-5] A deadlock surfaces as a confusing error.** Probe: an owner transaction that updated a `conversations` row, then inserted
an `app_settings` row while the delete ran: `{'delete': 'InFailedSqlTransaction', 'writer': 'committed'}`, owner data intact.
Postgres broke the deadlock correctly; the delete rolled back whole and a retry works. But `db.tenant_scope`'s `finally`
(`db.py:195`) runs `set_config` in the aborted transaction and hides `DeadlockDetected`. Lock order with confirm/purge is fine:
the table lock comes first, confirm's row lock (ROW SHARE) does not conflict, and an unconfirmed friend makes the delete refuse.
NIT: epoch bumped twice in the delete plus once by the sweep (harmless); `account.py:17` still says backups hold "the encrypted text".

**(c) Docs.** Backups text: exact. PRIVACY.md:119-120 "stops any chat answer ... clears what that answer wrote" is not exact (a Charts
run is not stopped, a turn past 15 s is not); Settings "Everything you own is deleted" holds only after the BLOCKER fix.

Verdict: DO NOT MERGE (fix the BLOCKER: claim wipes a deleted row 1; then re-run these probes).

## Round 3 (4918589..30d4b84, HEAD 30d4b84)
On `rectifi_fix_test`, one process at a time: `test_owner_delete_complete.py` + `test_account.py` 41 passed; `test_api_charts.py`
+ `test_ai_mu.py` + `test_userctx.py` 85 passed; `test_chat_api.py` + `test_auth_multiuser.py` 69 passed. Round-2 probes re-run
(`ownerdel2/`, 5 passed) and 5 new probes (`ownerdel3/test_probe_ownerdel3.py`, 5 passed). Output quoted below.

| Round 2 | Status | Evidence |
|---|---|---|
| BLOCKER late write after the sweep | PARTIAL | A late write between the delete and the claim is now wiped: round-2 probe a gives `new owner's chat list: []`, `chart gallery: []`, library `checkpoints 0`. Two paths remain, see NOT CLEAN below. |
| Stop cleared during prepare | RESOLVED | `flag seen by the turn's work: True` (`ask.py:207`). |
| Charts run not drained | RESOLVED as far as it goes | `charts.drain_runs` + `_stop`; the check runs only between stream events, and `finish` saves the chart before it emits (`charts/graph.py:172`), so a run inside `finish` still saves. Covered by the mark only if it lands before the claim. |
| Drain timeout silent | RESOLVED | Count-only warning lines (test). |
| LOW-5 deadlock hidden | RESOLVED | Probe now gives `{'delete': 'DeadlockDetected', 'writer': 'committed'}`; data intact. |

**What holds.** The mark survives `purge_unconfirmed` / `_reset_owner_claim` (test), `db.init_db()` (probe: `sweep_state 'deleted'`
after it), and a confirm that crashes: `_rekey_owner_dedupe` raising gives 500, `sweep_state 'deleted'`, late rows still there
(rolled back with it); the retry gives 200, `'pending'`, the new owner sees `threads [] charts [] library 0`. A second cycle
(A deletes, B claims clean, B deletes, late write, C claims) leaves C with nothing. The claim wipe is the same
`_wipe_owner_rows_on` as the delete (every `_OWNER_ORDER` tenant table + the 5 library tables) plus the default categories,
in confirm's transaction under `FOR UPDATE` on row 1. It leaves the invite, the pending session and the people tables alone, as
it should. The laptop claim with no mark keeps its data (`test_a_normal_laptop_data_claim_keeps_its_data` passes).

**[BLOCKER-1] A sealing sweep that is still running erases the mark.** `seal_sweep.run` (`seal_sweep.py:440`, `:450`) keeps
going after the delete commits. It calls `account.set_sweep_state` (progress JSON) and then `account.finish_sweep` (`account.py:170-190`).
Neither checks that row 1 is still the person it was started for. The sweep starts at the owner's sign-in and runs in the
background for a laptop database being sealed, so a delete soon after a claim meets it. Probe: delete inside the sweep's first
batch: `state right after delete: {'sweep_state': 'deleted', 'encrypted': False}`, then `sweep result: finished True`,
`state after the sweep: {'sweep_state': 'done', 'encrypted': True, 'unclaimed': True}`. Then one late write and a new claim:
`new owner sees: {'threads': ['late-after-sweep-lost-mark'], 'charts': [...], 'library': 4}`. The same old sweep, still running
after a new claim, would also seal the NEW owner's plain rows under the OLD key. Also `should_run(1)` is True for a deleted row
(`parse_state('deleted')` is "pending"), so a login that races the delete starts one.
Fix: `set_sweep_state` / `finish_sweep` update only `WHERE id = %s AND public_key = %s` (the ctx's key) `AND confirmed_at IS NOT NULL`,
and `run` stops when that UPDATE changes 0 rows. `should_run` is False for `DELETED_MARK`. Test: delete during `on_batch`, mark still there.

**[BLOCKER-2] A write that lands after the claim commits is inherited.** The mark only covers writes before the claim. Nothing
on the write path refuses the old owner's context afterwards (`db.connect` sets only `app.user_id`). The AI tables are plain,
so the new owner reads it all.
- Probe (a stuck Ask turn, drain timed out, the new owner claims, then the turn's model call returns and it runs
  `conversation_touch` + a chart): right after the claim `threads [] charts [] library 0`. After the turn finished:
  `threads ['what did I spend on rent'], charts ['old owner rent chart'], library 4`. The preview and search text hold the
  masked answer, and the checkpoint holds the whole thread.
- Probe (the claim racing a late write): a conversations INSERT during confirm waits on confirm's `FOR UPDATE` on row 1 (the FK).
  `blocked during confirm: True`, then it commits right after the claim: `new owner sees: {'threads': ['raced-the-claim'], 'library': 4}`.
  A checkpoint written between the claim's wipe and its commit survives too (no FK, READ COMMITTED).
The window is one model call after a timed-out drain (`OLLAMA_TIMEOUT` 120 s by default, 60 s for other providers), a chart's
sandbox run, or a stopped turn's `_finish_stop` + `conversation_touch`, which always write. It is narrow for a hand-over, but it
is real: a new owner who signs up within about two minutes of the delete can inherit the thread.
Fix (smallest): in `confirm_signup`'s marked branch, refuse with 409 "still finishing; try again in a minute" while any
in-process AI work is running (a hook in `hooks.py` that asks `ask` for a held turn lock and `charts._busy`, so `users.py` does
not import the API). Do this check before the wipe. Robust: an in-process owner generation that `delete_account` bumps; a
`UserContext` for uid 1 carries the generation it was bound with, and `db.connect()` raises when they differ. That one check covers
turns, charts, classify jobs and imports. Test: both probes above.

**Secondary (not blocking)**
- Drain / stop-flag regressions: none found. 154 focused tests pass. A user's own Stop pressed during `prepare` now survives too (better).
  `drain_runs` leaves `_stop` set when no chart was running; the next run clears it. Ask then charts drain can take 20 s in all.
- `tenant_scope`: no regression. A healthy block still clears the setting. A failed block lets the real error through (probe:
  `DeadlockDetected`). A savepoint rollback reverts the local `set_config` anyway.
- PRIVACY.md:119-121 is not exactly true. "the next owner's sign-up wipes it again before they see anything" fails for
  BLOCKER-1 and BLOCKER-2. Also "given a few seconds" means up to 10 s for chat and 10 s for charts. After the fixes, say that
  sign-up waits until the old owner's work has stopped.
- Not checked: the classify worker (`agent/jobs.py`) and the alerts sweep after a delete. They seem to UPDATE wiped ids only, but no probe
  ran. The generation fix would cover them.

NOT CLEAN: a sealing sweep still running at delete time erases the 'deleted' mark (sweep_state -> 'done'), and any owner write that lands after the claim commits (stuck chat turn, chart run, or an insert queued on confirm's row lock) is listed for the new owner
