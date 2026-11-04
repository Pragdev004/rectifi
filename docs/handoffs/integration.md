# Integration notes (stage 4, branch `integration`)

Merge order after the foundation: auth, core, ai, alerts, account, deploy, web, proof (all clean, no conflicts).
Test database: `rectifi_int_test`. Docker, a real phone and the real database were never touched.

## Bugs found by PROOF

1. **The owner sweep stopped with `UniqueViolation` (`uq_alerts_dedupe`).** `rectifi/seal_sweep.py` (the `alerts` branch in `run`).
   At the owner claim two things start together: the sweep and `alerts.after_sign_in` (catch-up). Catch-up writes a custom
   merchant's alert under the hidden key (`blind_ingest`). The sweep then renamed the old plain-keyed copy to the same hidden key,
   and the unique index refused. The sweep died, `users.encrypted` stayed false, and plain text stayed in `alerts`, `alert_rules`
   and `push_subscriptions` until the next sign-in. Fix: if the hidden key is already taken, the old copy is deleted (it is only
   history that repeats the new alert). Test: `tests/test_seal_sweep.py::test_an_old_alert_whose_hidden_key_already_exists_is_dropped_not_a_crash`.
   Found by `test_the_owners_old_plain_data_is_gone_from_a_dump_after_the_sweep` and the owner-claim browser test.

No other PROOF assertion needed a code change. Every other red was a wrong assumption in a test helper or in a test body, fixed
without weakening what is asserted:

| Red | Cause | Change |
|---|---|---|
| 429 on sign-up everywhere | A1: the helper looked for `reset_limiter(s)` under another name. `users.reset_limiters` exists. | `proof.reset_limiters` also tries `reset_limiters`. |
| `apply-similar` gave a friend 404, not 403 | A10 listed it as owner-only. It is deterministic (no model) and `FRIEND_ROUTES` allows it on purpose. | Moved from the owner-only list to a `by_id` case: B uses A's id, must be refused and A's tables must not change (stricter than before). |
| "B's use of A's upload id imported something": `5 == 1` | The test hard-coded that B starts with one row. B has five. | Compare with B's count before the calls. |
| Revoked token still answered 200 | The test revoked `devices[0]`, which was the world's other device, not "Bob's phone". | Pick the device by its name. |
| Tick pushed nothing / 410 not seen | "baseline tick" does not baseline fresh rows (the first sweep treats them as news, bulk, and uses up the cap steps). | `proof.baseline_alerts(uids)` sets each person's watermark to their newest transaction. |
| `acct` canary found in the dump | Account names are plain by plan (D4, section 1). | `Canaries.sealed_words()` leaves out `acct`/`acct2` for the dump scans; the laptop sanity run still finds them. |
| "rs1 never opened" | The one-minute ticker has no data key, so an alert it writes after the last sign-in is `rs1.` until the next sign-in (by design). | The friend signs in once more before waiting. |
| Sweep "8 plain cells left" | `plain_sealed_cells` counted brand targets of `alert_rules` (plain by D3/D4). | Count only custom-merchant targets. |
| B's `DELETE /api/transactions/{A's id}` gave 200 | Delete is idempotent on master (`{"deleted": false}`, tested in `test_api_transactions.py`). | Accept 403/404, or 200 with exactly `{"deleted": false}`; A's checksum is still compared. |
| Owner-claim test: `VaultLocked` in `seed_month` | The fixture turns multi-user on before the "old laptop data" is written. | Seed with the variable off, then turn it on. |

## Integrator requests

Applied: ws-auth 1 (test already fixed), 2 (WS-AI mapped uid 1 to `"owner"`), 3 and ws-web 2 (lifespan: `_start_background_work` returns
at once in multi-user mode; `ensure_watermark` was already skipped; `tests/test_integration_flow.py` checks no ERROR is logged),
ws-account 1 (allow-list line was already there), ws-deploy gateway log line (`sms_gateway.py`: on a public bind the line says
"Funnel", not "anyone on your network"; test added), ws-web 1 (JS budget 175 KB), CLAUDE.md (rules 18 to 20, addenda, map, commands),
foundation 2 and 3 (HOSTING.md uses `-U postgres`; `deploy-check` requires `RECTIFI_DB_PASSWORD`; both already done by WS-DEPLOY),
ws-ai 1 (clean-up), the `app_settings` alert prefs (below).

- **Alert prefs gap (ws-account 6).** New muted and snoozed scopes already carry the hidden key for a custom merchant (WS-ALERTS).
  The old ones (written before the claim) were plain, so the sweep now rewrites `muted_scopes` / `snoozed_scopes` in `alerts.prefs`
  with the same `_hide_key` (`seal_sweep._hide_prefs`, idempotent, test added). Not added to `sealed_fields`: the value is a JSON
  list of scope keys, not a sealed column, and the Privacy page text is unchanged.

Not applied, with reasons:
- ws-account 2 (`users.verify_password` public): `delete_account` works with the private helper; nothing needs it.
- ws-account 4 (`VACUUM FULL`, delete old backups): it is an operator step, and HOSTING.md step 6 already says it.
- ws-ai 4 (sweep deletes `rag_documents` of corpus `note`): the owner's claim runs a reindex through the sign-in hook that removes
  them, and `test_seal_sweep.py` says the notes stay after the sweep. Left as is.
- ws-ai 2 (`agent/memory.py` namespace for friends): AI is owner-only.
- `dev.py` inside the container: `project_python()` falls back to `sys.executable` and `scripts/` is copied by the Dockerfile, so it
  should work. Unverified (no Docker here).
- PROGRESS.md: stage 7.

## What was run end to end

`tests/test_integration_flow.py` (new, runs in the suite): the real `dev.py invite` -> `POST /api/auth/signup` -> `/signup/confirm`
-> account -> statement upload -> ledger (amount, search) -> table holds `rx1.` -> the second person sees nothing and gets 403/404
on the batch -> sign out (401) -> SMS through the gateway with the device token (200 `added`, stored `rs1.`, `unlocked = false`) ->
sign in -> the row becomes `rx1.`, `unlocked = true`, the ledger finds it. A second test starts the server in multi-user mode and
asserts nothing is logged at ERROR. On top of that the three PROOF files drive about 100 routes as two people and a dump.

## Suite results and screenshots

- Non-e2e half: 2034 passed, 50 skipped (about 21 min). Browser half (`tests/test_web_e2e*.py` + `test_multiuser_e2e.py`): 121 passed.
- `npm test` (vitest): 196 passed, 20 files. `web/dist` rebuilt with Node 24 after `npm ci` (new file hashes, JS stays inside 175 KB).
- Screenshots: Signup, Recover, Settings, Privacy and a friend's Overview at 390 and 1280, light and dark (20 files), saved outside the
  repo in the session scratchpad `shots/` folder. No console errors and no sideways scroll on any of them.
- Visual notes, no defects found: the Privacy and Signup pages scroll inside `main` (document height equals the viewport), so a
  plain full-page screenshot shows only the first screen; the page itself scrolls (4,555 px inside `main` at 390). The friend's
  Overview shows "Nothing recorded for this month yet" because the seed data is dated October and today is November: data, not a bug.
  The friend's sidebar and bottom bar carry no Ask, Charts, Library, Memory or Traces.

## Still open

- The ticker has no data key, so alerts written between two sign-ins have `rs1.` titles until the next sign-in (by design, WS-ALERTS).
- Nothing was run on Docker, Oracle, Funnel or a real phone. `VACUUM FULL` after the sweep is a manual step.
- The owner claim has never run on the real database (decision D10).
