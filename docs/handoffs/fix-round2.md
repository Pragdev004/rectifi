# Stage-6 fix, round 2 (branch `ws/fix`)

Fixer: Sonnet. Input: `docs/handoffs/review-round2.md` (PARTIAL verdicts 3, 4, 7 and new issues N1, N2).
Not touched (backlog): N3, N4, N5, N6, the device-cookie expiry. Line numbers are on `ws/fix`.

## A. N1 (MED): the owner re-key is atomic with the claim confirm

- `users.confirm_signup` (`users.py` ~565): the re-key now runs on the confirm's OWN connection, in the same transaction as
  `confirmed_at`. `seal_sweep.rekey_dedupe(ctx, conn=...)` (`seal_sweep.py` ~308) uses that connection through the new
  `db.tenant_scope(conn, ctx)` (`db.py`, sets `app.user_id` and the cell-opening row factory for the transaction only, puts them
  back after). Nothing commits until the confirm commits.
- If the re-key raises, the confirm is rolled back and the person gets `AuthError(500, "confirm_failed")`: "We couldn't finish
  setting up your account. Nothing was changed. Try again." The pending session stays, so the same token retries. There is no
  "log and carry on" any more (`_rekey_owner_dedupe` re-raises).
- Lock order, the same in `confirm_signup` and `purge_unconfirmed`: the person's `users` row first (`SELECT ... FOR UPDATE`),
  then the invite. After it gets the row, the confirm re-reads the pending session; if a purge got there first the session is gone
  and the confirm stops with `signup_expired` before it touches any row. If the confirm got there first the purge finds
  `confirmed_at` set and resets nothing. The `DeadlockDetected` in P4 is gone.
- Tests: `tests/test_fix_claim_atomic.py` (4): P4 as a regression (purge during the re-key: the confirm wins fully, no
  deadlock, rows `b1:` and the salt present); purge first (row 1 reset, old rows still plain, confirm says expired); the purge
  holds row 1 while the confirm arrives (confirm waits, then stops, rows untouched); forced re-key error (claim unconfirmed, rows
  plain, token still valid, retry succeeds). 3 of the 4 fail on the old code (the 4th is the sequential case that was already safe).
- Existing tests: `test_fix_dedupe_claim.py` monkeypatch `_rekey_owner_dedupe` with `lambda *a, **k: None` (the signature gained `conn`).

## B. Item 3 PARTIAL + N2 (MED): a stranger cannot keep a person out from a shared address

1. Device cookie: login and sign-up confirm already set it; recovery now does too (`api/routes/auth.py` `recover`, uses the new
   `users.uid_of`, only after a successful recovery; a failed one marks nobody).
2. A known device with the right password is never refused or delayed by anyone else's misses: its pair is `(name, dev:<id>)`,
   it skips the account-wide delay, and the global refusal below is gone. Only its own misses can block its own pair.
3. Trusted proxies: `config.trusted_proxies()` (`config.py`, `RECTIFI_TRUSTED_PROXIES`, comma separated addresses or networks,
   default empty = header ignored; a typo trusts nothing) and `auth.client_address()` (`api/auth.py`). The header is read only
   when the socket peer is trusted, from the RIGHT: trusted hops are skipped and the first untrusted address is the client, so
   whatever a client writes at the left is never reached. Garbage header: the peer is used. IPv6 clients are counted by /64.
   `routes/auth._client` uses it for login and recovery.
4. N2: `GLOBAL_MISS_LIMIT`, `GLOBAL_MISS_WINDOW` and `miss_window` are removed from `users.py`. The only global 429 is
   `AUTH_INFLIGHT` (hashes in flight).
5. Brute force stays bounded by the per-(account, client) pause and 15-minute block and the per-account delay for unknown clients.
- Tests: `tests/test_fix_shared_address.py` (12): P2 (10 bad tries from 127.0.0.1, the known device signs in with no pause; a
  new browser is held out, uses the recovery code, gets the cookie, then signs in at once with the new password), the cookie on
  confirm, login and recovery, P3 (300 junk misses, a new client still signs in; exactly the in-flight cap gives 429), brute force
  per pair and the account delay, the trusted-proxy rule (default ignores, trusted peer honoured, untrusted peer ignored,
  spoofing at the left, IPv6, typos) and the same through the routes. `test_fix_lockout.py`: the old global-budget test is now
  "never refuses a new client". 9 of the 12 new tests fail on the old code.
- What Funnel sends: NOT confirmed. The code and docs only say `X-Forwarded-For`; the repo has no sample of Funnel or Serve
  traffic (it uses `Tailscale-User-Login` for the optional lock, and that header is not used here). The default is to ignore forwarded
  headers, so if the guess is wrong nothing gets worse. To use it: set `RECTIFI_TRUSTED_PROXIES=127.0.0.1` for a native run, or
  the Docker bridge network (for example `172.16.0.0/12`) in the container, after checking the header with a test request.
- Residuals (also in `users.py` header and `docs/HOSTING.md`):
  - A brand-new browser, behind a shared address, while a stranger is actively failing from that address, can be blocked up to 15
    minutes. The recovery code always works (its own bucket) and gives that browser its device cookie.
  - First roll-out: browsers that have never signed in since this change have no cookie until their next good sign-in.
  - Parallel guesses at one account from many different addresses are limited only by the hashes in flight (4) and the delay
    that is applied after the hash (N4, backlog). Min 12 characters and scrypt make that a long way from feasible, but it is no
    longer capped at 120 a minute.
  - Docker: with no `RECTIFI_TRUSTED_PROXIES` every visitor shares the bridge address, as before.

## C. Item 7 PARTIAL: keys in repr

- `users.SignedIn.k` is `field(repr=False)` (`users.py` ~103).
- `tests/test_no_secret_in_repr.py` (4): imports every `rectifi` module (89 import here, none skipped), and for each dataclass,
  NamedTuple, attrs class and pydantic model fails on any field whose name has a secret-sounding word (key, k, secret, token,
  password, salt, digest, endpoint, cookie, dek, kek, verifier, credential, signature, private) unless it is `repr=False` or the
  class writes its own `__repr__` (the dataclass-generated one is seen through and does not count). Public values are in a
  commented `PUBLIC` allow-list (`public_key`, `dedupe_key`, `scope_key`, `Insight.key`, `Recurring.key`, `Brand.key`, retriever `k`
  and `fetch_k`), checked for stale entries. Also a direct test of `SignedIn` and `UserContext` reprs. Fails on the old `users.py`.
- The scan found nothing else to fix: only `SignedIn.k` and the already fixed `UserContext` fields were real.

## Docs

`CLAUDE.md` rule 11 multi-user addendum (still 153 lines), `.env.example`, `.env.deploy.example` (commented
`RECTIFI_TRUSTED_PROXIES`), `docs/HOSTING.md` (Funnel limits paragraph is now true for multi-user; the proxy setting). Note:
`docs/HOSTING.md` was already 333 lines before this round; I added 2 lines and did not split it.

## Test files run (one at a time, `rectifi_fix_test`)

| File | Result |
|---|---|
| test_fix_claim_atomic.py (new) | 4 passed |
| test_fix_dedupe_claim.py | 7 passed |
| test_fix_purge.py | 4 passed |
| test_users.py | 18 passed |
| test_seal_sweep.py | 21 passed |
| test_fix_shared_address.py (new) | 12 passed |
| test_fix_lockout.py | 9 passed |
| test_auth_multiuser.py | 47 passed |
| test_auth.py | 37 passed |
| test_no_secret_in_repr.py (new) | 4 passed |
| test_userctx.py | 8 passed |
| test_deploy_files.py | 46 passed, 8 skipped |
| test_foundation.py | 25 passed |
| test_rls.py | 16 passed |

Not run by me (the main session runs them): the full suite, `test_isolation_routes`, `test_multiuser_e2e`, `test_web_e2e*`, `npm test`.
`web/dist` is unchanged (no `web/src` change).
