# Stage-5 review, round 1 (branch `integration`)

Reviewer: Opus, read-only on the code. Scope: `git diff master..integration` (minus `web/dist`) plus the stage-2 foundation
(vault, userctx, RLS in schema.sql, db.connect/connect_system, migration guard, hooks). Probes live outside the repo
(session scratchpad `review2/probes/`, a `conftest.py` that execs the repo's `tests/conftest.py`), run on `rectifi_int_test`.
Line numbers are from `integration` at e08663c.

Totals: 1 HIGH, 5 MED, 7 LOW.

## HIGH

- [HIGH] Owner claim: dedupe stops working until the sweep ends, and then the sweep can never end — `rectifi/seal_sweep.py:291`
  (the UPDATE that re-keys `transactions.upi_ref` / `fingerprint`, also `sms_seen.text_hash`, `merchant_rules.pattern_blind`,
  `merchant_patterns.pattern_blind`), with `rectifi/tools.py:277-278`, `rectifi/importer.py:486-487` and `tools.py:1035`.
  What is wrong: from the owner's confirm until `seal_sweep.run` reaches a row, old rows hold plain dedupe keys (`123456789012`,
  the sha256 fingerprint) while every new write uses `b1:` keys (`blind_ingest` / `blind`). The two never compare equal, so
  `ON CONFLICT` and the importer's look-ups miss: a re-imported statement or a forwarded SMS for an old payment is added again
  (rule 3 broken, money totals double). When the sweep then re-keys the old row to the same `b1:` value, the unique index
  `(user_id, upi_ref, direction)` / `(user_id, fingerprint)` refuses; the batch rolls back, `run_in_background` logs
  "sweep stopped (UniqueViolation)", and every later sign-in hits the same row again. `users.encrypted` stays false for good,
  the owner's old text stays plain, and the DB trigger never switches on. The window is wide in practice: the sweep runs only in
  a sign-in thread (a restart pauses it until the next sign-in) while the gateway keeps writing; it also opens if a previous
  sweep died for any reason. The integrator fixed the same class for `alerts.dedupe_key` only (`seal_sweep.py:285-290`).
  Reproduced: `test_p1_sweep_dup.py`. Plain laptop import, then `RECTIFI_MULTIUSER=1` with owner keys: the same CSV imported again
  gives `added 5` (5 -> 10 rows), then `seal_sweep.run` raises `UniqueViolation`, state `(None, False)`. Same with two
  `tools.add_transaction(..., upi_ref="123456789012")` (one before, one after): 2 rows, sweep raises `UniqueViolation`.
  Fix: (a) re-key the dedupe columns (`upi_ref`, `fingerprint`, `sms_seen` / `sms_inbox.text_hash`, `pattern_blind`,
  `target_blind`) synchronously inside `confirm_signup`'s claim, before any cookie or gateway write can use the new keys (it
  is cheap: HMACs only, no text), and leave the slow text sealing to the background sweep; and (b) in the sweep, treat a
  unique collision on a dedupe column as a duplicate: keep the older row (it carries the user's category and note), move
  nothing that would double money, delete the newer copy (or mark it for review), and test that the sweep still finishes.

## MED

- [MED] An abandoned owner claim plus a friend called "owner" breaks every sign-in, sign-up and confirm — `rectifi/users.py:353`.
  `purge_unconfirmed` resets row 1 to `username = 'owner'`. After the owner's step 1 renamed row 1, "owner" is free, so a friend
  can take it (any case: `uq_username` is on `lower(username)`). When the owner's pending session expires, the reset hits the
  unique index; `purge_unconfirmed` runs first in `signup`, `confirm_signup` and `login`, so all three raise
  `UniqueViolation` (a 500) for everybody until someone edits the database. Reproduced: `test_p2_purge.py` (owner step 1, friend
  "Owner" signs up and confirms, clock +31 min, `users.login("Owner", ...)` raises `UniqueViolation`). Fix: never write the
  placeholder name back (keep the chosen name, or `NULL`, or `owner-<random>`), refuse "owner" in `username_problem`, and catch
  errors per row in the purge so one bad row cannot block sign-in.

- [MED] Any stranger can lock any account, including recovery and Settings — `rectifi/users.py:57-59, 541, 624, 689`.
  10 wrong passwords (or wrong recovery codes) in 15 minutes lock the account for 15 minutes, counted per account with no
  regard for who sends them. Friends' usernames are easy to guess, and the app is on the internet behind Funnel (D6). A script
  with 10 requests per 15 minutes keeps a person locked out of sign-in, `recover`, change-password, new recovery code and
  account delete forever. Not reproduced (by reading). Fix: keep the pause but drop the hard lock for a browser that proves a
  past good sign-in (a signed "known device" cookie set at login, OWASP device-cookie pattern), or lock only new devices.

- [MED] A stranger can stall the whole web app through the login door — `rectifi/users.py:530`, `rectifi/vault.py:44,235`,
  `rectifi/api/routes/auth.py:157`. In multi-user mode there is no global login limiter (the old `LoginLimiter` is single-password
  only). Each login with a new unknown name costs one scrypt (measured 0.12 s here) behind a 2-slot semaphore, while holding one
  of the 40 Starlette threadpool tokens. About 40 concurrent junk logins fill the threadpool, and every other request waits:
  `SessionLock` itself calls `run_in_threadpool(resolve_user)`. Suspected (by reading plus the measured scrypt cost; no load test).
  Fix: a global cap on concurrent logins answered 429 at once (a non-blocking `acquire` on the scrypt semaphore, or an
  `anyio.CapacityLimiter` of 2 to 4 just for auth), plus a small global failure budget per minute.

- [MED] Anyone on the internet can pause every friend's SMS capture — `rectifi/sms_gateway.py:183-194`. `failures` is keyed by
  `request.client.host`. Behind Funnel / the Docker port every request has the same client address, so 30 wrong tokens a minute
  from a stranger make every phone get 429. `/api/phone/test` (friend-allowed, `routes/phone.py:93`) posts from 127.0.0.1 and
  feeds the same counter. Reproduced: `test_p5_gateway.py` (30 bad tokens, then Alice's real token gets 429). Fix: check the token
  first and rate-limit failures by token prefix / globally with a much higher bar, never blocking a token that is valid; or key
  failures on `Tailscale-Funnel-Request`-style forwarded data only when it is trustworthy.

- [MED] PRIVACY.md and the Privacy page claim more than the ingest hashes give — `docs/PRIVACY.md` ("Keyed hashes", "they
  cannot list the references"), `rectifi/account.py:247-249`, `rectifi/vault.py:204-213`. `blind_ingest` is HMAC under a key
  derived from `users.ingest_salt`, which sits in plain BYTEA in the same database. Whoever has a dump or browses the database
  holds the key, so: 12-digit UPI references in `transactions.upi_ref` can be enumerated offline (10^12 HMACs, far fewer with the
  date-shaped prefix), and a custom merchant's name in `alerts.dedupe_key` / `scope_key` and in `app_settings` `alerts.prefs`
  (`streak:b1:...`) falls to a dictionary guess, without the password. The "dump: sealed No, unless they crack your password"
  row is not true for those. Those alert/prefs columns are also not in `sealed_fields` (rule 19: the registry lists every `b1:`).
  Not a code bug; by reading. Fix: say it plainly on both pages (ingest hashes are keyed with a server-held salt: they stop
  casual reading, not a dump holder), add the alert/prefs keys to the registry, or wrap the ingest salt (for example seal it to a
  server key kept outside the database) if the stronger claim is wanted.

## LOW

- [LOW] Owner-claim window loses SMS text if the claim is abandoned — `rectifi/users.py:419-424, 353-356`. Step 1 already writes
  `public_key` / `ingest_salt` on row 1, so the owner's laptop-era phone writes `rs1.` rows and `b1:` keys under them; if nobody
  confirms, the purge sets them to NULL and those texts can never be opened (amounts survive). Fix: give row 1 its keys at confirm.
- [LOW] Before the owner claims (after migration, multi-user on) the owner's phone gets 500, not 503 — `rectifi/tools.py:1035`
  raises `VaultLocked` (no ingest salt), caught at `sms_gateway.py:208`. Rule 8 reserves retry codes; answer 503 with Retry-After.
- [LOW] Session cookie lacks `Secure` in multi-user mode unless `RECTIFI_PUBLIC=1` or a non-loopback bind — `rectifi/api/auth.py:226`.
  Docker sets it; a laptop + Funnel multi-user setup would not. Fix: `public_mode() or multiuser()`.
- [LOW] Global sign-up limiter (10 tries per hour, failed ones too, `users.py:392`) lets anyone without an invite block every
  sign-up, the owner claim included, until a restart. Fix: count only tries whose invite code exists.
- [LOW] `/api/recover` reveals whether a username exists by timing — `rectifi/users.py:674-691`: a real name pays a DB write
  (`_register_failure`), an unknown one does not, and there is no scrypt to hide it. Measured median 1.02 ms vs 0.31 ms
  (`test_p3_timing.py`). Fix: do the same `connect_system` round trip for ghosts.
- [LOW] Keys outlive sign-out in memory more than needed — `rectifi/db.py:179` leaves the last person's opening row factory
  (with their `UserContext`, data key and private key) on a pooled connection until it is reused; `categorize._index_cache`
  keeps per-person payee labels with no eviction; `UserContext` and `users.SignedIn` are dataclasses whose `repr` prints `key`,
  `private_key` and `k`. Fix: reset `row_factory` in a `finally`, bound the cache, `field(repr=False)` on secret fields.
- [LOW] Friends can read server internals — `rectifi/api/routes/phone.py:42-52` (`/api/phone/status`, friend-allowed) returns the
  owner's Tailscale node name, tailnet URL and the server's private LAN addresses, and runs `tailscale status` per call.
  Fix: in multi-user mode return only the public gateway URL to friends.
- [LOW] Owner account delete leaves chat memory — `rectifi/account.py:190-208`: the LangGraph `store` (namespace "owner") and
  checkpoints of threads not listed in `conversations` stay; a later owner claim inherits them, and PRIVACY.md says delete
  removes "every row you own in every table". Fix: clear the owner namespace and all checkpoints on owner delete, or say so.

## Checked and fine

- Auth: invite codes (100 bits, sha256 stored, printed once, single use via reserve then `used_at`; concurrent sign-ups and
  confirms serialize correctly, failures inside the `connect_system` block roll back). Two-step sign-up: no session or
  sign-in before confirm; token `sid.k` opens only the pending row; confirm deletes it and issues a fresh sid/k. Login: one
  scrypt for unknown, unconfirmed and wrong-password cases, `hmac.compare_digest` on the verifier hash and the gateway token
  hash. No reset path: only `change_password` (needs password), `recover` (needs code), owner delete (needs password), purge
  (unconfirmed rows only); `dev.py` has none. Cookie: HMAC-SHA256 signed, HttpOnly, SameSite=Strict, `exp`/`iat` checked,
  `uid` + `sid` + epoch checked against `user_sessions`, single-password and multi-user cookies refuse each other.
  Change-password and recover bump the epoch and drop sessions; logout deletes the row.
- `FRIEND_ROUTES`: enumerated every `/api` route; no literal owner-only route matches a friend `{id}` pattern, path tricks
  (`//`, `..`, trailing `/`) only reach 403/404, AI routes (Ask, Charts, Chat, Library, Memory, Traces, Models, classify, both
  Explain) are owner-only. Non-`/api` routes serve static files only.
- RLS: ENABLE + FORCE + `tenant_isolation` on all 22 tenant tables; `users` own-row read, `invites` / `user_sessions` system only;
  GUCs are `set_config(..., true)` (transaction-local), no explicit `commit()` in `rectifi/`, so a pooled connection never carries
  a person into the next transaction. All `connect_system` callers (users, account, alerts.people, authenticate_device,
  init_db, doctor, migrate status) touch only people tables or device lookup.
- Context on every path: `SessionLock` binds for the whole request (streaming and background tasks included); jobs bind per job
  via `_owners`; ticker binds each person with no data key; gateway binds the device's person for ingest and sweep; seal sweep
  and hooks bind; `userctx.spawn` copies context. Laptop and single-password modes unchanged.
- Crypto: AES-256-GCM random 96-bit nonces, AAD binds uid (cross-person swap fails); rs1 = fresh X25519 ephemeral + HKDF +
  AES-GCM; wraps carry purpose labels; scrypt N=2^16 r=8 p=1 split into verifier/KEK; recovery code 125 bits + HKDF;
  `blind` key separated by HKDF. Sealed values never used in SQL search (Python search over 20,000 rows).
- Alerts: push text for custom merchants is generic, `_push_all` filters by uid, two-key advisory locks, catch-up waits on the
  person's sweep lock. Export: only the person's rows, Decimals exact, formula cells defused, hashes/tokens/endpoints dropped.
- Rules 1, 2, 7, 13 (no money maths in the browser, routes are wrappers), 14, 16, 17 untouched by this diff. MCP and Streamlit
  refuse multi-user. No SQL built from user input (dynamic SQL uses registry names only). No XSS sinks
  (`dangerouslySetInnerHTML` unused). Secrets: logs carry exception types and uids only; codes/tokens never in URL or storage
  (`useSecretCall`, no TanStack mutation). New pages use tokens only, 44 px targets, labelled inputs, `role="alert"` errors.

## Not verified

- Load: the threadpool stall (MED above) was not driven; scrypt cost measured once on this laptop.
- Funnel / Docker client addresses: inferred from how Funnel and Docker port publishing proxy (one source address), not observed.
- The browser half of the suite and `npm test` were not rerun; Privacy page text checked by reading `Privacy.tsx` only.
- Real owner claim on a years-long ledger (sweep duration, hence the size of the HIGH's window).
