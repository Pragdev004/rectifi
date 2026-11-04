# WS-ALERTS handoff (branch ws/mu-alerts)

Built from `docs/MULTIUSER_PLAN.md` section 6. Tests: `tests/test_alerts_mu.py` (19) plus the existing alert, gateway, merchant
and web push suites, unchanged and green.

## What changed

- **`alerts.py`**
  - Advisory locks are two-key `pg_try_advisory_xact_lock(kind, uid)` (sweep and tick), so two people never wait for each other.
  - `tick()`: laptop mode is the old one-person tick (`_tick_one`). Multi-user mode lists people with `people()` (one
    `connect_system` call, the allow-listed caller `alerts.py`), binds each with no data key (public key and ingest salt only,
    `strict=False`) and runs `_tick_one` for them. One person's error is logged (type only) and the next still runs. A person with no
    public key yet (the owner before claiming) is skipped: nothing can be sealed for them.
  - Watermarks and prefs were already per person. They are made lazily at that person's first sweep (`sweep` -> `ensure_watermark`).
  - `build_context` selects `merchant_key`. A row whose payee is readable is matched by text as before; a row whose payee is sealed
    (arrived signed out) gets its merchant from the plain `merchant_key` (`merchants.by_key`). A rule whose target reads `None`
    (sealed, no key) is skipped.
  - A custom merchant (any merchant name not on the allow-list) has its cap target sealed with `target_blind = vault.blind(target)`
    (checked in `create_rule`, since the DB trigger does not guard `alert_rules.target`). Brands, groups, categories and "all" stay plain.
  - `dedupe_key` and `scope_key` for a custom merchant use `vault.blind_ingest(name)` (works signed out); a brand keeps its name.
    `scope_key` also lands in `muted_scopes` / `snoozed_scopes` in prefs, so the blinded form keeps names out of there too.
  - `alerts.title` and `detail` are sealed in `_insert` (signed out: `rs1.`; at sign-in they become `rx1.`). `push_title` and `push_body`
    stay plain; for a custom merchant they are generic ("A merchant cap: 80% of weekly cap", "5th order from one merchant this week").
  - `_push_all` filters subscriptions by the bound `user_id` (RLS already does) and deletes / updates only that person's rows on 410.
    `push_subscriptions.label` is sealed.
  - `kick()` uses `userctx.spawn`. `alerts.py` is out of `KNOWN_BARE_THREADS` (the list got shorter).
  - New: `catch_up()` (caps and streaks on custom merchants, at sign-in, needs the data key), `reseal_signed_out()`, `after_sign_in()`.
- **`merchants.py`**: `BRANDS` (key -> name, group) covers every `merchant_allowlist.ALLOWED` brand; the registry gained Uber, Ola, Rapido,
  IRCTC, BookMyShow, PVR, Netflix, Spotify, Hotstar (group "Other"); `is_plain`, `by_key`. Taught patterns are sealed in `add_pattern`
  (`pattern`, `merchant`, `pattern_blind`) and `user_patterns` skips a pattern that reads `None`.
- **`hooks.py` WS-ALERTS block** calls `alerts.after_sign_in()`. **`api/app.py` WS-ALERTS blocks**: the lifespan no longer calls
  `ensure_watermark` in multi-user mode (it raised `NoUser`); the ticker is unchanged and `tick` binds people itself.
- `webpush.py` and `api/routes/alerts.py` needed no change (the routes run under `SessionLock`; the code below them is per person).

## Requests for files I do not own

- None blocking.
- WS-ACCOUNT: the sweep (`rs1`/plain to `rx1`) should include `alerts.title`/`detail`, `alert_rules.target` (+ `target_blind`) for custom
  merchants only (use `merchants.is_plain` and `alerts._is_plain`), and `push_subscriptions.label`. `alerts.reseal_signed_out()` already
  re-seals `rs1` alert texts at sign-in; it does not seal old plain rows. Old `dedupe_key` / `scope_key` values that hold a custom merchant
  name (`streak:<name>:...`, `cap:merchant:<name>:...`) are not rewritten by anything: the sweep should either re-key them with
  `blind_ingest` or delete those alerts (they are inbox history). Prefs `muted_scopes` / `snoozed_scopes` can hold such names too.
- WS-DEPLOY: nothing. The ticker needs one uvicorn worker, as before.

## Decisions

1. **Flipkart Minutes is a custom merchant** (not on the allow-list: its brand key is `flipkart`). A cap on "Flipkart Minutes" is sealed and
   fires only signed in. Signed out, those payments count as Flipkart.
2. **Instamart's name stays "Swiggy Instamart"** (registry name) for the allow-list key `instamart`, so existing caps keep working.
3. **Uber, Ola, Rapido, IRCTC, BookMyShow, PVR, Netflix, Spotify, Hotstar are group "Other"** (the DB check on `merchant_patterns.grp`
   allows only four groups). Short names (four letters or fewer) match as whole words in the registry, like the allow-list.
4. **Catch-up covers the current day, week and month only**, not past periods (a cap crossed last week while signed out stays unfired).
   It takes a blocking per-person advisory lock so it never loses to a running sweep.
5. **The sign-in hook does not push.** It writes alerts (with the normal gates); the next one-minute tick delivers them. No network at sign-in.
6. **People with no public key are not ticked** (owner before claim). Their gateway sweeps still run with the device context.
7. **Multi-user `tick()` returns an aggregate** (`people`, `failed`, `sweep.new`, `released`, `digest`, `retried`); laptop mode returns the old shape.
8. **A digest or bulk alert** carries only counts and totals, so signed out it needs no change beyond sealing the texts.

## Not verified

- No real phone, no real push service (the 410 test uses `httpx.MockTransport`; the rest uses a recording stand-in for `webpush.send`).
- The API routes were not driven with a real multi-user cookie; `alerts.py` is exercised directly with the person bound.
- A person with thousands of rows: `build_context` reads six months of rows per tick per person (as before, now times the number of people).
