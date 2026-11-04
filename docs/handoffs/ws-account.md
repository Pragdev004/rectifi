# WS-ACCOUNT handoff (branch ws/account)

Built: `rectifi/account.py` (export, delete, privacy facts, the few people-table queries the sweep needs),
`rectifi/seal_sweep.py` (owner sweep + sign-in rs1 drain), `rectifi/api/routes/account.py`, the WS-ACCOUNT blocks in
`api/app.py` and `hooks.py`, `docs/PRIVACY.md`, tests `test_account.py`, `test_account_claim.py`, `test_seal_sweep.py`.

## Requests for the integrator (files I do not own)

1. **tests/test_rls.py::test_connect_system_is_only_called_from_the_allow_listed_modules** — add `"rectifi/account.py"` to the
   `allowed` set (line ~251). `account.py` calls `db.connect_system` for: sweep_state read/write, `encrypted = true` at the end of the sweep,
   deleting a friend's `users` row, resetting row 1, counting friends. This test is RED in this branch until that one line lands. Alternative: move
   those five queries into `users.py` as helpers and have `account.py` call them (then no allow-list change).
2. **`users.verify_password(uid, password)`** (public): `account.delete_account` calls the private `users._verify_current`. Fine to keep; expose it
   when convenient.
3. **WS-WEB**: `GET /api/account/export` answers a ZIP (`application/zip`, `Content-Disposition: attachment`, `no-store`); fetch it as a blob with
   the `X-Rectifi-Request` header. `DELETE /api/account {password, confirm_username}` -> `{ok:true}` and clears the cookie; errors:
   401 `invalid_password` (field `password` is not set; it is the same code the change-password form uses), 422 `invalid` with field
   `confirm_username`, 409 `owner_has_friends`, 429 `login_locked`, 400 `not_multiuser`. `GET /api/privacy` JSON keys: `mode`, `encryption_at_rest`,
   `model_provider` (local|cloud), `model`, `ai_for_friends`, `backup_days`, `sealed_fields` ("table.column"), `keyed_hash_fields`, `plain_fields`,
   `unsealed_owner_tables`, `merchant_allowlist` [{key,name}], `who_can_read` [{who,plain,sealed}], `limits`, `recovery`. Render these; the page text
   must carry them all (the honesty test).
4. **WS-DEPLOY**: after the sweep finishes, `VACUUM FULL` and delete old backups (plain copies survive there). `account.BACKUP_DAYS = 7` mirrors
   `tail -n +8` in `deploy/compose.oracle.yml`; `test_account.py` fails if the compose file changes.
5. **WS-AUTH / AI**: the hook block runs `seal_sweep.drain_rs1` for every person and starts the sweep for the owner. It does not touch the
   owner's AI tables or chat history (checkpoints/store), which stay readable (D8).
6. **WS-ALERTS** (message from the coordinator): the sweep covers `alerts.title/detail`, custom-merchant `alert_rules.target` (+ `target_blind =
   vault.blind(target)`; brand targets stay plain), `push_subscriptions.label`, `merchant_patterns.pattern/merchant` + `pattern_blind`, and rewrites
   `alerts.dedupe_key` / `scope_key` of the forms `cap:merchant:<name>:...`, `streak:<name>:...`, `merchant:<name>:<period>` so a non-allow-listed
   name becomes `vault.blind_ingest(name)` (tests: `test_seal_sweep.py`). If WS-ALERTS adds another key form, extend `seal_sweep._hide_key`.
   `app_settings` (alert prefs `muted_scopes` / `snoozed_scopes` hold names like `streak:Chaayos`) is NOT in `sealed_fields.SEALED`, so the sweep
   leaves it; if it should be sealed, add it to the registry (and a rule here).

## Decisions

- **Export is type-based for formula defusing.** Only text cells starting with `= + - @`, tab or CR get a leading `'`. A Decimal such as `-500.00`
  is a number, not text, and stays exact (money must equal `tools`). No BOM (Excel users: Data > From Text/CSV, UTF-8).
- **Export contents.** All tenant tables except `sms_seen`, `classify_jobs`, `trace_spans`, `rag_chunks` (reasons in `EXCLUDED_TABLES`), minus
  `user_id`, keyed hashes, `token_hash`, push endpoint/keys, chart images. `transactions.csv` uses the readable UPI reference. A test fails when a new
  tenant table is in neither list. The owner's chat messages (checkpoints) are not exported.
- **Owner delete.** With no friends, the owner's rows are wiped (tenant tables, plus the chat store rows of his threads), sessions are deleted, row 1 is
  reset to the unclaimed state (username `owner`, no credentials, `encrypted=false`, `sweep_state` NULL, epoch bumped) and the defaults re-seeded, so a
  new `invite --owner` works. Row 1 is never DELETEd. The LangGraph `store` (long-term memory) is not cleared.
- **Delete order of checks.** `confirm_username` (case-insensitive) is checked before the password so a typo does not count against the account;
  a wrong password does (via `users._verify_current`).
- **Sweep state** in `users.sweep_state`: `pending` (set by confirm), JSON `{"stage","done","total"}` while running, `done` at the end (with
  `encrypted = true`). The sweep never sets `encrypted` unless a full re-scan finds no plain value. A NULL state on an unencrypted owner counts as pending.
- **Rows the sweep writes** only change columns that need it, in batches of 200, one transaction per batch (`FOR UPDATE SKIP LOCKED`). One sweep per
  person at a time (in-process lock; run one worker). The hook thread logs failures by exception type only.
- **rs1 drain.** WS-CORE's hook already drains `transactions` (unlocked=false) and `sms_inbox`; `drain_rs1` handles every other sealed table (for
  example signed-out alert text) and skips those two. Both run at every multi-user sign-in; both are idempotent.
- **Privacy `model_provider`:** `ollama` / `openai_compat` is `local` only when the base URL is localhost / 127.0.0.1, else `cloud`; other
  providers are `cloud`.
- **`/api/privacy` touches no database** (a plain handler), so it answers when the database is down.

## Not verified

- Sweep speed and memory on a real years-long ledger; only a few dozen rows are tested. Batch size 200 is a guess.
- `ctid`-free paging relies on `id`; `weekly_reviews` and `sms_seen` (no id) rely on every matched row changing.
- Cascade delete under RLS is proven by the tests here on Postgres 16 (Windows), not on the Oracle VM.
- Export of very large ledgers is built in memory (a ZIP of a few MB).
- The real owner claim on Prag's real database has never run.
