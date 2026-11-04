# Stage-6 re-review of the round-1 fixes (branch `ws/fix`, head 104906a)

Reviewer: Opus, read-only on the code. Scope: the 7 items in the brief, `git diff integration..ws/fix` (minus `web/dist`),
`review-round1.md`, `fix-round1.md`. Probes live outside the repo (session scratchpad `rereview1/test_rr_probes.py`, a
`conftest.py` that execs `tests/conftest.py`) and ran on `rectifi_fix_test`. Also run: `tests/test_fix_blind_key.py` (11 passed).
Line numbers are on `ws/fix`.

Verdicts: 1 RESOLVED (reported path; see N1) · 2 RESOLVED · 3 PARTIAL · 4 PARTIAL · 5 RESOLVED · 6 RESOLVED (with N3) · 7 PARTIAL.
New issues: 2 MED, 4 LOW (2 of them suspected).

## Items

**1. HIGH, old plain keys + new `b1:` keys: RESOLVED for the reported path.**
- `rekey_dedupe` (`seal_sweep.py:297`) covers every dedupe column (`upi_ref`, `fingerprint`, `sms_inbox`/`sms_seen.text_hash`,
  `pattern_blind`, `target_blind`, alert keys, prefs) and fills `upi_ref_sealed` / `digest_sealed`. It runs in `confirm_signup`
  (`users.py:587`) before `confirmed_at`, and `authenticate_device` (`tools.py:1024`) gives an unconfirmed person no keys.
  No other writer can use new keys before the confirm: no cookie exists yet, and the ticker only writes `alerts`.
- Clash handling (`_apply`, `_drop_duplicate`): a clash is only found on the columns of the real unique indexes (`CLASH_SCOPE` adds
  `direction` and `(scope, period)`), so two rows clash only when rule 3 would have refused the second one anyway. No distinct
  payment can be dropped that the database would have kept. Keeping the older row is right (it has the person's category; a `user`
  category on the newer row is handed over). Small loss: a `note` on the dropped newer row is not carried (LOW, not listed).
  The FKs to `transactions` are `CASCADE` / `SET NULL`, so the delete cannot fail. Savepoint plus one retry covers a live insert.
- Residual gaps are stated truthfully. But the claim ordering is not atomic. See N1: under one race the fix makes things worse.

**2. MED, abandoned claim + friend named "owner": RESOLVED.** `RESERVED_NAMES` refuses "owner" in any case on the friend path
(after `strip()`). There is no rename route. `_reset_owner_claim` falls back to `owner-<hex>` in a savepoint. `purge_unconfirmed`
isolates each row. Owner delete (`account.py:238`) still writes 'owner', but only with no friends, so it cannot clash.

**3. MED, stranger lockout: PARTIAL.**
- Sound parts: there is no account-wide lock any more, and `_verify_current` is fed only when signed in. A known device has its own
  pair, skips the account delay and the global budget. The device cookie is HMAC-signed with the session secret under a `device.`
  prefix. A session body is base64url and has no `.`, so neither cookie can pass as the other. It is bound to the uid
  (`_client_pair` checks `device[0] == row["id"]`). Recovery has its own `_Throttle`. Memory is bounded (10k pairs, 10k names,
  `miss_window` 100k). Evicting a blocked pair needs about 10k misses, which the global budget spreads over about 80 minutes.
- Not resolved: behind Funnel or the Docker port every client has the same address, which is the deployment this item was about.
  So the pair is `(name, ip:<proxy>)` for every browser that has no device cookie. **Probe P2:** 10 wrong passwords from
  127.0.0.1, then the real person, also from 127.0.0.1, with the right password and no device cookie, gets `locked`,
  retry 900 s. A stranger can renew that every 15 minutes. It hits everyone at first roll-out (nobody has a device cookie yet),
  a new phone, cleared cookies and private windows. Recovery from a new device is blocked the same way. The fixer lists this
  as a residual, but it is the main case, not an edge case.
- Brute force: on one shared address the per-pair cap still means about 10 guesses per 15 min per account, as before. With
  distinct addresses (direct exposure, or if X-Forwarded-For is trusted later as the handoff suggests), the "per-account delay"
  is not a limit. It is an `asyncio.sleep` *after* the hash, so parallel requests ignore it, and only the global 120/min caps
  guesses at one account (N4).
- Fix idea: key the shared-address pair block on (name, address) only for misses that a device cookie does not vouch for, and
  let a correct password through a blocked new-device pair at the delay, not the block (the scrypt plus the delay already caps
  the rate). At the least, say in HOSTING.md and the sign-in page that a new device can be held out for 15 minutes.

**4. MED, login flood: PARTIAL.** `_derive`'s non-blocking `BoundedSemaphore(4)` works. Every scrypt site uses it, a flood gets
429 at once, `pause` is async, and the fixer's test shows at most 4 in flight. So SessionLock's threadpool is not starved by
hashing. Still open:
- N2 (MED): the global miss budget lets a stranger shut out every new browser for everyone (probe P3).
- Suspected (LOW, N6): each login does `purge_unconfirmed` (a write transaction) and a user lookup *before* the budget and
  slot checks. The pool has `max_size=5` (`db.py:124`), and `SessionLock` needs the same pool. A raw flood of junk logins
  queues ahead of every page's session check. Not load-tested. Fix: run the cheap budget and slot checks first, and purge at
  most once every N seconds.

**5. MED, gateway limiter: RESOLVED.** The token is checked first (`sms_gateway.py:185-196`). Only a wrong token reaches
`failures`, and a good token never reads it. The codes are still 401/429/413/422/503 and 200. Carried LOW from round 1: an
unconfirmed owner's phone still gets 500 (VaultLocked), not 503 with Retry-After. The phone retries either way.

**6. MED, RECTIFI_BLIND_KEY: RESOLVED.**
- The key comes only from the environment (`config.blind_key`). Compose passes it via `env_file` to the app and gateway only.
  The backup container has no `env_file`, and `pg_dump` dumps only the database. Nothing writes the key to the DB, a log or a
  message. `set-password` adds a key only when none exists (`--print` shows it, like the session secret). `deploy-check` and
  `doctor` FAIL without it in multi-user mode, and both the app and the gateway refuse to start. HOSTING.md has
  "Back up the blind key".
- Fingerprint: HMAC of a fixed label, cut to 64 bits. It cannot reveal the key. It only confirms a guessed key, and a
  `token_urlsafe(48)` key cannot be guessed.
- Re-blind: the only `blind_ingest` users are `transactions.upi_ref` (tools.py:277, importer.py:487) and the two SMS digests
  (tools.py:1113), and `reblind` covers all three. `upi_ref_sealed` and `digest_sealed` are written on every path. Running it
  twice is a no-op (`new != row[col]`, fingerprint stored last). Two runs at once are stopped by the per-process `-uid` lock,
  and only the API runs hooks. A clash with a live gateway write goes through `_apply`. Rotation is tested.
- Docs: PRIVACY.md, `WHO_CAN_READ`/`LIMITS` and Privacy.tsx now say what is true: dump alone no; owner with key and DB yes;
  data-key hashes need the password.
- Suspected (LOW, N5): `_reblind_value` rebuilds an inbox digest with `received_at.date()` (DB session timezone), but ingest
  used `date.today()` (process timezone) when the SMS has no `sent_at`. If the two zones differ, a same-day resend after a
  rotation lands in the inbox twice.
- **Deviation (alert keys and prefs under `vault.blind`): sound, with one regression (N3).** Export drops hashes, delete
  removes rows, and password change or recovery keep the same data key, so the hashes stay stable. The catch-up runs with the
  key. Signed out, a custom name is normally sealed and reads as None. But in the owner's pre-seal window it is plain.

**7. Keys in repr: PARTIAL.** `UserContext` is fixed. But `users.SignedIn` (`users.py:98`, a frozen dataclass) still shows
`k` in its repr. **Probe P1:** `repr(confirm_signup(...))` contains `k=b'...'`. `k` is the cookie secret that unwraps the data
key from `user_sessions`. Fix: `field(repr=False)` on `k`. No log call prints a key or token: `log.exception` is used only in
two insights routes and jobs, and the others log `type(exc).__name__` or ids. `vault` errors carry no key material.

## New issues

- **N1 [MED] The owner claim's re-key is not atomic with the confirm. A race leaves the old rows keyed under keys that are
  gone.** `users.py:587-590`, `seal_sweep.rekey_dedupe`. The re-key commits on its own pooled connections while the confirm
  transaction holds the invite lock but not row 1. **Probe P4:** a purge from another request ran during the re-key (the
  pending session expired meanwhile). It reset row 1, then waited on the invite. The confirm's `UPDATE users` waited on the
  purge. Postgres reported `DeadlockDetected` and aborted the **confirm**. Result: row 1 is back to `owner` with no
  verifier and no ingest salt, but `transactions.upi_ref` is already `b1:` under the thrown-away salt and data key. The next
  claim and the sweep treat `b1:` as done (`_plain_blind`). So every old row's dedupe key can never match again, and
  re-importing old statements doubles them, silently and forever. The same outcome follows if the confirm fails for any
  reason after the re-key and the claim is later abandoned. Also, a re-key exception is only logged (`_rekey_owner_dedupe`)
  and the confirm goes on, which reopens the original window, including the stated no-reference SMS gap. The trigger needs a
  confirm close to the 30-minute expiry, a slow re-key and a concurrent request, so it is rare. The damage is permanent.
  Fix: `SELECT ... FROM users WHERE id = 1 FOR UPDATE` in the confirm before the re-key (the purge then waits and sees
  `confirmed_at`). Fail the confirm (500, retry) when the re-key raises. Ideally do the re-key in the confirm's own
  transaction.
- **N2 [MED] The global miss budget lets a stranger block sign-in on every new browser.** `users.py:621-622`. 120 misses per
  minute from anyone (2 per second, random names, cheap) make every browser without a device cookie get 429 `login_busy` for
  every account. **Probe P3** confirms the 429. This is the same "stranger keeps people out" class as item 3, now global.
  Fix: count the budget per address and globally only as a far higher ceiling, or let the budget turn on a delay instead of a
  refusal.
- **N3 [LOW] The signed-out alerts sweep raises `VaultLocked` on a plain custom-merchant name.** `alerts.py:282`
  (`vault.blind` needs the data key). **Probe P5:** laptop-era debit at a custom merchant with a cap, newer than the
  watermark, then the owner claim, then the ticker's context. `alerts.sweep` raises `VaultLocked`. `_tick_one` fails for the
  owner every minute (no alerts, digest or retries) until the seal sweep has sealed those payees or a signed-in sweep moves
  the watermark. Before the fix, `blind_ingest` worked here. Fix: in `_name_part`, skip (return None and drop the alert) when
  `userctx.current().key is None`, so the catch-up fires it later.
- **N4 [LOW] The per-account delay is not a rate limit.** It is applied after the hash and asynchronously, so parallel
  attempts ignore it. It matters only with distinct client addresses (see item 3). Fix: check it before `_derive` and refuse
  (429) inside the delay window.
- **N5 [LOW, suspected] Inbox digest day: timezone mismatch in `reblind`** (item 6).
- **N6 [LOW, suspected] Junk logins do DB writes before any limiter check, on a 5-connection pool** (item 4).
- Also LOW: the device cookie has no server-side expiry or epoch. It survives a password change or recovery and keeps its
  global-budget exemption (it opens nothing). Fix: put `session_epoch` in it.

## Not verified

- Funnel's real client address (assumed one proxy address, as round 1). No load test (N6).
- Not rerun: `test_fix_dedupe_claim.py`, `test_fix_lockout.py`, `test_fix_purge.py`, the e2e and browser files (reported green
  by the main session). P4 was made deterministic with a forced expiry inside the re-key, and the deadlock victim may vary.
