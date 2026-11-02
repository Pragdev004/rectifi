# WS-PROOF handoff (integrator)

Files: `tests/test_isolation_routes.py`, `tests/test_dump_no_plaintext.py`, `tests/test_multiuser_e2e.py`, helpers in
`tests/support/proof.py`. Written from `docs/MULTIUSER_PLAN.md` only. On the foundation branch most of them fail at "dev.py invite
is not built yet" (expected); the meta-tests, the scanner test, the laptop-mode sanity test and the browser Privacy test run.

When something is red at integration, the fix belongs in the owning stream, never in a looser assertion. When the red is one
of the assumptions below, change the helper named in brackets and nothing else.

## Assumptions (each is one helper in `tests/support/proof.py` unless noted)

| # | Assumption | Where |
|---|---|---|
| A1 | The sign-up and login limiters are in memory in `rectifi.api.auth` or `rectifi.users` and have `reset_memory` / `reset_limits` / `reset_limiter`. Without one, nothing is reset (the tests sign up fewer than 10 people per test). | `reset_limiters` |
| A2 | `python scripts/dev.py invite [--days N] [--owner]` prints the code once on stdout; the best-looking token (label "code", digits, dashes, capitals, length 8+) is the code. | `_parse_code`, `make_invite` |
| A3 | `POST /api/auth/signup {invite, username, password}` answers 201 with top-level `signup_token` and `recovery_code`; `POST /api/auth/signup/confirm {signup_token}` answers 200 or 201 and sets the cookie; `POST /api/auth/login {username, password}` 200; `POST /api/auth/logout` 200 or 204 and kills the session row (next call is 401). | `sign_up`, `Person.login/logout` |
| A4 | Ids come back as `body["id"]` or `body["<account\|transaction\|batch\|device>"]["id"]`; lists are a bare list or `{"items": [...]}`. Import outcome: `{"outcome": "imported"\|"preview", "batch": {"id"}, "upload_id", "mapping"}` as on master. | `first_id`, `items_of` |
| A5 | The owner sweep starts by itself after the owner claim is confirmed, and is finished when user 1 has no plain value left in any `sealed_fields` column (the `when` filter honoured). State names such as `users.sweep_state` are not read. | `plain_sealed_cells`, `wait_for_sweep` |
| A6 | `alerts._clock` still exists and `tests/alerts_support.Clock` can replace it; `tools.today` is the date source. | `pin_clock` |
| A7 | `alerts.tick()` called with no person bound loops over every user itself (plan section 6, WS-ALERTS). A push is `webpush.send(sub, payload, ...)` and `sub` has `endpoint`; `send` answering `"gone"` deletes that subscription (true on master). | `raise_one_alert`, `PushRecorder` |
| A8 | Row level security reads `app.user_id` from the session, so `psycopg.connect(url, options="-c app.user_id=N")` is how a test sees exactly one person's raw rows (foundation does the same in its raw-connect tests). | `raw_conn` |
| A9 | Pages: `/ledger` lists a payee's text, `/` has `data-testid` `hero-income`, `hero-spending`, `hero-saved`, `recon-slip[data-balanced=true]`, `/privacy` is public, a signed-out visitor to `/ledger` ends at `/login` (or sees a password box), owner-only pages are `a[href]` to `/ask /charts /library /memory /traces` and are absent for a friend. | `test_multiuser_e2e.py` |
| A10 | Owner-only (friend gets exactly 403, even for a status route): everything under `/api/ask`, `/chat`, `/charts`, `/memory`, `/traces`, `/models`, `/knowledge`, `/classify`, plus `POST /api/insights/explain`, `POST /api/alerts/{id}/explain`, `POST /api/transactions/{id}/apply-similar`. `GET /api/imports/{id}/rows` is treated as friend-reachable (403 or 404 for another person's batch both pass). | `CASES` in `test_isolation_routes.py` |
| A11 | Routes from the plan: `GET /api/account/export` (ZIP of CSVs), `DELETE /api/account {password, confirm_username}`, `POST /api/auth/change-password {current, new}` (answers 200 or 204, then B signs in again with `new`), `POST /api/auth/recovery-code {password}`, `POST /api/auth/logout-all`, `GET /api/privacy`. | `CASES`, `PUBLIC` |
| A12 | `/api/privacy` JSON shape is open. The honesty test flattens every key and value, lower-cases and drops punctuation, and looks for each sealed column name, each brand display name, each plain item from plan section 1 (stems: amount, date, categor, accountname, last4, cap, time, merchant) and the word "owner". Provider: any key containing `provider` or `model` holds `local` or `cloud`. Mode: any key containing `mode` or `multi` holds `true` or a word containing `multi` (laptop mode must not say `true`). | `flatten`, `haystack`, `norm` |
| A13 | A signed-out gateway SMS is stored as an `rs1.` envelope; sign-in opens them (waited for up to 15 s) into `rx1.`. Every column in `sealed_fields.SEALED` (tables without a `when` filter and with `enforce=True`) holds an envelope or NULL for an encrypted person; blind columns start `b1:`. | `check`, `count_envelopes` in `test_dump_no_plaintext.py` |
| A14 | `merchant_key` for a payment to `swiggy@icici` is `swiggy` (a Swiggy cap fires while signed out), and NULL for any other payee; every stored value is on the allow-list. Brand names of 5+ letters must not appear inside any sealed cell (3 and 4 letter brands would match base64 by chance). | `brand_words` |
| A15 | `HDFC`-style CSV with `Account No : XXXXXXXX4417` imports at once into the person's account whose last 4 digits are 4417, or answers a preview that `POST /api/imports/commit {upload_id, account_id, mapping}` accepts. | `statement_csv`, `commit_preview` |
| A16 | `tools.reconcile("SBI Savings")` and `tools.spending_summary(...)` run for user 1 under `UserContext(uid=1, strict=False)` and give equal results before, during and after the sweep (they use amounts only, never sealed text). | `figures()` in the e2e file |

## Choices to know about

- ids: A's rows start at 7,000,000 and B's at 8,000,000 (sequences are moved with `setval`), so an id in a response is unmistakable.
  Amounts are distinct per person (`7422.59` for A, `7433.66` for B ...). Canaries are `zqx<tag><label><6 hex>`, lower-case letters and digits.
- The dump: `pg_dump --data-only` (plain format is uncompressed, so no compression flag is needed; PostgreSQL 16 has no `--no-compression`) when
  found (PATH or `C:\Program Files\PostgreSQL\*\bin`), plus `COPY` per table for the cell-level checks. `FORCE ROW LEVEL SECURITY` is switched off
  on every table of the `_test` database for the read and put back in a `finally` (refuses to run unless the database name ends in `_test`).
- Isolation test: one big test loops over every case and reports all problems at once; it checks A's table checksums before and after every
  single call, so a route that writes into A's rows is named.
- `test_every_case_names_a_real_route` goes red until WS-ACCOUNT and WS-AUTH add their routes. That is intended.
- Not covered here (owned elsewhere): cookie tampering, lockouts, recovery flow (WS-AUTH), export content (WS-ACCOUNT), friend-sees-no-owner-pages
  screens (WS-WEB). The e2e file only checks the anchors and the API gate.
