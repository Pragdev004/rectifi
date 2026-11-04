# Stage-6 re-review of the round-2 fixes (branch `ws/fix`, head 46853cb)

Reviewer: Opus, read-only on the code. Scope: N1, item 3 + N2, item 7 from `review-round2.md`, the `fix-round2.md` claims,
`git diff b377de4..46853cb`, and any new issue that diff brings in. Probes are outside the repo (session scratchpad
`rereview2/test_rr3.py`, with a `conftest.py` that execs `tests/conftest.py`). They ran once, on `rectifi_fix_test`: 7 passed
(each probe asserts what it found). I did not rerun the suite: the main session reports it green on 46853cb.

Verdicts: N1 RESOLVED · N2 RESOLVED · item 3 PARTIAL · item 7 RESOLVED (the meta-test has a gap, N10).
New issues: 2 MED (N7, N8), 2 LOW (N9, N10), plus two LOW points marked "suspected".

## N1 [MED] owner re-key atomic with the confirm: RESOLVED

- The re-key runs on the confirm's own connection (`users.py:596-601` → `seal_sweep.rekey_dedupe(conn=...)` →
  `db.tenant_scope`). Nothing commits on its own. `_rekey_owner_dedupe` no longer catches errors. `confirm_signup` turns any
  error into `AuthError(500, "confirm_failed")`, and leaving `connect_system` rolls the transaction back.
- Lock order: the confirm takes `users` row 1 `FOR UPDATE` first (`:582`), then reads the pending session again, then the invite.
  The purge's `_reset_owner_claim` also takes row 1 first, inside one outer transaction that holds it until commit. Its
  `AND confirmed_at IS NULL` is checked again after the wait, so a purge that waited on a finished confirm resets nothing. The
  owner `signup` path locks an *unreserved* invite and then row 1. That is a different invite row from the confirm's
  (`reserved_by = 1`), so there is no cycle. RLS stays narrow: inside `tenant_scope`, `app.scope='system'` only widens
  `phone_devices`, `users`, `invites`, `user_sessions` and `allowed_merchants`, and the re-key touches none of them.
- **R1** (my round-2 P4, unchanged: a purge after expiry from another thread, during the re-key): the confirm succeeds, the purge
  raises nothing, row 1 is confirmed with its salt, `upi_ref` is `b1:`. No `DeadlockDetected`.
- **R2** (a real SQL error inside the re-key aborts the transaction, so `tenant_scope`'s `finally` fails too): 500
  `confirm_failed`, row 1 unconfirmed, `upi_ref` still plain, 1 pending session left. The same token then succeeds.
- **R3** (another person's sign-in, whose purge sees the claim as expired, during the re-key): the sign-in waits about 1.6 s on
  row 1 and then answers `invalid`. The confirm wins, and there is no deadlock.
- LOW, suspected: the confirm now holds row 1 and the invite for the whole re-key. Only in the P4 window (the claim expires during
  the re-key) does every sign-in's and sign-up's `purge_unconfirmed` wait on row 1, each holding a pool connection
  (`max_size=5`). A large laptop history could stall sign-in for that long. A double-submitted confirm waits, then gets
  `signup_expired` although the first one worked (a UX point). `_fetch` uses `SKIP LOCKED`, so a row another writer has locked is
  left plain for the sweep (behaviour from before this round).

## N2 [MED] global miss budget: RESOLVED (as filed)

`GLOBAL_MISS_LIMIT`, `GLOBAL_MISS_WINDOW` and `miss_window` are gone (`users.py:621-653`, and the reworked test in
`test_fix_lockout.py`). No count of misses can refuse a new client any more. But removing the budget opened the hash-slot lever
wider: see N7.

## Item 3 [MED] a stranger keeps a real person out: PARTIAL

Resolved on the miss path:
- The device cookie is set on sign-up confirm (`routes/auth.py:148`), sign-in (`:192`) and recovery (`:250-252`, only after a
  successful `users.recover`; `uid_of` matches the same case-insensitive name).
- A known device counts as `(name, "dev:"+id)` (`users.py:616-618`) and skips the account-wide delay (`delay_for`). Only its own
  pair's misses can pause or block it. The test `test_fix_shared_address.py` P2 shows 200 with no pause.
- The stated residual is acceptable: a brand-new browser behind the shared address can be held out for 15 minutes only while a
  stranger is failing on that account, and the recovery code (its own bucket) is a way out.

Not resolved: a stranger can still keep everyone out, known devices included, through the hash cap (N7). The recovery escape
needs a hash slot too (`users.py:805`).

Trusted proxies (`api/auth.client_address`): the app-level rule is right. The header is read only from a trusted peer, from the
right, with trusted hops skipped. A direct client can't pass. A bad hop falls back to the peer. Typos trust nothing, and IPv6 is
grouped by /64. Two problems sit around it: N8 (uvicorn already rewrites the peer) and N9 (only the first header line is read).

Brute force, my judgement:
- One shared address with no XFF: at most 10 guesses per 15 minutes per account. That is fine.
- Distinct addresses (natively through uvicorn, see N8; Docker with `RECTIFI_TRUSTED_PROXIES`; IPv6, where one free /48 gives
  65,536 /64 keys): the per-account delay is applied after the hash (N4), so it does not bound anything. The only bound is the
  global hash rate. **R4** measured scrypt at 0.118 s here, so `AUTH_INFLIGHT` 4 allows about 34 guesses a second, roughly
  2.9 M a day, all aimed at one account.
- That is out of reach for a random 12+ character password. It is not for a human 12-character one taken from a leak list
  (hours). So the bound is acceptable only once N4 is fixed: refuse unknown clients inside the account delay window *before*
  `_derive`. With XFF or IPv6 in play I would raise N4 from LOW to MED. Otherwise, add a breached-password check.

## Item 7 keys in repr: RESOLVED; the meta-test has a gap (N10)

- `SignedIn.k` is `field(repr=False)` (`users.py:111`), and a direct test checks `repr` and `str`.
- The meta-test cannot pass vacuously for dataclasses. **R7**: a leaky dataclass is flagged, all 89 of 89 modules import here,
  and the anchors assert that `SignedIn.k`, `UserContext.key` and `UserContext.private_key` were really checked.

## New issues

- **N7 [MED] A junk sign-in flood refuses everyone, known devices and recovery included, and round 2 made this worse.**
  `_derive` (`users.py:342-350`) refuses with 429 when 4 hashes are in flight. Unknown names still cost one scrypt
  (`_burn_scrypt`), and random names never block a pair, so about 4 parallel junk requests in a loop (around 34 a second here)
  hold every slot. **R4**: with the slots held, a known device with the right password gets 429 `login_busy`. Before round 2, the
  miss budget refused unknown clients *before* the hash after 120 misses a minute, so junk could fill only about 6% of slot time.
  Now it can fill all of it. Fix: keep 1 or 2 slots that only known-device sign-ins and recovery can use. Also add a small
  pre-hash cap per client address for unknown clients (with trusted XFF it is per real address).
- **N8 [MED] uvicorn already believes `X-Forwarded-For` from loopback, so "empty = header ignored" is untrue for the native
  run.** uvicorn 0.54 defaults to `proxy_headers=True` and `forwarded_allow_ips="127.0.0.1,::1"`. **R6**: a request from peer
  127.0.0.1 carrying `X-Forwarded-For: 203.0.113.5` reaches the app as client `203.0.113.5`. `dev.py api_command` and the
  Dockerfile pass no `--no-proxy-headers`.
  - So behind Serve or Funnel (`dev.py up`), XFF decides the limiter's key whatever `RECTIFI_TRUSTED_PROXIES` says. The doc
    advice `RECTIFI_TRUSTED_PROXIES=127.0.0.1` for a native run does nothing.
  - The CLAUDE.md rule 11 addendum ("from any other peer the header is ignored"), HOSTING.md:198 and `.env.example` all say
    otherwise.
  - Whether Tailscale appends a real address or passes a client's header through is unconfirmed. If it passes it through, a
    client can make up a fresh address per guess: suspected.
  - `test_dockerfile_starts_one_worker_and_trusts_no_proxy_headers` checks that the flag is *absent*, which leaves the
    behaviour on. Docker is safe only because the bridge peer is not in uvicorn's default list.
  - Fix: add `--no-proxy-headers` to the app command (dev.py and Dockerfile) and the gateway's `uvicorn.run`, plus a test that
    asserts it.
- **N9 [LOW] Only the first `X-Forwarded-For` line is read.** `routes/auth._client` calls `request.headers.get`. **R5**: with
  two header lines (the client's `6.6.6.6`, then the proxy's own `203.0.113.5`) and a trusted peer, the limiter counts
  `6.6.6.6`. A proxy that adds its own line (for example HAProxy `option forwardfor`) lets the client pick its address.
  uvicorn joins the lines. Go proxies merge them into one, so Tailscale is not affected. Fix:
  `", ".join(request.headers.getlist("x-forwarded-for"))`.
- **N10 [LOW] The meta-test's NamedTuple and attrs coverage is vacuous.** `own_repr()` treats any `__repr__` in the class
  `__dict__` whose code is not from `<string>` as the class's own. The repr that NamedTuple generates comes from
  `collections/__init__.py`, and attrs' comes from a generated file name, so both always pass. **R7**: a leaky NamedTuple and a
  leaky attrs class are both *not* flagged, although the NamedTuple's repr shows the value. `rectifi` has no such class today
  (R7 lists none), so the gap is latent. Also, `except ImportError: continue` skips a broken module without a word, and nothing
  asserts the count. Fix:
  - treat `collections`- and attrs-generated reprs as generated;
  - add NamedTuple and attrs cases to `test_a_detector_catches_a_leaky_class`;
  - skip only an explicit allow-list of optional modules.

## Not verified

- What Tailscale Serve or Funnel really put in `X-Forwarded-For` (N8, and the old Funnel question).
- No load test of N7. R4 holds the slots by hand to prove the mechanism, and the junk rate needed is worked out from the
  measured scrypt time.
- The pool stall in the P4 window (N1 LOW).
- Not rerun: the full suite, the e2e and browser files, vitest (reported green on 46853cb).
