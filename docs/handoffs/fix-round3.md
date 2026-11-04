# Stage-6 fix, round 3 (branch `ws/fix`, the last allowed round)

Fixer: Sonnet. Input: `docs/handoffs/review-round3.md` (N7, N4 promoted from LOW, N8). Not touched (backlog): N9, N10 and the other
LOWs. Line numbers are on `ws/fix`.

## 1. N7 (MED): separate hash lanes

- `users.py` 78-80 (`AUTH_INFLIGHT` 4, new `AUTH_DEVICE_INFLIGHT` 2, `AUTH_RECOVERY_INFLIGHT` 1), 366-385 (`_LANES`, `_LANE_SLOTS`,
  `_derive(password, salt, lane)`): each lane has its own `BoundedSemaphore`; a full lane gives 429 `login_busy` at once, the other
  lanes are untouched. The total is 7 threads, so the threadpool keeps free threads for `SessionLock`.
- Lane choice: `login` (~683) uses "device" only when `_client_pair` says `known`, which means the web layer already verified a
  signed device cookie (an HMAC check, no hashing) and its uid is THIS account's row id. Another account's cookie, a forged
  cookie or no cookie use "other". `recover` (~848) hashes the new password in "recovery", reached only after the code was proven.
  Sign-up, change-password and recovery-code view stay in "other".
- Tests: `tests/test_fix_hash_lanes.py` (7). Eight parallel junk sign-ins with a slow hash fill exactly the "other" lane (the rest
  get 429); meanwhile a known device with the right password signs in, a recovery succeeds, another account's cookie gets no
  lane, a full device lane does not affect the other two, the lane sum is bounded, and the same through the routes (cookie from an
  earlier sign-in, recovery sets the device cookie). 5 of the 7 fail on the old code.
- Residual: a person with their OWN account and its valid cookie can fill the device lane with their own sign-ins (needs an
  invite). No stranger can. A per-client address cap before hashing was not added.

## 2. N4 (promoted to MED): account backoff refuses before hashing

- `_Throttle.reserve` / `release` (`users.py` 262-285), `delay_for` (256, now only the pair's own pause), `fail(..., reserved=)`
  (287), `login` (~668-700), `MESSAGES["login_wait"]` (92). For a client without a known device, `login` calls `reserve` before
  any hash. After `ACCOUNT_FREE_MISSES` (10) misses on a name, `reserve` refuses with `AuthError(429, "login_wait", retry_after=n)`
  (the route sends `Retry-After`) until the backoff (2 s doubling to 30 s) since the latest miss OR admitted try has passed. An
  admitted try is booked as a provisional miss, so parallel tries count each other; a right password or a busy refusal releases
  it. A refusal itself is not a miss, so refusals cannot extend the wait. Unknown names get the same treatment (no oracle).
- Known devices skip `reserve`. Recovery has its own bucket and never calls it.
- Tests: `tests/test_fix_account_backoff.py` (6): 40 parallel guesses from 40 addresses after the backoff elapsed run exactly 1
  hash (39 get 429); the refusal runs 0 hashes and is not a miss; unknown names behave the same; a good sign-in leaves no
  provisional miss; a known device signs in at once; during the backoff the recovery code works and sets the device cookie (route
  test with `Retry-After`). 5 of the 6 fail on the old code.
- Changed existing tests (the new client now gets 429 where it used to get a delay): `test_fix_lockout.py` (2 tests),
  `test_fix_shared_address.py` (2 tests). They use a `miss()` helper for setup loops.
- Residual: while a stranger keeps failing (one try per backoff, at most 30 s), a NEW browser on that account can be refused
  repeatedly; a known device and the recovery code are the ways in. Brute force across many addresses is now about one hash per
  2-30 s per account after the first 10 misses, instead of about 34 a second.

## 3. N8 (MED): uvicorn proxy headers off on every launch path

- `scripts/dev.py` 197 (`api_command`, used by up, web, app, demo) and 264 (`gateway_command`, used by phone and up) add
  `--no-proxy-headers`. `rectifi/sms_gateway.py` 330-331 (`main`, also the compose gateway service) passes `proxy_headers=False`.
  `deploy/Dockerfile` 34-35 CMD adds `--no-proxy-headers`. `deploy/compose.oracle.yml` has no uvicorn command of its own (the app
  uses the image CMD, the gateway runs `python -m rectifi.sms_gateway`). `rectifi/api/app.py` docstring command updated.
  A grep of the repo found no other `uvicorn.run`, `Config` or `-m uvicorn` outside tests (tests/web_support.py runs a server in a
  thread on 127.0.0.1 and is not a launch path).
- Tests: `tests/test_fix_proxy_headers.py` (10): the real uvicorn CLI parser says `proxy_headers` is False for dev.py's app
  (with and without reload) and gateway commands and for the Dockerfile CMD, True for plain defaults; `sms_gateway.main` passes
  `proxy_headers=False`; compose lines with uvicorn carry the flag; a scan makes any new file that starts uvicorn fail until it is
  added to the list and carries the switch; live: a real uvicorn server on the options of `api_command`, empty trusted list, a
  request with `X-Forwarded-For: 203.0.113.5` from 127.0.0.1 is counted under `ip:127.0.0.1`, and the control server on uvicorn's
  defaults counts it under the forged address. 7 of the 10 fail on the old code.
- Fixed the wrong test: `test_deploy_files.py::test_dockerfile_starts_one_worker_and_trusts_no_proxy_headers` now asserts the flag
  IS present. `test_dev_script.py` expectations (3 asserts) include the flag.
- Docs: CLAUDE.md rule 11 addendum (153 lines), HOSTING.md (one sentence on the Real client addresses bullet), `.env.example`,
  `.env.deploy.example`, Dockerfile comment.
- Residual: with `RECTIFI_TRUSTED_PROXIES=127.0.0.1` behind Serve, what Tailscale really puts in `X-Forwarded-For` is still
  unconfirmed (as before). N9 (only the first header line is read) is still open.

## Test files run (one at a time, `rectifi_fix_test`)

test_fix_hash_lanes 7, test_fix_account_backoff 6, test_fix_proxy_headers 10, test_fix_shared_address 12, test_fix_lockout 9,
test_auth_multiuser 47, test_auth 37, test_users 18, test_deploy_files 46 passed + 8 skipped, test_dev_script 52,
test_sms_gateway 46, test_tailscale 66 (its dev.py assert gained the flag). Not run: the full suite, the browser tests, vitest (main session).
