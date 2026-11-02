# WS-AUTH handoff (branch ws/auth)

Built: `rectifi/users.py`, `rectifi/api/auth.py` (multi-user cookie, `resolve_user`), `rectifi/api/routes/auth.py`,
the WS-HOST block in `rectifi/api/security.py` (SessionLock binds the person, `FRIEND_ROUTES`, D11 check),
`scripts/devtools/users.py` (`invite`, `invites`, `revoke-invite` are real), tests in `tests/test_auth.py` (3 status
asserts updated, see Decisions), `tests/test_auth_multiuser.py`, `tests/test_users.py`.

## Requests for the integrator (files I do not own)

1. **tests/test_tenancy_migration.py::test_the_invite_commands_say_they_are_not_built_and_change_nothing** now fails on
   purpose: the three commands are real. Replace it with a check that `set(users.COMMANDS)` still equals
   `{"migrate-tenancy","invite","invites","revoke-invite"}` (the real behaviour is tested in `tests/test_users.py`).
2. **WS-AI / chat memory namespace.** In multi-user mode `request.state.uid` is the real int (`1` for the owner). Laptop and
   single-password mode still say `"owner"`. `api/routes/chat.py` does `str(value)`, so the owner's memories would move from
   namespace `"owner"` to `"1"`. Map uid 1 to `"owner"` there (or migrate the store key) so existing memories stay visible.
3. **WS-ALERTS / lifespan (`api/app.py`).** In multi-user mode `lifespan` calls `alerts.ensure_watermark` and
   `jobs.requeue_unfinished` with no person bound, so they raise `NoUser`. `tick` must loop users under `connect_system`
   and bind each one; `requeue_unfinished` moves to owner sign-in (`hooks.ai_after_sign_in`). Nothing in my files can fix it.
4. **WS-ACCOUNT.**
   - Owner claim (D9) leaves `users.encrypted = false` and sets `sweep_state = 'pending'` at confirm. The sweep should
     encrypt old rows, then set `encrypted = true` (this turns on the DB trigger). Friends are `encrypted = true` from
     sign-up. `account_after_sign_in` is where the sweep resumes; it is called at confirm and at every multi-user login.
   - `/api/privacy` is in `SESSION_EXEMPT` (public). `/api/account` (GET POST DELETE) and `GET /api/account/{x}` are already
     on the friend list. Any other friend-facing route a stream adds needs one line in `_FRIEND_RULES` (security.py, my block);
     send it to me or add it in integration. Until then a friend gets 403 on it (deny by default).
   - `DELETE /api/account` must verify the password through `users` (`_verify_current` counts a miss against the account);
     I did not add a public helper. Ask and I will expose `users.verify_password(uid, password)`.
5. **WS-WEB.** Contract, all JSON, errors are `{"error":{"code","message","field"}}`:
   - `GET /api/auth/status` -> `{required, signed_in, multiuser, username, is_owner}` (always these five keys).
   - `POST /api/auth/signup {invite, username, password}` -> 201 `{signup_token, recovery_code}`; `.../signup/confirm
     {signup_token}` -> `{ok, username, is_owner}` + cookie. Codes: `invalid_invite` (one message for wrong/used/expired/
     reserved), `username_taken` (409), `signup_limited` (429), `signup_expired` (400, token spent or older than 30 min), `invalid` (422).
   - `POST /api/auth/login {username, password}` -> `{ok, username, is_owner}`; 401 `invalid_login`, 429 `login_locked`
     (+ `Retry-After`). Laptop/single-password login still takes `{password}` only (401 `invalid_password`).
   - `POST /api/auth/recover {username, recovery_code, new_password}` -> `{recovery_code}` (the NEW code; show it once, old one is
     dead, everyone is signed out, no cookie). 401 `invalid_recovery` for a wrong code and an unknown name alike.
   - `POST /api/auth/change-password {current, new}`, `POST /api/auth/recovery-code {password}` -> `{recovery_code}`,
     `POST /api/auth/logout`, `/logout-all`. Wrong current password: 401 `invalid_password`, field `current`.
   - Friends get 403 `forbidden` on every owner-only route (Ask, Charts, Library, Memory, Traces, Models, classify, Explain).
6. **WS-DEPLOY.** Multi-user mode needs only `RECTIFI_SESSION_SECRET` (32+ chars). `RECTIFI_PASSWORD_HASH` is ignored there.
   Invite runbook: `python scripts/dev.py invite [--days 7] [--owner]` (prints the code once), `invites`, `revoke-invite <id>`.
   First run on a migrated database: `invite --owner`, sign up with it, then the owner sweep starts.
7. **.env.example:** `RECTIFI_MULTIUSER=1` needs `RECTIFI_SESSION_SECRET`.

## Decisions (plan or task unclear, safest option taken)

- **Status keys.** The plan says status "adds" three keys. I always add them. Three assertions in `tests/test_auth.py` compared
  the whole dict; I made them exact for the new shape (nothing loosened).
- **signup_token is `<sid>.<k>`.** `k` alone cannot find the pending row, and storing a hash of `k` needs a column. The row holds
  only the data key wrapped under `k`, so a dump still cannot confirm or open anything. Confirm deletes the pending row and
  makes a fresh session (new `sid`, new `k`) for the cookie; the sign-up token is spent.
- **Wrapped-key labels have no uid** (`dek|password`, `dek|recovery`, `privkey`, `session|<sid>`): each wrap already uses a
  different key, and the uid is not known before the INSERT.
- **Owner claim.** Credentials are written to row 1 at step one but `confirmed_at` stays NULL (login needs it), so it cannot sign
  in early. If nobody confirms in 30 minutes `purge_unconfirmed` clears the credentials (row 1 is never deleted) and frees the invite.
  `create_invite("owner")` refuses once row 1 has a password. The owner picks a username at sign-up (placeholder was `owner`).
- **Unknown, unconfirmed and wrong-password logins** are one path: one scrypt, same 401, same message, same pause and lock
  rules. Real accounts keep counters in `users.failures` / `locked_until` (survive restarts). Unknown names use an in-memory,
  bounded counter (lost on restart, which only helps an attacker probe names a little; no account is affected).
- **Global signup limit** is in memory: 10 attempts per hour, counting failed ones too, because every request comes from 127.0.0.1.
- **Wrong recovery codes** count against the account's login counters (10 in 15 min lock it). One account's lock never touches another.
- **Password change** signs out every other device (session rows deleted, epoch bumped) and re-issues this device's cookie.
  **Recovery** signs out everywhere and does not sign in.
- **hooks.after_sign_in** runs at confirm and at every multi-user login (not in laptop or single-password mode, to keep that
  behaviour exactly as before). It runs in a worker thread before the response is returned; blocks must be quick or use `spawn`.
- **D11.** `install_session_lock` calls `db.assert_app_role(strict=True)` in multi-user or public mode and lets
  `UnsafeDatabaseRole` propagate (so `create_app()` raises it, as `lifespan` already did). A database that is down at that
  moment is skipped; `init_db` checks again.
- **Session lifetime** 7 days (cookie and row). `last_seen_at` is touched at most every 5 minutes.
- **Usernames:** 3 to 32 of letters, digits, `.`, `-`, `_`; unique ignoring case. Passwords 12 to 1024 characters.
- **FRIEND_ROUTES** is an explicit (methods, pattern) list matched in full. `/api/transactions/{id}/apply-similar` is allowed
  (deterministic). Not allowed: `/api/classify/*`, `/api/insights/explain`, `/api/alerts/{id}/explain`, and every AI route.
  `tests/test_auth_multiuser.py` walks `api_routes()` so a new route must be classified as friend or owner-only.

## Not verified

- No browser run, no real two-device flow, no load. Scrypt (N=2^16) cost on the Oracle Arm core is unmeasured.
- Races: two logins for one account at the same instant can each add a failure before the other is saved (the row is locked
  with `FOR UPDATE` when counting, so counts do not get lost, but the pause decision is read before it).
- Hooks are the foundation stubs; nothing was exercised beyond "called with the right context".
- `request.state.signed_in` (the `users.SignedIn`, which holds the data key) is available to routes in multi-user mode; it is
  not logged anywhere.
