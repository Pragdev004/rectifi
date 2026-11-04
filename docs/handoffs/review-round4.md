# Stage-6 final re-review of fix round 3 (branch `ws/fix`, head fc31364)

Reviewer: Opus, read-only on the code. Scope: N7, N4, N8 from `review-round3.md`, the `fix-round3.md` claims,
`git diff de86b5f..fc31364`, new issues, then a class for every open item (owner's merge rule). Probes are outside the repo
(scratchpad `rereview3/test_rr4.py`): 4 passed on `rectifi_fix_test`. Rerun here, one file at a time: `test_fix_hash_lanes` 7,
`test_fix_account_backoff` 6, `test_fix_proxy_headers` 10, all passed. Full suite, browser tests and vitest were not rerun
(reported green on fc31364).

## Verdicts

**N7 hash-cap lanes: RESOLVED.**
- Three `BoundedSemaphore` lanes (`users.py:366-385`): device 2, recovery 1, other 4, each `acquire(blocking=False)`, 429 when full.
- Device lane: `login` picks it only when `_client_pair` returns `known`. That needs `device[0] == row["id"]`, and `device`
  comes from `read_device_cookie` (HMAC with a `device.` prefix, `compare_digest`) in the async route, before any hashing.
  A forged cookie, another account's cookie or an unknown name all go to "other". Probe P3: bob's valid cookie on alice in
  backoff gets `login_wait`. Probe P4: alice's own cookie signs in during backoff with the "other" lane held full.
- Recovery lane: `_derive(..., "recovery")` runs only after `unwrap_key` proved the code. Checking the code is HKDF plus
  AES-GCM (`vault.recovery_key`), with no scrypt, so a stranger can't reach that lane.
- Starvation: every scrypt call site goes through `_derive` (grep). No DB connection is held while hashing. That is at most
  7 hashing threads against anyio's default limiter of 40 (checked in the installed anyio).

**N4 account backoff before hashing: PARTIAL.**
- What is right:
  - `reserve` runs before any hash and under the throttle lock, and books the try as a provisional miss. So parallel tries
    see each other (fixer test: 40 parallel tries give 1 hash).
  - `release` in a `finally` handles a right password and a busy refusal. A refusal is not a miss.
  - Known devices skip it, and recovery never calls it. Unknown names are treated the same way.
- What is not: the bound can be reset for free (N11 below). So "one hash per 2-30 s per account" does not hold against an
  attacker who sends junk names.

**N8 uvicorn proxy headers: RESOLVED.**
- `--no-proxy-headers` is on `dev.py api_command` (used by up, web with reload, app, demo) and on `gateway_command`
  (phone, up), and on the Dockerfile CMD. The gateway passes `proxy_headers=False` in `sms_gateway.main` (compose runs it).
- The test runs each command through uvicorn's real click parser (`main.make_context`) and asserts `proxy_headers is False`.
  It also checks `main()` by monkeypatch, scans the repo for any other launcher, and runs a live pair: the forged XFF is
  counted as `ip:127.0.0.1` with the switch and as the forged address without it.
- CLAUDE.md rule 11, HOSTING.md, `.env.example` and `.env.deploy.example` are now true.
- Nit: the old plan docs `docs/AI_PLAN.md:832` and `docs/WEB_PLAN.md:244` still show the command without the flag.

## New issues

- **N11 [MED, brute-force bound] Filling the limiter tables evicts an account's backoff, at no hashing cost.**
  - `_Throttle.reserve` and `fail` pop the first-inserted key once `_names` has `THROTTLE_LIMIT` (10,000) keys. Reassigning
    a key does not move it, so an old target is evicted first.
  - A busy-refused junk name still leaves an empty `[]` key: `reserve` inserted it, and `release` only removes the stamp.
  - Probe P1 (real limit): after 10,000 new names, `alice` is gone and 11 tries are admitted again.
  - Probe P2 (limit 300, same path through `users.login`): with the "other" lane held full, 300 junk names cost 0 hashes
    (all `login_busy`) at 0.51 ms each in-process. Then alice gets 11 hashed guesses at once.
  - Estimate, many addresses: about 11 guesses per 10,000 junk requests, roughly 40k-190k a day per account (HTTP overhead
    not measured). That is far below round 3's 2.9 M a day but far above the claimed ~2.9k a day.
  - Estimate, one shared address: the pair block is evicted only by 10,000 real misses (hashed, about 5 min), so about
    10 guesses per 5 min, against 10 per 15 min documented.
  - Fix: never evict a name that is inside its backoff window, drop empty lists first, and when the table is full of active
    names answer 429 instead of evicting.
- No other new issue. The lane, reserve and release code does not change who a session belongs to: `_open_session_row` uses
  the looked-up row and the password-derived key, and the device id only picks a lane or pair.

## BLOCKERS

None. Evidence:
- Round 3 changes only throttles, lanes and launch flags. No query, RLS policy, key or sealing path is touched by
  `de86b5f..fc31364`.
- The device cookie is not a credential: every sign-in still needs the password's scrypt to open the key.
- The carried items below were checked against the code. None lets a person reach another person's rows, bypass sign-in,
  lose data or mishandle a key.

## KNOWN GAPS (ready for the PROGRESS.md backlog)

- N11: a junk-name flood evicts the per-account backoff from the 10,000-entry limiter table, raising the guess bound to about 11 per 10,000 junk requests.
- A stranger's flood fills the 4-slot "other" lane: new browsers, sign-up and the owner claim, change password and new recovery code get 429 (no per-address pre-hash cap).
- A stranger who keeps failing (one try per backoff of up to 30 s) holds NEW browsers of that account at 429 `login_wait`; a known device and the recovery code are the ways in.
- An invited person can fill the global 2-slot device lane (parallel good sign-ins) or the 1-slot recovery lane (parallel recoveries with their own code).
- The device cookie has no server-side expiry or epoch. It survives a password change or recovery and now also skips the pre-hash backoff (still limited to 10 misses per 15 min per pair).
- N9: only the first `X-Forwarded-For` line is read; behind a trusted proxy that adds its own line, the client picks its limiter address.
- Unconfirmed: what Tailscale Serve and Funnel put in `X-Forwarded-For` (it matters only with `RECTIFI_TRUSTED_PROXIES` set).
- N6: each login does `purge_unconfirmed` plus a user lookup before any limiter, on a 5-connection pool and one worker; a raw flood slows every session check.
- N1 LOW: a claim expiring during the owner re-key makes every sign-in purge wait on row 1, which can stall the pool; a double-submitted confirm answers `signup_expired`.
- N10: the repr meta-test is vacuous for NamedTuple and attrs and skips ImportError silently (latent: the only NamedTuple, `checkpoint.Memory`, holds no key).
- Round-1 LOW: the global sign-up limiter (10 per hour, failed tries too) lets anyone block every sign-up, the owner claim included.
- Round-1 LOW: before the owner claim is confirmed, the owner's phone gets an error, not 503 with Retry-After (not rechecked this round).
- Round-1 LOW: `/api/recover` timing still differs a little between real and unknown names (HKDF plus AES-GCM only for real ones; not remeasured).
- Round-1 LOW: the session cookie is `Secure` only in public mode. Laptop multi-user over Funnel gets it only via `RECTIFI_PUBLIC=1`, which `dev.py up`'s Funnel check makes you set. Serve traffic is inside WireGuard.
- Round-1 LOW: the last person's key stays on an idle pooled connection's row factory. Every checkout (`connect`, `connect_system`, the only two) sets a new factory, so there is no cross-person read; it is memory hygiene only.
- Round-1 LOW: friends can call `/api/phone/status`, which returns the server's MagicDNS name and published ports (LAN IPs only if opened) and runs `tailscale` on each call.
- Round-1 LOW: owner account delete leaves the LangGraph `store` memories (namespace uid 1) and checkpoints of unlisted threads; PRIVACY.md's "every row" claim is untrue. Not a BLOCKER: only the operator can issue an owner invite, and those tables are plain to the operator anyway.
- N3: the signed-out alerts sweep raises `VaultLocked` on an unsealed custom-merchant name, so the owner's alerts stall until the seal sweep runs.
- N5 (suspected): the inbox digest day uses the DB timezone in `reblind` and the process timezone at ingest; a same-day resend after a rotation can land in the inbox twice (a duplicate, nothing lost).
- Round-1 residual: an SMS with no UPI reference that is resent during a blind-key change, before that person signs in, can be added twice (a duplicate, nothing lost).
- Round-2 note: in a sweep dedupe clash, the dropped newer duplicate's `note` is not carried over (that path is unreachable since the re-key became atomic with the confirm).
- Docs nit: `docs/AI_PLAN.md:832` and `docs/WEB_PLAN.md:244` show the uvicorn command without `--no-proxy-headers`.

Class note: N10, the pooled-connection key, phone status, owner delete, N3, N5, the two duplicate notes and the docs nit are
not rate limiting. They go here because none exposes another person's data, loses data
for good or mishandles a key (evidence in each line). The owner may promote the owner-delete memory leftover if a server
handover to a new owner is a supported case.
