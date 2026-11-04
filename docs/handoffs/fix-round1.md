# Stage-6 fix, round 1 (branch `ws/fix`)

Fixer: Sonnet. Input: `docs/handoffs/review-round1.md` (1 HIGH, 5 MED). The 8 LOW findings were not touched (backlog).
Every fix has a regression test. Line numbers are on `ws/fix`.

## 1. HIGH: old plain dedupe keys plus new `b1:` keys

Cause: after the owner claim, new writes used `b1:` keys while old rows kept plain ones, so dedupe missed and the sweep later
hit a unique index and died.

- `rectifi/seal_sweep.py` `rekey_dedupe` (297): re-keys every dedupe column (`upi_ref`, `fingerprint`, `sms_seen` / `sms_inbox`
  hash, `pattern_blind`, `target_blind`, custom-merchant alert keys, alert prefs) with HMACs only. It also keeps the plain value
  sealed (`upi_ref_sealed`, new `sms_seen.digest_sealed`).
- `rectifi/users.py` `_rekey_owner_dedupe` (550), called inside `confirm_signup` BEFORE `confirmed_at` is set. `tools.authenticate_device`
  (`tools.py` ~1024) gives the phone no keys until `confirmed_at` is set, so no keyed write can land before the re-key. The
  window between the owner's step 1 and the confirm is closed.
- `seal_sweep._apply` (223) / `_write_row` (270): a dedupe clash never raises. Money and SMS rows: the older payment is kept, the newer
  duplicate is dropped (its category is handed over if the person chose one there), counted in `run()["merged"]` and logged.
  Rules and caps (`KEEP_NEWER`): the one set later wins. A savepoint plus one retry covers a race.
- Schema: `sms_seen.digest_sealed` (registry entry in `sealed_fields.py`), `users.blind_fp`.
- Tests: `tests/test_fix_dedupe_claim.py` (real claim via invite, signup, confirm; re-import, resent SMS with and without a UPI
  reference; sweep finishes; `encrypted` true; clash cases with the re-key disabled; phone gets no keys before confirm).
  `test_without_the_claim_step_the_old_dedupe_misses` shows the original double insert.
- Known gap: an SMS with no UPI reference carries no dedupe key except `sms_seen`. If a clash ever happens (re-key skipped),
  the duplicate transaction behind it cannot be recognised. The claim ordering makes that path unreachable in normal use.

## 2. MED: abandoned owner claim plus a friend called "owner"

- `users.py` `RESERVED_NAMES` (392): a friend cannot sign up as "owner" (any case; answers "taken").
- `_reset_owner_claim` (395): row 1 goes back to "owner", or to `owner-<random>` if the name is held (old databases).
  `purge_unconfirmed` (409) wraps each stale row in a savepoint and logs by type, so one bad row cannot block sign-in.
- Tests: `tests/test_fix_purge.py` (the review's probe, the reserved name, a legacy clash, one bad row).
- `tests/test_users.py` writer-list assertions renamed for `_reset_owner_claim`.

## 3. MED: a stranger can lock any account

Design (limiter notes at the top of `users.py`): no account lock any more.
- Misses count per (account, client) in memory (`_Throttle`, 227). Client = a signed known-device id (`rectifi_device` cookie,
  set at sign-in, `api/auth.py` `make_device_cookie`) if it belongs to that account, else the network address.
  5 misses in a minute: 2 s pause. 10 in 15 minutes: that one pair is blocked for 15 minutes.
- Per account across clients: a response delay that doubles after 10 misses, capped at 30 s. It delays, never refuses, and a
  known device skips it. Brute force stays hard: 12-character minimum, scrypt, per-pair blocks, the delay, the hashing cap.
- Recovery has its own bucket (`recover_throttle`). Password misses never block it and the reverse. The recovery code has 125 bits,
  so that bucket only caps work.
- Signed-in password checks (`_verify_current`: change password, new recovery code, account delete) still use the per-account DB
  counter, but sign-in no longer feeds it, so a stranger cannot reach it.
- Routes: `api/routes/auth.py` passes the address and device to `users.login` / `users.recover`; login handles an `AuthError`.
- Residual: a NEW device behind a shared proxy address, while a stranger is actively failing from that same address, can wait
  up to 15 minutes. Known devices and other addresses are not affected. If Funnel gives a trustworthy client address
  (X-Forwarded-For), pass it as `client`; I did not verify what Funnel sends.
- Tests: `tests/test_fix_lockout.py`; `tests/test_auth_multiuser.py` recovery test rewritten (recovery miss blocks recovery only).

## 4. MED: login flood

- `users._derive` (339): at most `AUTH_INFLIGHT` (4) password hashes running or waiting; the rest get 429 `login_busy` at once
  (`Retry-After: 2`), so a flood holds at most 4 threadpool threads. A global miss budget (120 per minute): beyond it only
  known devices may try (429 for new clients). All scrypt call sites use `_derive`.
- Test: `test_a_sign_in_flood_gets_429_at_once_...` (30 parallel logins with a slow hash: peak in flight is at most 4, an
  ordinary request answers in under 1 s, the rest are 429) and the global budget test.

## 5. MED: gateway limiter

- `sms_gateway.py` (~186): the token is checked first. Only the answer to a WRONG token is throttled (401, then 429 after 30 a
  minute per address). A good token is never refused for other clients' bad ones.
- Cost: a flood of bad tokens now does one device lookup each (before, the limiter short-circuited). Cheap; noted.
- Test: `test_a_flood_of_wrong_tokens_never_refuses_a_phone_with_a_good_token` in `tests/test_sms_gateway.py`. Rule 8 codes unchanged.

## 6. MED: blind keys and the privacy claims

- `RECTIFI_BLIND_KEY` (`config.blind_key`, 32+ chars). `vault.blind_ingest` (223) = HMAC under HKDF(server key, salt = person's
  ingest salt). Missing key raises `BlindKeyMissing`. The app (`api/auth.startup_problem`) and the gateway lifespan refuse to
  start in multi-user mode without it. Laptop mode is unchanged.
- `users.blind_fp` stores a 64-bit fingerprint of the key (an HMAC of a label; never the key). Set at friend sign-up and at the
  owner confirm. `hooks.account_after_sign_in` starts `seal_sweep.reblind` (480) in a thread when it differs (or is NULL). It
  rebuilds `upi_ref` from `upi_ref_sealed`, `sms_inbox.text_hash` from the opened SMS, `sms_seen.text_hash` from `digest_sealed`,
  with the same clash handling as item 1. Nothing is lost. A resend that arrived during the change is merged afterwards.
- DEVIATION, please check: alert keys and prefs scopes (a custom merchant's name inside `alerts.dedupe_key` / `scope_key` /
  muted and snoozed scopes) use `vault.blind` (a key from the DATA key), not `blind_ingest` (`alerts._name_part`,
  `seal_sweep._hide_key`). That is stronger (no dump, backup or server key holder can guess the name) and needs no re-blinding
  when the server key changes, because the data key survives. It is safe because a custom merchant's name is sealed, so the
  signed-out sweep never sees it (its cap already fires at the next sign-in). These columns are listed under a new `hidden`
  field and `HIDDEN_SETTINGS` in `sealed_fields.py` and shown on the Privacy page and PRIVACY.md.
- Docs and claims: `docs/PRIVACY.md` (two kinds of keyed hash, who can guess what; owner limit stated), `account.py`
  `WHO_CAN_READ` and `LIMITS`, `web/src/pages/Privacy.tsx` (+ type, test), `docs/HOSTING.md` (back up the key apart from DB
  backups, and why), `.env.example`, `.env.deploy.example`.
- `dev.py set-password` adds a key only if none exists (never replaces: a new one means a remake at sign-in). `deploy-check`
  FAILS without it in multi-user mode; `dev.py doctor` FAILS without it in multi-user mode.
- `CLAUDE.md` rules 8, 11, 19, map rows and commands updated (still under 200 lines). `web/dist` rebuilt; `npm test` 196 pass.
- Tests: `tests/test_fix_blind_key.py` (key in the hash, no key stored, rotation then sign-in keeps dedupe for UPI reference,
  no-reference SMS and inbox, merge of a window resend, hook starts the remake, refuse to start, gateway refuses, laptop needs none).
  `tests/conftest.py` sets a default test key.

## Not done

- The 8 LOW findings.
- Residual windows: an SMS with no UPI reference resent while the blind key is changed and before that person signs in is added
  once (nothing to merge it with). An owner database from before the fix is not retro-fitted for `digest_sealed`; the claim
  re-key fills it.
- Funnel client address (see item 3) and a load test against real concurrency were not verified.

## Environment note

Linking `web/node_modules` to the main checkout made `npm ci` (inside `dev.py build-web`) empty
`rectifi-new/web/node_modules`. I restored it by copying the worktree's install. Nothing tracked changed there.

## Added after the fixer stopped (main session)

- `rectifi/userctx.py`: `key`, `ingest_salt` and `private_key` are `field(repr=False)`. Before, any log line, traceback or
  assertion that printed a `UserContext` showed the raw keys (seen in a pytest failure). Test:
  `tests/test_userctx.py::test_the_keys_never_show_in_repr_or_str` (failed before, passes now). Not found by either review.
- Two older tests updated for the blind key, nothing weakened: `test_deploy_files.py` (the multi-user env file carries a key;
  a new test proves a missing or short key fails and is never echoed) and `tests/support/tenants.make_user` (stores the key
  fingerprint, as a real sign-up does, so the hook test no longer sees a pre-key row).

## Verification (real results)

- Full non-e2e suite before those two test updates: 2068 passed, 2 failed (the two above), 44 skipped (same reasons as
  integration: POSIX sandbox 31, dockerfile_parse 6, brotli 2, hadolint, pwsh, java, file modes, knowledge-graph 1 each).
- After the updates: the two files plus every file using `make_user` (11): 237 passed, 8 skipped (deploy tool skips only).
  `test_userctx.py` + `test_vault.py`: 21 passed. `npm test`: 196 passed.
- NOT yet run on `ws/fix`: `test_rls`, `test_isolation_routes`, `test_dump_no_plaintext`, `test_multiuser_e2e`, `test_web_e2e*`.
  The run was stopped by Claude Code because the machine was low on memory (about 23 tests in, no failure seen).
