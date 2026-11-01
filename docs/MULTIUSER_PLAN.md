# Multi-user Rectifi: plan (stage 1 of docs/AGENT_WORKFLOW.md, updated with Prag's decisions of 2026-11-01)

Goal: a few friends each use Rectifi with their own bank SMS and money data, private from each other and, as far as possible, from Prag (the server owner), at ₹0.
Based on reading `ws/alerts` (530565d) and master (1fffe81). File:line references may drift; check them.

**This phase, friends get:** import, SMS capture, ledger, budgets, insights, alerts. **Owner only:** charts, AI chat, library, memory, traces, "Explain this", the model pass on imports.

## 1. What this will and won't protect

Can this person read this data?

| Who is looking | Amounts, dates, categories, account names, last 4 digits, caps, times, **well-known merchant names (§2 D3)** | SMS text, narrations, all other payees, notes |
|---|---|---|
| Another friend, through the app | No (app checks + RLS) | No (app checks + RLS + a different key) |
| Someone with a DB dump or stolen backup | **Yes, readable** | No, unless they crack the password (scrypt) |
| Prag browsing the database | **Yes, readable** | No |
| Prag changing the code, or reading server memory or the login request | Yes | **Yes.** Passwords and SMS pass through the server in clear |

RLS is a seatbelt against our own forgotten `WHERE`. It does not stop anyone who can run SQL as the database owner. Encryption is what limits the owner, and it only covers data at rest. The Privacy page says this plainly (decision 2): what the owner can see, what he can't, and how he could get around it.

## 2. Decisions

- **D1. Two modes.** `RECTIFI_MULTIUSER` unset (default): laptop mode, today's behaviour, uid 1, no encryption, Streamlit and MCP work. `RECTIFI_MULTIUSER=1`: invite sign-up, encryption required, Streamlit and MCP refuse to start with a plain message. RLS is on in both modes, so the whole suite exercises it.
- **D2. Username, no email. No reset path** (Prag). The recovery code is the only way back: no email, no admin reset, no `dev.py` command, and a test checks none exists. Code must be downloaded or confirmed before sign-up finishes (§4 API). Forgotten password + lost code = encrypted text gone for good.
- **D3. Plain merchant key, fixed allow-list, not opt-in** (Prag). `transactions.merchant_key` holds a brand from `rectifi/merchant_allowlist.py`, so alerts fire while signed out. All other payees stay encrypted. Database guard: FK to `allowed_merchants`, so nothing else can be written there. Never filled from `categorize.merchant_key()` (same word, different thing: that one returns any payee's key) and never from a user's own pattern.
  List: Swiggy, Instamart, Zomato, Blinkit, Zepto, BigBasket, JioMart, Domino's, EatSure, Amazon, Flipkart, Myntra, Ajio, Meesho, Nykaa, Uber, Ola, Rapido, IRCTC, BookMyShow, PVR, Netflix, Spotify, Hotstar. Nothing that reveals health, lending, investing, donations or a person.
- **D4. `last4`, account names and category/group/brand cap targets stay plain.** The signed-out gateway and sweep need them. A cap on a custom merchant is sealed and fires at next sign-in.
- **D5. Per-user copies of categories and merchant rules**, seeded at sign-up.
- **D6. Reach and hosting.** Friends can't join Prag's tailnet and the laptop isn't on all day, so this needs the Oracle VM plus a public HTTPS path (Funnel on 443 and 8443) for the app and the gateway. HOSTING.md says the gateway stays off on the server; that changes. Funnel's free status is unconfirmed.
- **D7. AI is owner-only, deny by default** (Prag). `SessionLock` lets a friend reach only an explicit `FRIEND_ROUTES` list; every other `/api` route answers 403, so a new route is closed until someone opens it. Friends' rows are never indexed, charted or sent to a model. Categorising for friends is layers 1 to 4 only.
- **D8. Owner's own AI tables are not encrypted this phase.** `conversations`, `charts`, `rag_*`, `trace_spans`, `checkpoints*`, `store` hold only Prag's data and contain no friend's data. The Privacy page says so. In multi-user mode the `txn` library corpus is not indexed, so ledger text isn't copied out of sealed columns; what Prag types in his own chat and what it answers does stay readable. Sealing these is part of the later phase that opens AI to friends.
- **D9. Owner claims user 1 by the same door.** `dev.py invite --owner` makes a one-time code; signing up with it sets a password on row 1 and starts encrypting the existing rows (resumable).
- **D10. No migration on the real DB without asking** (Prag). `init_db` auto-migrates only a database whose name ends in `_test` or `_demo`. Any other pre-tenancy database: start refuses with `TenancyMigrationNeeded` and a plain message, and nothing changes. `dev.py migrate-tenancy` is the only way, needs a typed `yes`, and refuses without a terminal. Agents never run it outside `_test`. After this merges, Prag's laptop app won't start until he says go.
- **D11. App role: non-superuser, no BYPASSRLS** (Prag). Both compose files use a separate `postgres` superuser (bootstrap and backups only) and `rectifi` as the app login. `db/init.sql` and `setup_windows.ps1` enforce and verify it. Multi-user or public start refuses a superuser or BYPASSRLS role; laptop mode warns via `doctor`.
- **D12. No new dependencies.** `cryptography` arrives with `ws/alerts`.

## 3. What the code showed

1. **Superusers skip RLS silently.** `deploy/compose.oracle.yml` and `docker-compose.yml` set `POSTGRES_USER: rectifi`, which makes that role a superuser. Native `db/init.sql` does not (the role on this PC is non-superuser, confirmed). So: `FORCE ROW LEVEL SECURITY`, D11, and `conftest.py` fails loudly on such a role. Backups use `postgres` because `pg_dump` errors on forced tables.
2. **Foreign keys skip RLS.** B could insert a row pointing at A's `account_id`, and FK errors reveal which ids exist. So: composite FKs `(user_id, account_id)`.
3. **Worker threads lose context.** `ask.py:231`, `routes/charts.py:151`, `routes/knowledge.py:81`, `agent/jobs.py:36` start bare threads; `alerts.py` adds background tasks. So: `userctx.spawn`, and a test bans bare threads.
4. **Few SQL sites need plaintext**: `tools.py:405` (ledger search), `categorize.py:165` (`ORDER BY length(pattern)`). Insights, similar-payment and recurring logic already work in Python. So decrypting in one row factory is enough. (Chat search and keyword search stay in SQL: owner-only and unsealed, D8.)
5. **Caches have no owner.** `categorize._index_cache`, `api/uploads` `pending` and `recent`, `merchants` patterns. All need `uid` in the key.
6. **Global state that assumes one person.** `LoginLimiter` (one friend's typos lock everyone out), `session_epoch`, `alerts.watermark`, `alerts.prefs`, the sweep and tick lock ids, all `app_settings` keys.
7. **Text hiding in odd places.** `category_reason` ("like #812 SWIGGY BLR"), `import_batches.filename`, `alerts.dedupe_key` (`streak:{merchant}:…`), `alerts.title/detail`, `sms_inbox.sender`.
8. **Later phase, not now:** LangGraph keeps `str` channels inline in `checkpoints.checkpoint` JSONB (`postgres/__init__.py:321`), `PostgresStore` has no serializer, and the chart child can read the parent's `/proc` environ. All matter when friends get AI, not while it is owner-only.

## 4. Contracts (foundation builds these first; everyone else codes against them)

**`rectifi/userctx.py`**
```python
@dataclass(frozen=True)
class UserContext: uid: int; key: bytes | None; public_key: bytes | None; ingest_salt: bytes | None; strict: bool
def current() -> UserContext      # laptop mode with nothing bound: uid 1. Multi-user with nothing bound: raises NoUser
def bound(ctx) -> ContextManager  # sets the ContextVar and always resets it
def spawn(target, *args, name) -> Thread   # copy_context().run; the only allowed way to start a thread under rectifi/
OWNER_UID = 1
```
`SessionLock` binds it per request. The gateway binds it from the device token (key None, strict False). The ticker binds it per user.

**`rectifi/db.py`**
- `connect()`: as today, plus `set_config('app.user_id', uid, true)` on entry, and a row factory that opens any `rx1.`/`rs1.` cell. No key: `strict` raises `VaultLocked` (API answers 401 "sign in again"), otherwise the cell becomes `None`.
- `connect_system(reason)`: sets `app.scope='system'`, reaches only `users`, `invites`, `user_sessions` and device lookup. A test allow-lists its callers: `users.py`, `tools.authenticate_device`, `alerts.tick`, `db.init_db`.
- `init_db()`: runs `schema.sql`, the guarded `_migrate_tenancy` (D10), then per-user seeding. `assert_app_role()` per D11.

**`rectifi/vault.py`** (crypto only, no DB, no langchain). Laptop mode: `seal` and `blind` return their input.
```
seal(text) -> "rx1.<b64 nonce|ct>"   AES-256-GCM under the data key, AAD "rectifi|rx1|<uid>"
           -> "rs1.<b64 eph|nonce|ct>"  if no data key: X25519 + HKDF-SHA256 + AES-256-GCM to the user's public key
seal_bytes, seal_json, open_value
blind(text)        -> "b1:<HMAC under HKDF(data key)>"   session only: fingerprint, pattern_blind, custom cap target_blind
blind_ingest(text) -> "b1:<HMAC under users.ingest_salt>" works signed out: upi_ref, text_hash, custom-merchant alert keys
```
**Keys.** Data key: 32 random bytes per user. `scrypt(password, salt, N=2^16, r=8, p=1, 64 bytes)` splits into a verifier (stored hashed) and a key-encrypting key, so login and unlock cost the same and neither is a cheap target. At most 2 scrypts at once. Recovery code: 125 random bits, shown once, wraps a second copy of the data key. X25519 private key is wrapped by the data key.
**Session.** Cookie gains `uid`, `sid`, and `k` (random 32 bytes). `user_sessions.dek_wrapped` holds the data key wrapped under `k`. A DB dump alone can't unlock a session, a restart doesn't sign anyone out, and logout deletes the row. The server has the data key only during a request or a job that request started.

**`rectifi/sealed_fields.py`**: the registry of every sealed column. The Privacy page test, the dump test, the owner sweep and the DB trigger all read it.
**`rectifi/merchant_allowlist.py`** (stdlib only): `ALLOWED` (key, display name, aliases such as `amzn`, `grofers`, `bundl tech`, `kiranakart`, `bbnow`, `olacabs`) and `plain_key(payee, description) -> str | None`. Patterns of 4 letters or fewer match at the start of a word (`ola`, not `cola`). `init_db` seeds `allowed_merchants` from it.

**Schema block** (`schema.sql`, safe to run again)
```sql
CREATE OR REPLACE FUNCTION app_uid() RETURNS int LANGUAGE sql STABLE AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::int $$;
-- every tenant table: user_id INT NOT NULL DEFAULT app_uid() REFERENCES users(id) ON DELETE CASCADE
-- ENABLE + FORCE ROW LEVEL SECURITY; POLICY USING (user_id = app_uid()) WITH CHECK (user_id = app_uid())
-- parents add UNIQUE (user_id, id); children use FOREIGN KEY (user_id, account_id) REFERENCES accounts (user_id, id) ON DELETE SET NULL (account_id)
CREATE TABLE users (id SERIAL PRIMARY KEY, username TEXT, scrypt_salt BYTEA, verifier BYTEA, dek_by_password BYTEA, dek_by_recovery BYTEA, recovery_salt BYTEA,
  public_key BYTEA, private_key_wrapped BYTEA, ingest_salt BYTEA, session_epoch INT NOT NULL DEFAULT 0, encrypted BOOLEAN NOT NULL DEFAULT false, confirmed_at TIMESTAMPTZ,
  sweep_state TEXT, failures JSONB NOT NULL DEFAULT '[]', locked_until TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE UNIQUE INDEX uq_username ON users (lower(username));   -- row 1 is 'owner' with no credentials until claimed
CREATE TABLE invites (id SERIAL PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('friend','owner')), expires_at TIMESTAMPTZ NOT NULL, reserved_by INT REFERENCES users(id) ON DELETE SET NULL, used_at TIMESTAMPTZ);
CREATE TABLE user_sessions (id UUID PRIMARY KEY, user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE, dek_wrapped BYTEA, pending BOOLEAN NOT NULL DEFAULT false, expires_at TIMESTAMPTZ NOT NULL, last_seen_at TIMESTAMPTZ);
CREATE TABLE allowed_merchants (key TEXT PRIMARY KEY, name TEXT NOT NULL);   -- global, public list, no user data
ALTER TABLE transactions ADD COLUMN upi_ref_sealed TEXT, ADD COLUMN merchant_key TEXT REFERENCES allowed_merchants(key), ADD COLUMN unlocked BOOLEAN NOT NULL DEFAULT true;  -- false: arrived signed out
```
`DEFAULT app_uid()` means the many existing INSERTs need no change, and no user means a NOT NULL error, so it fails closed. `enforce_sealed()`, a BEFORE INSERT/UPDATE trigger on each table with sealed columns, refuses a non-envelope value when that user has `encrypted = true`. Like dedupe (rule 3), the database is the real guard: a forgotten `seal()` becomes an error, not a leak.

**Every table**

| Table | Unique key becomes | Sealed columns (rest stay plain) |
|---|---|---|
| accounts | (user_id, name) | none (D4) |
| categories | (user_id, name) | none |
| transactions | (user_id, upi_ref, direction), (user_id, fingerprint); values become `b1:` | payee, description, raw_text, note, category_reason, upi_ref_sealed. `merchant_key` plain (D3) |
| budgets, balance_snapshots | PK unchanged (parent id is already one user's) | none |
| merchant_rules | (user_id, pattern_blind) | pattern when learned; shipped defaults stay plain |
| weekly_reviews | (user_id, week_start) | commitment |
| phone_devices | token_hash stays global (token finds the user) | name |
| sms_inbox | (user_id, text_hash) | sender, raw_text, reason |
| sms_seen | (user_id, text_hash) | none (keyed hash only) |
| import_batches | none | filename |
| app_settings | (user_id, key); global markers move to `users` or a global row | prefs money fields stay plain |
| merchant_patterns | (user_id, pattern_blind) | pattern, merchant |
| alert_rules | (user_id, scope, target_blind, period); `target_blind` = target, or `blind(target)` for a custom merchant | target for a custom merchant only |
| alerts | (user_id, dedupe_key); custom merchant part via `blind_ingest`, brand may be plain | title, detail. `push_title/body` plain, limited to category, amount and allow-listed brand; otherwise generic text |
| push_subscriptions | (user_id, endpoint) | label; endpoint and keys stay plain (needed signed out, disclosed) |
| classify_jobs, charts, conversations, rag_documents, rag_chunks, trace_spans | PK or (user_id, …) as needed | none this phase (D8). Same `user_id` + RLS, all rows `user_id = 1` |
| checkpoints, checkpoint_blobs, checkpoint_writes, store, *_migrations | library's own | none (D8). No `user_id`; allow-listed in the RLS test as "owner-only by route gate", with a test that a friend's request never opens them |
| users, invites, user_sessions, allowed_merchants | see above | wrapped keys only; first three reached through `connect_system`; `allowed_merchants` is public |

**API** (money rules, guards, `X-Rectifi-Request` unchanged)
```
POST /api/auth/signup {invite, username, password}   201 {signup_token, recovery_code}. NO session yet. Invite reserved, user unconfirmed.
POST /api/auth/signup/confirm {signup_token}         Sets confirmed_at, uses invite, sets the cookie. Unconfirmed users can't sign in; after 30 min the row is deleted, username and invite free again.
POST /api/auth/login  {username, password}           multi-user; laptop/single-password form {password} still works
POST /api/auth/recover {username, recovery_code, new_password}   {recovery_code}  new code, signs out everywhere
POST /api/auth/change-password {current, new}   POST /api/auth/recovery-code {password}
GET  /api/auth/status   adds {multiuser, username, is_owner}, so the browser can hide owner-only pages (the server still enforces)
GET  /api/account/export  ZIP of CSVs. Money as text; cells starting = + - @ are prefixed with '
DELETE /api/account {password, confirm_username}     the owner can't delete row 1 while friends exist
GET  /api/privacy  public: facts (mode, provider local or cloud, backup days, sealed field names, allow-list), no user data
```
`signup_token` is the `k` of a `pending` session row, so the data key isn't readable from the database between the two calls. The Finish button stays disabled until the code is downloaded or a "I saved it" box is ticked.
`dev.py invite [--days 7] [--owner]` prints the code once and stores only its hash. `dev.py invites` lists invites without codes. `dev.py revoke-invite` cancels one.

**Limiter.** Per account in `users.failures`, plus a global signup limit. Unknown usernames take the same time (dummy scrypt) and get the same message.

**After sign-in** (`rectifi/hooks.py`, foundation stub, one `# ═══ WS-X` block per stream, imports inside functions so `rectifi.api` still loads no langchain): open `rs1` values into `rx1` and re-run layers 1 to 4 on `unlocked = false` rows (never touching `category_source='user'`), catch up alerts on custom merchants. Owner only: requeue classify jobs, guides reindex.

**Sealed SMS flow.** Token finds user. Parse in memory (rules only, no model). Amount, date, direction, `merchant_key`, category from built-in rules and heuristics are stored plain; `rs1` for the texts; `unlocked=false`. Reply 200 as today. Learned rules and the look-alike index wait for next sign-in. Rule 8's status codes are unchanged.

## 5. Stage 2: FOUNDATION (one agent)

Owns: `rectifi/schema.sql`, `db.py`, `config.py` (`multiuser()`), new `userctx.py`, `vault.py`, `sealed_fields.py`, `merchant_allowlist.py`, `hooks.py`, `tests/conftest.py`, `tests/support/olddb.py`, new `tests/support/tenants.py` (`as_user(uid)`, two seeded users with keys), `tests/test_schema_additive.py`, `tests/test_categories_migration.py`, new `tests/test_tenancy_migration.py`, `test_rls.py`, `test_vault.py`, `test_merchant_allowlist.py`, `test_app_role.py`.
App role (D11): `db/init.sql`, `docker-compose.yml`, `deploy/compose.oracle.yml` (+ a small db init script for the app role's password), `scripts/setup_windows.ps1`, their tests.
Migration command (D10): `scripts/devtools/users.py` with `migrate-tenancy`, stubs for `invite`, `invites`, `revoke-invite`; the start refusal in `api/app.py`, `sms_gateway.py`, `app/streamlit_app.py`, `dev.py doctor`.
Also, once only: updates `ON CONFLICT` targets (`tools.py`, `importer.py`, `rag/store.py`, `db.py`, `api/auth.py`, `alerts.py`), adds `options=-c app.user_id=1` to the 3 raw-connect test files, and adds markers: `tools.py` (WS-AI: classify, charts, conversations, traces, rag; the rest is WS-CORE), `api/app.py` (WS-ALERTS ticker, WS-ACCOUNT routes), `hooks.py`.

**Migration** (`_migrate_tenancy`, one transaction, marker `tenancy_v1`): counts every table, creates user 1, backfills `user_id = 1`, swaps indexes and FKs, turns RLS on, re-counts, raises and rolls back on any difference. No crypto here: encrypting needs the password (WS-ACCOUNT sweep).

**Acceptance**
- `test_tenancy_migration`: an old-schema DB holding a row in every table upgrades; all rows are `user_id=1`; every money sum and row count matches; the second run changes nothing; a forced failure leaves the old schema intact. **Guard:** a pre-tenancy DB with a non-`_test`, non-`_demo` name refuses, and is byte-for-byte unchanged (name check monkeypatched, run on the test DB); `migrate-tenancy` refuses without a typed `yes` and without a terminal.
- `test_rls`: walks `pg_tables`. Every public table has `user_id` + FORCE + a policy, or is on the short allow-list. As B, a bare `SELECT *` returns no A rows. With no user: zero rows, and INSERT errors. B can't insert a row pointing at A's account. GUC is empty after commit and after rollback.
- `test_app_role`: connects with the app's URL. `rolsuper` and `rolbypassrls` are false **and** RLS really applies: owner-role connection without the GUC sees zero rows of a table with rows. `assert_app_role` refuses a fake superuser in multi-user mode. Static checks: both compose files have `POSTGRES_USER: postgres`, the app URL uses `rectifi`, no BYPASSRLS anywhere; `setup_windows.ps1` verifies it, stays ASCII and PowerShell 5.1.
- `test_vault`: round trips; wrong uid fails; tampering fails; `rs1` opens only with the private key; `blind` is stable and per-user; forged envelope is left as text.
- `test_merchant_allowlist`: fixtures for each brand and alias; negatives (`COLA HOUSE`, `AMAZING TAILORS`, a person's UPI handle) give None; writing a non-listed value to `merchant_key` fails in the database; no health, lending or investing brand on the list.
- Trigger refuses plain text for an encrypted user, allows it for a laptop user. `conftest` fails on a superuser role.
- **Full suite green in laptop mode with RLS on.** This is the gate for fan-out. Pre-existing failures are recorded from a baseline run first.

Merge to master only after `ws/alerts` is on master.

## 6. Stage 3: BUILD (parallel, one worktree each, test DB `rectifi_<ws>_test`)

No stream touches a file owned by another. Needs from elsewhere go to `docs/handoffs/integrator-requests.md`.

| Stream | Owns (edits nothing else) | Job |
|---|---|---|
| **WS-AUTH** | new `rectifi/users.py`, `api/auth.py`, `api/routes/auth.py`, `api/security.py` (WS-HOST block), `scripts/devtools/users.py`, `tests/test_auth.py`, new `tests/test_users.py` | Two-step sign-up, invites, login, sessions, password change, recovery, per-account limiter, binding `UserContext`, `request.state.uid` = real id, startup refusals, `FRIEND_ROUTES` deny-by-default gate (D7) |
| **WS-CORE** | `tools.py` (unmarked parts), `importer.py`, `insights.py`, `categorize.py`, `sms_gateway.py`, `api/uploads.py`, `api/routes/{transactions,imports,budgets,accounts,phone,overview,review,insights,meta}.py`, their tests | Seal all writes, set `merchant_key`, Python ledger search (newest 20,000 rows), `b1:` dedupe, uid in caches and uploads, sealed SMS flow, re-categorise after unlock, no model job for a friend's import |
| **WS-AI** | `agent/jobs.py`, `agent/classify.py`, `api/ask.py`, `rag/index.py`, `api/routes/{ask,chat,charts,knowledge,classify,memory,traces,models}.py`, `tools.py` WS-AI block, their tests | Small on purpose (D7, D8): threads via `spawn`, jobs carry `UserContext`, restart requeue moves to owner sign-in, no `txn` corpus in multi-user mode, Explain routes owner-only. Works in multi-user mode for the owner with sealed transactions |
| **WS-ALERTS** | `alerts.py`, `merchants.py`, `webpush.py`, `api/routes/alerts.py`, `api/app.py` (ticker block), `tests/test_{alerts,api_alerts,gateway_alerts,merchants,webpush}*.py` | Per-user prefs, watermark, two-key advisory lock; tick loops users; `merchant_key` drives signed-out caps and streaks; registry covers every allow-listed brand; custom merchants at sign-in; sealed alert text; one user's alert never reaches another's phone |
| **WS-ACCOUNT** | new `rectifi/account.py`, `rectifi/seal_sweep.py`, `api/routes/account.py`, `api/app.py` (routes block), new `docs/PRIVACY.md`, new `tests/test_account.py`, `test_seal_sweep.py` | Export, delete, `/api/privacy`, resumable owner sweep (from registry, also backfills `merchant_key`), `rs1`→`rx1` drain, re-blinding old `upi_ref` and fingerprints |
| **WS-DEPLOY** | `deploy/*` (except the role work above), `docs/HOSTING.md`, `.env.example`, `.env.deploy.example`, `scripts/devtools/deploy.py`, `rectifi/mcp_server.py` + `app/streamlit_app.py` (mode refusal only), `tests/test_deploy_files.py`, `tests/test_mcp.py` | Gateway + Funnel steps, 7-day backup note, invite runbook, `VACUUM FULL` + delete old backups after the owner sweep |
| **WS-WEB** | `web/**` including `web/dist` | `Signup` (code screen: download or tick before Finish), `Recover`, Login with username, `Settings` (password, recovery, export, delete), public `Privacy` per decision 2; owner-only pages hidden for friends; 390/1280, light/dark, 44 px; codes in component state only |
| **WS-PROOF** | new `tests/test_isolation_routes.py`, `tests/test_dump_no_plaintext.py`, `tests/test_multiuser_e2e.py` | Written from this plan by an agent that read none of the other branches; expected red until the merges land |

**Acceptance per stream**
- **AUTH:** no session or data access before confirm; an unconfirmed user can't sign in and is cleared after 30 min, freeing username and invite; a code works once and expires; a wrong or reused code gets one message; cookie tamper, wrong epoch, deleted session and other-user's `uid` all give 401; one user's lockout leaves others alone; recovery restores decryption and the old code stops working; password change keeps data readable; a scan of `scripts/` and routes finds no reset without password or code; friend gets 403 on every owner-only route, including a route added by the test; laptop and single-password tests still pass; nothing secret in `caplog`.
- **CORE:** with `encrypted=true`, every sealed field is an envelope in the raw table and reads back through `tools`; `merchant_key` set for brand payees, NULL for the rest; same SMS for A and B makes one row each; retried SMS stays one row; SMS arriving signed out then sign-in gives the same row as a signed-in one, with a `user` category untouched; A's token can't write as B; rule 14 snapshot test still passes; no langchain in gateway, importer or insights; a friend's import starts no model job.
- **AI:** owner's chat, chart, library and classify flows work end to end in multi-user mode with sealed transactions; `rag_chunks` gets no `txn` rows there; a friend's rows are never returned by anything here; laptop mode unchanged.
- **ALERTS:** two users' sweeps don't block each other or share a watermark; signed-out SMS to a brand fires its merchant cap and streak; signed-out SMS to any other payee fires only category and total caps, with no payee text in the row or the push; custom merchant cap fires at next sign-in; a 410 deletes only that user's subscription.
- **ACCOUNT:** export has exactly A's rows, decrypted, with money equal to `tools` totals and formula cells defused; delete leaves zero rows for that uid in every table (walks `pg_tables`), others untouched, cookie dead; sweep stopped halfway resumes and ends with no plain value; sweep twice changes nothing; old statement re-imported after sweep still finds duplicates.
- **DEPLOY:** Streamlit and MCP refuse in multi-user mode and run in laptop mode; no port on 0.0.0.0; backup runs as `postgres`.
- **WEB:** vitest for new pages; e2e sign-up → Finish blocked until download or tick → sign out → recover → data readable; no code or password in the URL, storage or query cache; friend sees no Ask, Charts, Library, Memory or Traces; `web/dist` rebuilt; Privacy page text passes the registry test.

## 7. Order of merges (stage 4, one integrator)

Before any of it: `ws/alerts` finished and green on master. Prag has backed up his real DB; no agent touches it.

1. FOUNDATION (on master; full suite green)
2. WS-AUTH: real contexts exist from here
3. WS-CORE
4. WS-AI
5. WS-ALERTS
6. WS-ACCOUNT: needs CORE's sealed writes to sweep them
7. WS-DEPLOY
8. WS-WEB: rebuild `web/dist` last if anything under `web/` changed
9. WS-PROOF: goes green here. Any red is a real bug, fixed in the owning stream's file, never by loosening the test.

Integrator then applies integrator requests, updates CLAUDE.md (§9), runs real routes end to end in `RECTIFI_MULTIUSER=1`, the full suite, `build-web`, screenshots. Then stage 5 (Opus review, with auth bypass, RLS gaps and key handling first) and the fix loop.

## 8. Whole-feature acceptance (WS-PROOF)

- **`test_isolation_routes`:** seeds A through the API with a canary in every text field. A meta-test reads `api_routes()` and fails if a route has no case or isn't on a short public list, so new routes can't dodge it. For each route as B, with A's real ids: status is 404, 403 or empty; the body holds no A canary, id or amount. After all of B's writes, a checksum of every A table is unchanged. Each non-public route with no cookie gives 401. Owner-only routes give B 403. Also covers: the upload id from A used by B, gateway with B's token, `alerts.tick` not crossing subscriptions.
- **`test_dump_no_plaintext`:** runs real flows for a friend with canaries (signed-out SMS, signed-in SMS, needs-review SMS, statement import, note, alert), plus the same for the owner after the sweep. Then scans a dump of every table, including BYTEA and JSONB, plus `caplog`. Uses `pg_dump --no-compression` when found, else `COPY`. The test temporarily sets `NO FORCE` on its `_test` DB, because a forced owner can't dump. Fails on any canary, including hex and base64 forms. Allow-listed brand names are expected in `merchant_key` only. Sanity check: the same run in laptop mode **must** find them, proving the scan works.
- **`test_multiuser_e2e`:** Playwright, two browser contexts, A and B side by side, reading the DB to prove it. Owner claim with existing data: user 1's totals and reconciliation are the same before, during and after the sweep.
- **Honesty test:** every field in `sealed_fields.py`, every allow-listed brand and every plain item in §1 appears on the Privacy page; `/api/privacy` agrees with config.

## 9. CLAUDE.md changes (integrator only)

New rule 18 (tenancy: `user_id`, FORCE RLS, composite FKs, app role, no bare threads, `connect_system` allow-list, migration consent; a new table must join the matrix or the test fails). New rule 19 (sealed fields: registry, trigger, allow-list, nothing sealed is used in SQL, no key means no text). New rule 20 (`FRIEND_ROUTES` deny by default; AI stays owner-only until a plan says otherwise). Rule 8: the gateway may run on the server behind Funnel in multi-user mode. Rule 11 addendum: `uid` is an int; `RECTIFI_MULTIUSER`. Rule 13: Streamlit and MCP are laptop-mode only. Map rows for the new modules.

## 10. Not verified

- **Every design claim here until foundation proves it:** RLS with `FORCE` under the `rectifi` role on Postgres 16 on Windows (role attributes confirmed, behaviour not); cascading deletes under RLS; `ON DELETE SET NULL (col)`; `pg_dump` against forced tables; `SET LOCAL` per transaction cost across the suite; running the RLS block on every `fresh_db`.
- **Real data.** The migration has never met Prag's real database. Agents won't run it (D10). He runs it after being asked, and reports counts.
- **Compose changes.** No Docker on this laptop, so the compose files get static checks only. An existing Docker volume keeps its old superuser role; `assert_app_role` catches that.
- **Oracle VM, Funnel, public gateway.** All of `deploy/` has never run on a real VM. Funnel's free status, its ports for a second service, and Host header behaviour are unconfirmed. If Funnel doesn't work the fallback is a free tunnel, and this section changes. Getting an Always Free VM can fail for lack of capacity.
- **Real SMS forwarder on friends' phones**, different banks' wording, Android battery savers. No real phone has ever posted to the gateway.
- **Real UPI handles** for the allow-list. Written from `ws/alerts`'s registry and general knowledge. A person named Ola would be tagged `ola`; a brand with an unusual handle stays NULL and just loses signed-out alerts.
- **Speed.** `N=2^16` scrypt on an Oracle Arm core; AES-GCM across a ledger search.
- **Text in Python memory** can't be wiped. Decrypted values and keys live until garbage collection, possibly in swap or a crash dump.
- **Old copies.** After the owner sweep, plain text can survive in old backups (including the one Prag just took), dead rows and WAL until removed. Friends' data never has this gap. Deleted accounts stay in backups up to 7 days (encrypted text, plain amounts).
- **Crypto review.** Standard primitives from `cryptography`, but the composition is ours and no outside expert will look at it. It's a pet project; the Privacy page must not claim more.
- **Legal.** Holding friends' bank data has legal duties in India (for example, the DPDP Act). This plan does not assess them. Tell friends plainly, and ask someone who knows if this grows beyond a few people.
