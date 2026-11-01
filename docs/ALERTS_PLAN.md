# Leak alerts / real-time budget alerts — plan (Opus)

Branch `ws/alerts` from master AFTER the integrator merge lands green.

## Key decisions
1. Push stack: pin `http-ece==1.2.1` + `cryptography==<installed>` (direct dep). Own VAPID ES256 JWT with `cryptography` (~30 lines), send with `httpx` (pinned). Fallback if RFC vector fails: `pywebpush==2.5.0` (MPL-2.0, unmodified use OK).
2. Watermark sweep: `alerts.sweep()` evaluates debits with `id > app_settings['alerts.watermark']`, under `pg_try_advisory_xact_lock` (gateway :8765 and API :8501 are separate processes). Alert rows have `dedupe_key UNIQUE`, written `ON CONFLICT DO NOTHING`. Real-time paths call sweep() right away; a 60 s ticker catches the rest. Do NOT hook inside `add_transaction` (it runs inside caller's txn).
3. Bulk mode: if a sweep found > BULK_MIN (5) debits, or any has `import_batch_id`, or any `txn_date` < today−2 → one summary alert (`import:{batch_id}` or `bulk:{min_id}-{max_id}`) + one push; crossed cap alerts still written with `push_state='skipped'` so a later SMS doesn't re-fire.
4. New `rectifi/alerts.py`, sibling of insights.py: fixed rules, SQL, Decimal; no rectifi.agent/langchain. CLAUDE.md rule 14 (worded like rule 10). API wraps it like insights.
5. No model on the push path ever. LLM only in `POST /api/alerts/{id}/explain` reusing `agent/explain.explain` via an Insight built from the alert.
6. Use local naive time like `tools.today()` (no zoneinfo — Windows lacks tzdata).

## Schema (append to schema.sql, additive)
```sql
CREATE TABLE IF NOT EXISTS merchant_patterns (
  id SERIAL PRIMARY KEY, pattern TEXT UNIQUE NOT NULL CHECK (length(pattern) >= 3),
  merchant TEXT NOT NULL, grp TEXT NOT NULL CHECK (grp IN ('Quick commerce','Food delivery','Shopping','Other')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS alert_rules (
  id SERIAL PRIMARY KEY, scope TEXT NOT NULL CHECK (scope IN ('merchant','group','category','total')),
  target TEXT NOT NULL, period TEXT NOT NULL CHECK (period IN ('day','week','month')),
  cap NUMERIC(12,2) NOT NULL CHECK (cap > 0), muted BOOLEAN NOT NULL DEFAULT false,
  snoozed_until TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (scope, target, period));
CREATE TABLE IF NOT EXISTS alerts (
  id SERIAL PRIMARY KEY, dedupe_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('act','watch','info')),
  title TEXT NOT NULL, detail TEXT NOT NULL, push_title TEXT NOT NULL, push_body TEXT NOT NULL,
  numbers JSONB NOT NULL, rule_id INT REFERENCES alert_rules(id) ON DELETE SET NULL,
  txn_id INT REFERENCES transactions(id) ON DELETE SET NULL, scope_key TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), read_at TIMESTAMPTZ,
  push_state TEXT NOT NULL DEFAULT 'pending' CHECK (push_state IN ('pending','sending','sent','held','skipped','failed')),
  push_attempts INT NOT NULL DEFAULT 0, pushed_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS idx_alerts_unread ON alerts (created_at DESC) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alerts_push ON alerts (push_state) WHERE push_state IN ('pending','held','failed');
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id SERIAL PRIMARY KEY, endpoint TEXT UNIQUE NOT NULL,  -- capability URL: secret, never log/return
  p256dh TEXT NOT NULL, auth TEXT NOT NULL, label TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_ok_at TIMESTAMPTZ, fail_count INT NOT NULL DEFAULT 0);
```
(Check transactions.id type — BIGSERIAL → use BIGINT FK.)
app_settings keys: `alerts.prefs` {push_on, private_notifications, quiet_start "22:00", quiet_end "07:30", max_per_hour 3, max_per_day 8, cooldown_hours 6, digest_on, digest_time "21:00", monthly_income (str|null), spend_plan (str|null), snooze_until, muted_kinds[]}; `alerts.watermark` {txn_id}; `alerts.digest_last` {date}. VAPID private key: env `RECTIFI_VAPID_PRIVATE_KEY` (base64url raw 32 bytes) or file `.rectifi/vapid_private.pem` created 0600 on first use; `.rectifi/` in .gitignore. Never logged/returned.

## Modules
- `rectifi/merchants.py` (stdlib + categorize.merchant_key): ordered REGISTRY (pattern, merchant, group), most specific first. Quick commerce: instamart→Swiggy Instamart (before swiggy), blinkit/grofers→Blinkit, zepto/kiranakart→Zepto, bigbasket/bbnow→BigBasket, "flipkart minutes"→Flipkart Minutes, jiomart→JioMart. Food delivery: swiggy/"bundl tech"→Swiggy, zomato→Zomato, eatsure, dominos. Shopping: amazon/amzn→Amazon, flipkart, myntra, ajio, meesho, nykaa. NOT the bare token "eternal". `match(payee, description) -> Merchant|None` (user patterns first; ≤4-letter patterns must start a word; lru_cache cleared on change), `groups()`, `add_pattern()`, `delete_pattern()`.
- `rectifi/alerts.py` tunables: THRESHOLDS=(50,80,100), STREAK_TIERS=(5,7,10) per ISO week per merchant, BULK_MIN=5, REALTIME_DAYS=2, MIN_PROJECTION_DAY=5, SMALL_SPEND_MAX=Decimal("500"), PRESET_MONTHS=3, PRESET_TRIM=Decimal("0.8"), ROUND_TO=Decimal("100"), SALARY_CATEGORY="Salary & Allowance", INCOME_LOOKBACK_DAYS=45.
  - `Alert` dataclass like Insight + dedupe_key, push_title, push_body, rule_id, txn_id, scope_key.
  - Pure (Context, today): `cap_alerts` (rules + budgets as implicit category/month caps; only the HIGHEST newly crossed threshold fires; key `cap:{scope}:{target}:{period}:{period_start}:{pct}`); `streak_alerts` (key `streak:{merchant}:{week_start}:{tier}`, "7th Swiggy order this week, ₹2,140 total"); `projection` (MTD×days_in_month÷day from day 5; "Y% of income" if known); `safe_to_spend` (spendable = spend_plan else sum budget limits else income else None; committed = active recurring (insights.find_recurring) whose usual day ≥ today and not yet paid this month; daily = floor((spendable − spent_before_today − committed) ÷ days_left_incl_today), min 0; left_today = daily − spent_today; return inputs for the UI formula); `income` (manual > detected salary credits of latest month within 45 days > none, with source); `presets` (3-month avg per merchant & group, "keep" rounded up to ₹100 and "trim 20%"); `leak_report(month)` (waterfall segments [Income, Fixed (recurring + Rent/EMI cats), Big one-offs ≥₹1,000 non-recurring, Quick commerce, Food delivery, Shopping, Other small UPI < SMALL_SPEND_MAX, Left] with server-computed start/end/amount Decimal + top-merchants table; invariant income − Σoutflows = left exactly); `today_view()` (safe/left today, today's small spends with merchant or masked payee, unread count).
  - Side effects: `sweep(origin) -> {new, mode}` (advisory lock, watermark, evaluate, insert ON CONFLICT DO NOTHING, set held/skipped/pending by gates); `tick(now)` (release held as ONE merged push "While you were asleep: N alerts", daily digest once `digest:{date}`, retry failures with backoff ≤5).
  - Push gates in order: global push off → snooze → muted kind/rule → quiet hours (held) → per-scope_key cooldown (100% bypasses) → hourly/daily limits (skipped; still in inbox/digest).
  - Templates; push_body is its own short template (never truncated detail). Private mode push: "Rectifi" / "New spending alert".
- `rectifi/webpush.py` (cryptography, http_ece, httpx only): `load_or_create_keys()`, `public_key_b64()`, `vapid_auth(endpoint, now)` (ES256 JWT aud=endpoint origin, exp=now+12h, sub=RECTIFI_VAPID_SUB or `https://<first allowed origin>`; fallback mailto if FCM rejects), `encrypt(payload, p256dh, auth)` (http_ece aes128gcm, ephemeral P-256), `send(sub, payload, ttl=3600, urgency="normal") -> ok|gone|retry|too_big` (headers TTL, Content-Encoding aes128gcm, Authorization vapid t=…,k=…, Urgency; 201 ok; 404/410 gone → delete sub), `allowed_endpoint(url)` (https + host ends with fcm.googleapis.com / push.services.mozilla.com / push.apple.com / notify.windows.com — SSRF guard). Payload `{"t","b","u":"/alerts#<id>","tag":scope_key}` masked, ≤2 KB.

## API (under /api, existing guards unchanged; money as strings via wire.py)
GET /alerts?status=unread|all&limit= · POST /alerts/{id}/read · POST /alerts/read-all · POST /alerts/{id}/mute · POST /alerts/{id}/snooze {hours:1|24|168} · POST /alerts/{id}/explain (model imported inside route) · GET /alerts/today · GET/POST /alerts/rules, PATCH/DELETE /alerts/rules/{id} (validate target vs registry/groups/categories) · GET /alerts/presets · GET/PUT /alerts/prefs (HH:MM, limits 1–20, income via parse_amount) · GET /leaks?month= · GET/POST/DELETE /merchants/patterns · GET /push/key {configured, public_key, fingerprint} · GET/POST /push/subscriptions, DELETE /push/subscriptions/{id} (list shows only id, label, push-service host, created, last_ok) · POST /push/test.
Hooks: (1) sms_gateway: after ingest_sms `added`, `BackgroundTask(alerts.sweep_and_deliver, "sms")`; response stays `{"status":…}`; errors logged by type name only. (2) quick-add route: sweep after add. (3) imports.finish_import: sweep after commit. (4) API lifespan: asyncio task every 60 s → `run_in_threadpool(alerts.tick)` + `sweep("tick")`, cancelled on shutdown. (5) app.py explicit `Route("/sw.js")` → FileResponse media_type text/javascript, Cache-Control no-cache, before SPA catch-all (Windows registry may map .js to text/plain). Static stays public under SessionLock; push routes need session.

## Web
- `web/public/sw.js` push-only: install→skipWaiting; activate→clients.claim + delete ALL caches; push→showNotification(t,{body,tag,data:{u},icon:"/icon-192.png"}); notificationclick→focus/openWindow(u). NO fetch listener, NO Cache API writes. Registered only when user taps "Turn on phone alerts".
- `web/src/lib/push.ts` state machine: unsupported | needs-https | ios-needs-install | denied | off | on; permission only from a click; urlBase64ToUint8Array; subscribe/unsubscribe → /api/push/subscriptions.
- `pages/Alerts.tsx` (/alerts + nav entry with unread chip with a word "3 new"): Inbox (InsightRow pattern; Read, Snooze ▾, Mute, Explain hidden without model + ModelCheck); Caps (merchant/group/category rows with 3-month avg, preset buttons "Keep ₹X" / "Trim 20%: ₹Y", custom amount + day/week/month, mute toggle, add merchant pattern); Delivery (push toggle + state text, quiet hours, digest, private notifications, monthly income with "detected from salary: ₹X" override, device list + Remove, Send test).
- `components/TodayStrip.tsx` atop Overview: "Safe to spend today ₹X", "Left today ₹Y", today's small spends chips, link to Alerts; empty: "Set a budget or your monthly income to see this".
- `pages/Leaks.tsx` ("Where your ₹1 lakh went") + `components/Waterfall.tsx` (SVG from server start/end strings, floats only to draw; follow PaceChart; no inline styles — CSP) + top-merchants table "N orders, ₹X, Y% of income" + projection line. Not the chart harness.
- build-web, commit dist. Streamlit unchanged.

## Phases + acceptance (each ends green)
- P1 schema + merchants: tests/test_merchants.py — `UPI/DR/412…/SWIGGY/YESB/swiggy@yesbank/Pay`→Swiggy/Food delivery; "Swiggy Instamart"→Instamart/Quick commerce; BLINKIT.PAYU@HDFCBANK, "ZEPTO MARKETPLACE PVT LTD", "amzn mktp", zomato-order@ptybl; negatives "AMAZING TAILORS", "SWIGGLE TOYS", "BBQ NATION"; user pattern overrides; pattern < 3 rejected.
- P2 evaluator: tests/test_alerts_eval.py — 49→50→79→81→101% fire 50/80/100 once each; 40→85 fires only 80; budgets as implicit caps; streak tiers per ISO week; no projection before day 5; safe_to_spend with/without committed, floored, days_left incl today; income precedence; preset rounding; waterfall invariant exact; ₹-in-numbers for every kind (amounts_in_text(title+detail+push_title+push_body) ⊆ numbers); alerts.py loads no langchain/rectifi.agent.
- P3 push transport: pins (test_requirements green); tests/test_webpush.py — encrypt→decrypt round trip with test UA keypair (+RFC 8291 App. A vector if feasible); VAPID JWT verifies (aud origin, exp ≤24h); headers; httpx.MockTransport 201 ok / 410 gone / 429 retry; allowed_endpoint rejects http, localhost, 10.x, unknown; private key never in returns/logs (caplog).
- P4 sweep/delivery/hooks: tests/test_alerts_delivery.py + tests/test_gateway_alerts.py — concurrent sweeps (threads) → one alert per key; SMS today → one pending; SMS 5 days old → bulk summary; 40-row import → exactly one import: alert pushed, caps skipped, later SMS doesn't re-fire 80%; quiet hours held → tick sends ONE merged push; cooldown; hourly limit skipped but in inbox; snooze; mute rule; mute kind; digest once/day; 410 deletes sub; payload masking (no 4+ digit runs except ₹ amounts, no @handle, no upi_ref/last4, ≤2 KB, private mode generic); /sms still 200 {"status":"added"} when push raises; gateway still no langchain; /health unchanged.
- P5 API: tests/test_api_alerts.py — rule CRUD + validation; prefs validation; subscription list never contains endpoint/keys; bad host 422; X-Rectifi-Request required; session required in public mode; GET /sw.js text/javascript + no-cache without session; money as strings. tests/test_service_worker.py — static checks on web/public/sw.js and dist: no fetch listener/onfetch, no caches.open…put/cache.add/addAll, activate deletes caches.
- P6 UI: npm test push.test.ts (base64 helper, state machine w/ mocked navigator), Waterfall.test.tsx (renders server labels; grep test: no money arithmetic). build-web, commit dist. E2E tests/test_web_e2e_alerts.py: set Swiggy weekly cap from preset → DB row; TodayStrip text equals server; inbox Read sets read_at; screenshots 390/1280 light/dark.
- P7 docs: CLAUDE.md rule 14 + map rows (merchants.py, alerts.py, webpush.py, sw.js); .env.example RECTIFI_VAPID_PRIVATE_KEY, RECTIFI_VAPID_SUB; README "Phone alerts" (open Tailscale URL in Chrome → Add to Home screen → Alerts → Turn on → Send test); PROGRESS.md real counts + Not verified.

## UNSURE (test on real phone)
Delivery when Chrome killed / OEM battery saver; push while phone Tailscale off (should arrive via FCM; tapping needs tailnet); FCM acceptance of https `sub`; http_ece 1.2.1 API fit; real UPI handles (kiranakart, bundl tech, "Supermarket Grocery Supplies", Amazon Fresh vs Amazon) — verify on his statements; laptop-off gaps (alerts only while Rectifi runs; late SMS → bulk summary).

## Risks
Merge collisions (start after integrator); double-send (advisory lock + FOR UPDATE SKIP LOCKED + unique key); alert fatigue (highest-only, cooldowns, limits, bulk, max_per_day 8); SSRF (host allow-list); Windows MIME for sw.js; VAPID rotation breaks subs (UI warns, key fingerprint); committed payments only what find_recurring detects (show formula inputs).
