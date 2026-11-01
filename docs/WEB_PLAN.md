# Rectifi web front-end: architecture, design brief and build plan

I read CLAUDE.md, PROGRESS.md, the README head, all of `rectifi/tools.py`, the public surface of `insights`, `importer`, `pdf_statement`, `sms_gateway`, `forwarder_config`, `tailscale` and `config`, the agent files (`graph.py`, `explain.py`, `llm.py`), all of `app/streamlit_app.py`, `app/theme.py`, `.streamlit/config.toml`, `scripts/dev.py`, `conftest.py`, and the existing test patterns (ScriptedModel, uvicorn in a thread, `cmd_up(None)`).

Things I checked by running them:
- Container has Node 22.22 and npm 10.9, and registry.npmjs.org answers 200.
- Python Playwright 1.56 is installed, with Chromium at `/opt/pw-browsers`.
- `python-multipart` 0.0.32 is installed (pulled in by mcp and streamlit) but is not pinned.
- Starlette 1.7 has `Request.form(max_files, max_fields, max_part_size)`.
- LangGraph 1.2.16 `stream(stream_mode="updates")` yields `{node_name: {...}}` dicts.
- The Tailscale Serve docs (kb/1312/serve) confirm the identity headers. Details are in section A6.

Note: PROGRESS.md says 455 passed; the brief says 487. The implementer must run the baseline first and record the real number.

---

## A. Architecture

### A1. Stack (each choice justified)

| Concern | Choice | Why |
|---|---|---|
| Server | **Starlette 1.7 + uvicorn 0.54**, both already pinned. Add the pin `python-multipart==0.0.32` (already installed, now used directly for uploads). | The gateway already uses this stack, so there is no new framework and no FastAPI. A thin layer is all that is needed. |
| SPA | **React 19 + Vite + TypeScript (strict)** | Most reliable for a Sonnet implementer. React 19 is about 45 KB gzipped, which is acceptable for a local app. |
| Routing | **wouter v3** (about 2 KB) | 9 flat routes plus query strings. React Router's weight buys nothing here. |
| Data | **@tanstack/react-query v5** | Cache, loading and error states, invalidation after mutations, and refetch-on-focus (which matters when the phone comes back to the tab). |
| Charts | **Hand-written SVG and HTML**, no library | Only 3 charts (category bars, budget bars with a pace tick, a cumulative-vs-pace line). A library would impose its own look and weight. Float maths is used for pixel positions only, which CLAUDE.md rule 2 allows for drawing. |
| UI primitives | **Native elements**: `<dialog>` (focus trap and Esc for free), native `<select>` (opens the OS picker on Android), `<details>`, `<input type=date>`. Tabs, segmented control and toast are hand-written to ARIA patterns. | No UI kit, so no generic shadcn look, and native controls are the most accessible on a phone. |
| Styling | Plain CSS with custom properties (`tokens.css`) plus **CSS Modules** per component. No Tailwind. | A semantic token system, and nothing that looks like a default kit. |
| Icons | **lucide-react**, tree-shaken, stroke 1.5, about 15 icons, never decorative | Saves drawing icons by hand. Text labels always sit beside nav icons. |
| Markdown in Ask | **A small in-house renderer** (`lib/markdown.tsx`): paragraphs, `**bold**`, `-`/`1.` lists, inline code. It builds React elements and never uses `dangerouslySetInnerHTML`. | XSS-safe by construction. The system prompt already forbids tables. |
| Fonts | **@fontsource-variable/fraunces, @fontsource/ibm-plex-sans (400, 500, 600), @fontsource/ibm-plex-mono (400, 500)**. All are OFL 1.1, bundled by Vite into `dist/assets`, with `font-display: swap`. Copy the OFL texts into `web/public/licenses/`. | Self-hosted, so it works offline. The Streamlit app pulls Google Fonts today, which fails offline. |
| Unit tests (web) | **vitest** (dev only) | Only for `money.ts` and `markdown.tsx`. |
| Bundle budget | JS at most 150 KB gzipped in total (raised to 165 KB when the alert pages landed), CSS at most 25 KB gzipped. Enforced by a test. | |

### A2. Folder layout

```
rectifi/api/__init__.py
rectifi/api/app.py           create_app(dist_dir=None): routes, middleware stack, static/SPA, lifespan(init_db)
rectifi/api/security.py      pure-ASGI: HostGuard, ApiGuard (custom header/Origin/Sec-Fetch-Site), BodyLimit, SecurityHeaders
rectifi/api/errors.py        ApiError, exception handlers, error JSON
rectifi/api/wire.py          to_wire(obj): Decimal→str (format(d,"f")), date→iso, datetime→iso; insight/txn/recon serializers; insight link mapping
rectifi/api/uploads.py       PendingUploads (TTL 15 min, max 5, lock), RecentImports (sha256→batch_id, lock)
rectifi/api/routes/{meta,overview,review,insights,transactions,imports,budgets,accounts,phone,ask}.py
rectifi/api/ask.py           graph singleton (lazy), per-thread locks, transcript(messages), NDJSON turn runner
web/package.json, package-lock.json, vite.config.ts, tsconfig.json, index.html
web/public/ favicon.svg, icon-192.png, icon-512.png, manifest.webmanifest, theme-init.js, licenses/OFL-*.txt
web/scripts/make_icons.py    dev-only (Python Playwright renders the SVG monogram to PNG); not shipped
web/src/main.tsx, App.tsx (routes + shell)
web/src/api/{client.ts, queries.ts, types.ts, ndjson.ts}
web/src/lib/{money.ts, money.test.ts, money.cases.json, dates.ts, markdown.tsx, links.ts}
web/src/styles/{tokens.css, base.css, fonts.css}
web/src/components/<Component>/{index.tsx, styles.module.css}
web/src/pages/{Overview,Review,Insights,Add,Ledger,Budgets,Accounts,Phone,Ask}.tsx (+ module css)
web/dist/                    COMMITTED build output + build-info.json
tests/test_api_*.py, tests/test_web_dist.py, tests/test_web_e2e.py, tests/test_tools_web_additions.py
```

`.gitignore` additions: `web/node_modules/`. Do **not** ignore `web/dist/`.

### A3. Additive backend changes

Each of these is new, has its own tests, and changes no existing behaviour.

1. **`tools.set_account(txn_id: int, account_id: int | None) -> dict`**
   - Raises `RectifiError` in three cases:
     - missing transaction: "No transaction #x."
     - unknown account id: "No account #x." (an explicit `SELECT`, because `_account_id` passes ints through unchecked)
     - `source == 'statement'`: "This entry came from a bank statement, so it belongs to that statement's account. Undo the import to move it."
   - Otherwise it runs `UPDATE` and returns `get_transaction`.
   - The unique indexes (`upi_ref, direction` and `fingerprint`) do not involve `account_id`, so this is safe.
2. **`tools.list_transactions(..., unlinked: bool = False)`**: a new keyword argument that adds `t.account_id IS NULL`. Existing calls are unaffected.
3. **`tools.spending_pace(month) -> {"month", "through", "total_limit", "days": [{"day", "spent", "cumulative", "pace"}]}`**
   - `through = min(last, today())`.
   - `days` is empty when `through < first`.
   - `pace = (total_limit * day_index / days_in_month).quantize(PAISE)`.
   - This moves the Overview's float pace maths out of the UI and into Decimal. Streamlit computes it inline today.
4. **`tools.preview_pasted_sms(text) -> dict | None`** and **`tools.save_pasted_sms(text) -> dict`**
   - Both use `parse_sms` plus the existing `_sms_account`.
   - `save` calls `add_transaction(..., source="sms", upi_ref=..., raw_text=text)` and raises the existing "That doesn't look like a transaction SMS…" message when nothing parses.
   - This moves account matching out of the Streamlit UI (lines 872–874).
   - **Behaviour note:** with one account that has no last-4 saved and an SMS naming digits, this now attaches, the same as the gateway does. Streamlit did not. Document it.
5. **`importer.preview_totals(rows) -> {"count", "money_out", "money_in", "first", "last"}`**: the Decimal sums currently done in `manual_preview`.
6. **`graph.chat_events(graph, text, thread_id)`**: a generator.
   - It records `before = len(state)`, iterates `graph.stream({"messages": [HumanMessage(text)]}, cfg, stream_mode="updates")` with `recursion_limit: 16`, and for `node == "tools"` yields `("tool", ToolMessage)` for each message.
   - It ends with `("done", get_state(cfg).values["messages"][before:])`.
   - The guard, masking and `_original_sms` are untouched because they live inside the nodes.

Nothing else in `rectifi/` changes. `app/streamlit_app.py`, `.streamlit/config.toml` and the MCP server stay as they are.

### A4. Wire conventions

- **Money** is a string with exactly 2 decimals (`"1250.50"`, `"-12.00"` with an ASCII minus), produced by `format(d, "f")`. A Decimal from NUMERIC(12,2) already has 2 decimal places.
- **Percent** is a 1-decimal string or `null`. Counts are ints. Dates are `"YYYY-MM-DD"`. Datetimes are ISO strings with an offset.
- **Transaction:** `{id, date, amount, direction, payee, description, note, source, account_id, account, category_id, category, category_kind}`. `upi_ref` and `raw_text` are left out of lists.
- **Insight:** `{key, kind, severity, title, detail, numbers: {k: str}, action_label, action_page, is_data_quality, can_apply, link: {page, params}}`.
  - `link` is built in `wire.py` from kind and key:
    - `unlinked` → `/ledger?account=none&period=all`
    - `other_pile` → `/ledger?category=Other&period=all`
    - `leak:<cat>` → `/ledger?category=<cat>`
    - `budgets` / `accounts` / `add` → that page
- **Recon** is the full `tools.reconcile` dict, serialized.
- **The client never computes money.** `money.ts` formats strings only, so `"1250.5"` is never produced.

### A5. Endpoint table

Every path is under `/api`. Every endpoint except `/api/health` requires the `X-Rectifi-Request: 1` header.

| Method | Path | Request | Response | Calls |
|---|---|---|---|---|
| GET | `/health` | none | `{ok:true}` | none |
| GET | `/meta` | none | `{today, months: ["YYYY-MM" ×12 newest first], weeks: [Monday ×12], categories:[{id,name,kind}], accounts:[{id,name,last4}], default_account_id}` | `tools.today`, `list_categories`, `list_accounts`, `default_account` |
| GET | `/overview?month=YYYY-MM` | none | `{month,start,end, summary:{income,spending,saved,savings_rate,count,by_category[{category,amount,count,share}]}, budgets:BudgetStatus, pace:{through,total_limit,days[]}, recent:Txn[≤8], proofs:[{account:{id,name,last4}, reconcile:Recon\|null, reason:str\|null}], top_insights:Insight[]\|null, insights_error:bool}` | `parse_month`, `month_bounds`, `spending_summary`, `budget_status`, **`spending_pace`**, `list_transactions(limit=8)`, `list_accounts` + `reconcile`, `insights.top_insights(3)` (wrapped: DB errors re-raise, anything else gives `insights_error:true`, the same as the Streamlit strip) |
| GET | `/review?week=YYYY-MM-DD` | none | `weekly_review()` serialized + `act_insights:Insight[]` | `weekly_review(date)`, `insights.act_insights` |
| PUT | `/review/commitment` | `{week, text}` | `{week_start, commitment}` | `save_commitment` |
| GET | `/insights` | none | `{span:{count,first,last,days}, items:Insight[]}` | `insights.data_span`, `insights.insights` |
| POST | `/insights/apply` | `{key}` (the client never sends an amount) | `{category, month, limit}` | Re-runs `insights.insights()` and finds the key. 404 if gone, 422 if `apply` is missing or `apply.tool != "set_budget"`. Then `tools.set_budget(**apply)`. |
| POST | `/insights/explain` | `{key}` | `{text\|null, dropped:[money], error\|null}` | lazy `rectifi.agent.explain.explain(item)` |
| GET | `/transactions?period=all\|YYYY-MM&category=&account=<id>\|none&search=&limit=500` | none | `{items:Txn[], truncated, limit}` (fetch limit+1; maximum 2000) | `list_transactions(..., unlinked=account=="none")` |
| POST | `/transactions` | `{amount:str, direction, date, payee?, category?\|null, account_id?\|null, note?, inbox_id?}` | `{status:"added"\|"duplicate", transaction}` | `add_transaction`. If `inbox_id` is set: `inbox_prefill` supplies `upi_ref` and `raw_text` from the database (never trust the client), `source="sms"`, then `mark_inbox_added`. |
| PATCH | `/transactions/{id}` | any of `{category, note, account_id}` (a present `account_id: null` means unlink) | `{transaction, learned_rule\|null}` | `set_category(remember=True)` (same as Streamlit), `update_note`, **`set_account`** |
| DELETE | `/transactions/{id}` | none | `{deleted:bool}` | `delete_transaction` (also used for "Undo" right after a quick entry) |
| POST | `/sms/preview` | `{text}` (at most 4000 characters) | `{parsed: {...}\|null}` | **`preview_pasted_sms`** |
| POST | `/sms/save` | `{text}` | `{status, transaction}` | **`save_pasted_sms`** |
| GET | `/inbox/{id}/prefill` | none | `{amount,direction,txn_date,payee,upi_ref,account_id,raw_text,inbox_id}` | `inbox_prefill` |
| POST | `/imports` | multipart: `file` (at most 10 MB; csv, xlsx, xls, pdf), `password?`, `manual?="1"` | `ImportOutcome` | `read_table`, `assess_statement`, `account_for_statement`, `import_rows`, `get_import_batch`, `reconcile` |
| POST | `/imports/preview` | `{upload_id, mapping}` | `Preview` | `normalise`, **`preview_totals`**, `account_for_statement` |
| POST | `/imports/commit` | `{upload_id, mapping, account_id}` | `ImportOutcome` (imported) | `normalise`, `import_rows(rows + review, account_id, filename)` |
| GET | `/imports/{batch_id}` | none | batch card | `get_import_batch`, `reconcile` |
| POST | `/imports/{batch_id}/undo` | none | `{removed, snapshots_restored, already_undone, batch}` | `undo_import` |
| GET | `/budgets?month=` | none | `{status:BudgetStatus, expense_categories:[str], previous_month}` | `budget_status`, `list_categories("expense")` |
| PUT | `/budgets` | `{month, category, limit: str\|null}` | `BudgetStatus` | `set_budget`, or `delete_budget` when null |
| POST | `/budgets/copy` | `{from, to}` | `{copied:int}` | `copy_budgets` |
| GET | `/accounts` | none | `{items:[{id,name,last4,transactions,last_snapshot,reconcile\|null,reason\|null}], default_account_id}` | `list_accounts`, `reconcile`, `default_account` |
| POST | `/accounts` | `{name, last4?}` | `{account}` | `add_account` |
| POST | `/accounts/{id}/balances` | `{as_of, balance:str}` (a minus sign is allowed for overdraft) | `{account_id, as_of, balance, reconcile\|null, reason}` | `record_balance`, `reconcile` |
| GET | `/phone/status` | none | `{tailscale:{installed,name,app_published,gateway_published}, app_url, gateway:{up,port,host,open}, gateway_url\|null, health_url\|null, lan_addresses:[{ip,label}] (only when open), tailscale_login}` | `tailscale.find_cli`/`read_status`/`dns_name`/`read_served_ports`/`app_url`/`gateway_url`, `sms_gateway.gateway_reachable`, `private_ipv4s`, `config.sms_*`. `tailscale_login` is the `Tailscale-User-Login` request header, so the user can see who they are. Runs in the threadpool because the subprocess can take up to 5 seconds. |
| GET | `/phone/devices` | none | `[{id,name,created_at,last_seen_at,revoked_at}]` | `list_devices` |
| POST | `/phone/devices` | `{name}` | `{device, token, setup_file:{filename,content}\|null, setup_unavailable_reason\|null, manual:{webhook_url,headers_json,template,sender_regex,text_regex}}`, sent with `Cache-Control: no-store` | `create_device`, `forwarder_config.build_forwarder_rules` (in memory only) |
| POST | `/phone/devices/{id}/revoke` | none | `{revoked}` | `revoke_device` |
| POST | `/phone/test` | `{token}` | `{status, said}` | `sms_gateway.send_test_sms(token, config.sms_port())` |
| GET | `/phone/inbox` | none | `[{id,sender,raw_text,sent_at,received_at,reason,device}]` | `list_inbox("pending")` |
| POST | `/phone/inbox/{id}/dismiss` | none | `{dismissed}` | `dismiss_inbox_item` |
| GET | `/ask/status` | none | `{ok, why, notice, model}` | `llm_status`, `llm_notice`, `describe` (lazy imports) |
| POST | `/ask/threads` | none | `{thread_id}` (uuid4) | none |
| GET | `/ask/threads/{id}` | none | `{items: TranscriptItem[]}` | `graph.get_state`, `unverified_amounts` |
| POST | `/ask/threads/{id}/messages` | `{text}` (at most 4000 characters) | NDJSON stream | **`graph.chat_events`** |

**ImportOutcome** has three forms:
1. `{outcome:"imported"|"already_imported", batch:{id,account_id,account,filename,created_at,added,merged,duplicates,needs_review,undone_at,rows_left}, how:"digits"|"chosen"|"most_recent"|"only", needs_review_lines:[str], skipped:[str], reconcile, reconcile_reason}`. The `how` logic is the same as Streamlit's `import_card`.
2. `{outcome:"preview", upload_id, filename, reasons:[str], account_reason, columns:[str], mapping:{role: col|null}, preview: Preview}`.
3. Errors, covered by the error contract.

**Preview** is `{rows:[{date,narration,direction,amount,balance}] (first 12), review_rows:[{date,narration,amount,why}], problems:[str] (first 5), totals, suggested_account_id}`.

**Upload flow on the server:**
- Hash the bytes with sha256.
- If `RecentImports` has the hash and that batch has `undone_at` null, return `already_imported`. This replaces Streamlit's per-session hash; the database dedupe stays the real guard.
- Call `read_table(data, name, password)`, then `del password`.
- If accounts exist, `check.confident` is true and `manual` is not set, call `account_for_statement`. If it picks an account, import. Otherwise store `{table, check, filename, hash}` (no bytes, no password) in PendingUploads and return a preview.
- An expired `upload_id` gives 409 `upload_expired`.

**Ask (no token streaming):**
- `chat_events` runs in a daemon thread that feeds a `queue.Queue`.
- The response is a `StreamingResponse` with media type `application/x-ndjson` that yields:
  - `{"type":"step","tool","error":bool}` for each tool,
  - `{"type":"tick"}` every 5 seconds (keeps proxies from timing out),
  - finally `{"type":"done","items":TranscriptItem[]}` or `{"type":"error","message": explain_error(exc)}`.
- **Why no token streaming:** the "Not verified" check needs the whole reply, so assistant text is shown only at `done`.
- If the client disconnects, the run still finishes and is saved in the checkpointer, so reloading the history shows it.
- One turn per thread at a time: a `threading.Lock` per thread id, held by the worker thread. A second turn gets 409 `conflict`.
- Thread id: the server makes it, the path must match a UUID regex (otherwise 404), and the client keeps it in `localStorage["rectifi.ask.thread"]`. "New conversation" calls POST `/ask/threads`.
- The graph is a lazy singleton per `config.llm_provider()`, built with `build_graph(checkpointer=postgres_checkpointer())`. If that fails, return 503 `model_unavailable` with the message. Expose `ask.reset()` for tests.
- **TranscriptItem** is `{role:"user", text}` or `{role:"assistant", text, unverified:[money], steps:[{tool, output, error}]}`. It is built by porting Streamlit's `render()` exactly (replay the context, call `unverified_amounts(seen)` per AI message, attach the pending tool steps).

### A6. Error contract, security and authentication

**Error body:** `{"error": {"code", "message", "field": null|str}}`. The message is always one plain sentence that is safe to show.

| Status | code | When |
|---|---|---|
| 400 | `bad_host` | Host not allowed (plain text body for non-`/api` paths) |
| 403 | `forbidden` | Missing custom header, bad Origin, `Sec-Fetch-Site: cross-site`, any OPTIONS request |
| 404 | `not_found` | |
| 409 | `conflict` / `upload_expired` | |
| 413 | `too_large` | |
| 415 | `unsupported_media` | |
| 422 | `invalid` | Any `RectifiError`, with its message verbatim |
| 422 | `pdf_password_needed` / `pdf_password_wrong` | Decided by comparing `str(exc)` with `pdf_statement.NEEDS_PASSWORD_MESSAGE` / `WRONG_PASSWORD_MESSAGE` |
| 503 | `db_unavailable` | `DatabaseUnavailable`, `psycopg.OperationalError`, `PoolTimeout`. The message is `str(exc)` plus Streamlit's db_down text, which never contains the password. |
| 503 | `model_unavailable` | |
| 500 | `internal` | Generic message. Logs only the exception type and path, never bodies. |

**Security decision.** Threats: browser CSRF from any site, DNS rebinding against 127.0.0.1, other tailnet devices and users, and local processes.

1. **HostGuard** (all paths): the hostname from `Host` (port ignored) must match `RECTIFI_ALLOWED_HOSTS`. The default is `localhost,127.0.0.1,::1,*.ts.net`, the same list as Streamlit's `allowedHosts`. This is the defence against DNS rebinding: a rebinding page still sends `Host: evil.com`.
2. **ApiGuard** (`/api/*` except `/api/health`):
   - Requires the `X-Rectifi-Request: 1` header. A cross-site page cannot add it without a CORS preflight, and we never answer preflights: OPTIONS gives 403 and no `Access-Control-*` header is ever sent.
   - Rejects `Sec-Fetch-Site: cross-site`.
   - On state-changing methods, if `Origin` is present it must be one of:
     - a loopback http origin on any port (covers Vite dev on 5173),
     - an https origin whose host ends in `.ts.net`,
     - an origin whose host equals the Host header,
     - an entry in `RECTIFI_ALLOWED_ORIGINS` (set by dev.py from `tailscale_origin()`, like `STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS`).
   - With no `Origin`, the request is allowed only when `Sec-Fetch-Site` is also absent (a non-browser client such as tests or curl).
   - The ts.net rule is deliberately looser than "Origin == Host" because whether Serve keeps the Host header is UNSURE.
3. **Media types:** JSON endpoints require `application/json` (otherwise 415). `/imports` requires `multipart/form-data`.
4. **BodyLimit** (pure ASGI): checks `Content-Length` and also counts streamed bytes. 64 KB for JSON, 10 MB for `/api/imports`. Use `request.form(max_files=1, max_fields=5, max_part_size=64*1024)`.
5. **SecurityHeaders:**
   - CSP: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`
   - `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`
   - `Cache-Control: no-store` on `/api`
   - `index.html` gets `no-cache`; `/assets/*` gets `public, max-age=31536000, immutable`
   - React sets styles through the CSSOM, so `style-src 'self'` holds. The theme bootstrap is the external `/theme-init.js`, not an inline script.
6. **Secrets:**
   - The PDF password goes only from the form to `read_table`, is then deleted, and is never logged, cached or put in a query string.
   - The device token appears only in the POST `/phone/devices` response (`no-store`). The client holds it in React state only, never in localStorage, and builds the setup file as a Blob in memory.
   - `/phone/test` takes the token in the body.
7. **App passcode: not in v1 (decision).** This is parity with Streamlit today. The threat model is a personal tailnet whose devices have their own lock screens, and a passcode adds session and hashing work that is out of scope for this session.
   - **Optional Phase 7 lock instead:** `RECTIFI_TAILSCALE_USER=<login>`. When set, a request is allowed if `Tailscale-User-Login` equals it, or if there is no such header and the Host is loopback (a browser on the laptop). Everything else gets 403. This blocks devices shared to friends and tagged devices.
   - Verified from Tailscale docs (kb/1312/serve):
     - Serve adds `Tailscale-User-Login`, `Tailscale-User-Name` and `Tailscale-User-Profile-Pic` on proxied requests.
     - It removes those headers from incoming requests to prevent spoofing.
     - They are not set for tagged devices or Funnel.
     - They *are* set for users of shared devices.
   - Local processes bypass all of this, but they could read `.env` and the database anyway. Never enable Funnel; `doctor` could warn if it is on.
8. Tailscale traffic arrives from 127.0.0.1, so **no IP-based trust**.

### A7. Serving and dev.py

**`create_app(dist_dir=None)`:**
- `dist_dir` comes from `RECTIFI_WEB_DIST`, defaulting to `ROOT/web/dist`.
- Routes: `Mount("/api", routes=[...])`, then `Mount("/assets", StaticFiles(dist/assets))`, then a catch-all GET route:
  - It serves a file from the dist root if one exists, after resolving the path and checking it stays inside dist (no path traversal).
  - Otherwise it serves `index.html` (SPA fallback).
  - An unmatched `/api/...` must return JSON 404, not `index.html`.
  - If dist is missing, return a plain HTML 503: "The web app isn't built. Run `python scripts/dev.py build-web`, or use `python scripts/dev.py up --streamlit`."
- Run a single uvicorn worker, because the caches are in memory.

**dev.py** (stays stdlib-only; `tests/test_dev_script.py` must keep passing):
- `APP_PORT` stays `"8501"`. `cmd_app(args)` and `cmd_up(args)` read `getattr(args, "streamlit", False)`, because the tests call `cmd_up(None)`.
- Add `--streamlit` (store_true) to `app`, `up` and `demo`. Without it, run:
  `[python, -m, uvicorn, --factory, rectifi.api.app:create_app, --host, 127.0.0.1, --port, 8501, --log-level, warning]`
  with env `RECTIFI_ALLOWED_ORIGINS=<tailscale origin>` when one is known.
  - If `web/dist/index.html` is missing, print the build or `--streamlit` advice and exit 1.
  - With `--streamlit`, use today's Streamlit command unchanged, on the same port, so Tailscale Serve keeps working with no change.
- **`web`** (development): starts the API with `--reload` on 8501 and `npm run dev` (Vite on 127.0.0.1:5173, which proxies `/api` to 127.0.0.1:8501 with `changeOrigin: false`) as two children, stopped in a `finally`. Find npm with `shutil.which("npm")`; on Windows this resolves to `npm.cmd`.
- **`build-web`**:
  1. Check that `node --version` is at least 20.19 (Vite 7's floor; if Vite 6 is chosen, at least 18).
  2. Run `npm ci` when `node_modules` is missing or older than the lockfile.
  3. Run `npm run build` (`tsc -b && vite build`) and `npm test` (`vitest run`).
  4. Write `web/dist/build-info.json` = `{"source_hash": web_source_hash(), "node": "<version>"}`.
- **`web_source_hash()`**: sha256 over sorted relative POSIX paths plus `\0` plus the contents with CRLF normalised to LF. Files covered: `web/src/**`, `web/public/**`, `index.html`, `package.json`, `package-lock.json`, `vite.config.ts`, `tsconfig*.json`. CRLF is normalised so that Windows git autocrlf does not cause false "stale" results.
- **`doctor`**: add "Web app built" (ok/fail) and "Web app up to date" (warn when the hash differs). Node is never required to run the app.
- `tailscale` is unchanged and still proxies https 443 to 127.0.0.1:8501.

**CLAUDE.md:** add a rule 13 and update the Map and Commands sections. Rule 13: the web API (`rectifi/api`) is a third front door. It does no money maths, money crosses the wire as strings, every `/api` request passes the guards, only POST `/api/phone/devices` ever returns a token, and agent imports are lazy.

---

## B. Design brief

**Product understanding.** Rectifi is a ledger that proves itself. The user does four things:
1. capture a payment in under 10 seconds (on the phone),
2. see "am I on track this month?" in 3 seconds,
3. *trust* the numbers, because the books balance against the bank,
4. once a week, decide one thing to change.

The UI is ordered around that loop: capture, then categorise and link, then reconcile, then improve.

**Visual direction: "The Passbook."** It evolves the existing paper-ledger theme toward an Indian bank passbook and accountant's columnar paper. Warm paper, blue-black ink, hairline rules, rubber-stamp state labels, and serif headings like a printed annual statement.

**Philosophy: "Proof is the hero; every mark earns its place."** Hierarchy comes from type size, rules and whitespace, not boxes. There is no card grid. Only two things get a border: the *slip* (a reconciliation or import result, like a counterfoil) and overlays. One accent, used for actions, links, focus and data. Colour never carries meaning alone: every state has a word.

**Signature elements:**
1. **Stamp labels.** State words in 11.5 px uppercase with .08em tracking, a 1 px border in the state colour, the state background tint, 2 px radius: BALANCED, GAP, ACT, WATCH, NOTE, OVER, NEAR, ON TRACK, ALL CLEAR.
2. **Ruled totals.** In slips, a single ink rule sits above "= expected closing", and a *double* rule (accounting's mark for a final total) sits under the final figure.
3. **Double hairline before the amount column** in ledgers.
4. **Fraunces hero numerals** with lining tabular figures.
5. **Passbook day headers** in the phone ledger, sticky ("WED 21 OCT").
6. Paise drawn at 0.85em in muted ink in ledgers, so whole rupees scan first.

### B1. Typography

Families:
- **Display:** `"Fraunces Variable", Fraunces, Georgia, "Times New Roman", serif`. Weights 500 and 600, `font-optical-sizing: auto`.
- **Body:** `"IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
- **Figures:** `"IBM Plex Mono", ui-monospace, "Cascadia Mono", Consolas, monospace` with `font-variant-numeric: tabular-nums`.

Scale (about 1.25 ratio, 16 px base; inputs are never under 16 px, so Android does not zoom):

| Token | Size / line height | Use |
|---|---|---|
| `--t-xs` | 12/16 | Eyebrows: Plex Sans 600, uppercase, .12em tracking |
| `--t-sm` | 14/20 | Meta, captions |
| `--t-md` | 16/24 | Body |
| `--t-lg` | 20/28 | Section titles (Fraunces 500), phone page title |
| `--t-xl` | 25/32 | Account names, slip headline |
| `--t-2xl` | 32/38 | Desktop h1 (Fraunces 600, -0.01em) |
| `--t-hero` | `clamp(52px, 11vw, 88px)`/0.95 | Fraunces 600, `lining-nums tabular-nums` |

Mono figures: 15/22 in ledger rows, 22/28 in hero cells, 14/20 in slips.

### B2. Colour tokens (contrast ratios computed)

| Token | Light | Dark | Use |
|---|---|---|---|
| `--c-bg` | #F6F2E9 | #17150F | page (paper) |
| `--c-raised` | #FBF8F2 | #211E17 | dialogs, sheets, slips, toasts |
| `--c-sunk` | #ECE5D6 | #110F0B | input fill, user message, bar track, skeleton |
| `--c-text` | #1D1B16 | #ECE6D8 | primary text |
| `--c-muted` | #5C5547 | #A79F8E | secondary text |
| `--c-subtle` | #6E6656 | #8F8777 | placeholders, disabled text, tertiary |
| `--c-rule` | #D9D0BE | #3A352C | decorative hairlines only |
| `--c-rule-strong` | #8C826F | #6E6656 | input and control borders (non-text 3:1) |
| `--c-accent` | #1F4E5F | #7FBACB | primary button, links, focus, chart data |
| `--c-on-accent` | #F6F2E9 | #17150F | text on accent |
| `--c-accent-tint` | #DCE7E8 | #1E3036 | selected nav, saved-row flash |
| `--c-ok` / `--c-ok-bg` | #3F6B4F / #E3EADF | #8DBA98 / #22301F | ok, credit (+ word) |
| `--c-near` / `--c-near-bg` | #8A5A12 / #F3E6CC | #E0A64A / #3A2C14 | near, watch, check |
| `--c-over` / `--c-over-bg` | #9B2C2C / #F2DCD7 | #E07A6B / #3B1F1B | over, gap, act, danger |
| `--c-overlay` | rgb(29 27 22 / .42) | rgb(0 0 0 / .6) | backdrop |

Contrast, light (dark):

| Pair | Light | Dark |
|---|---|---|
| text on bg | 15.4 | 14.67 |
| text on raised | 16.23 | 13.37 |
| text on sunk | 13.72 | 15.38 |
| muted on bg | 6.61 | 6.95 |
| muted on raised | 6.96 | 6.33 |
| muted on sunk | 5.88 | 7.29 |
| subtle on bg | 5.08 | 5.13 |
| subtle on raised | 5.36 | 4.67 |
| accent on bg | 8.13 | 8.51 |
| on-accent on accent | 8.13 | 8.51 |
| accent on sunk | 7.24 | 8.92 |
| text on accent-tint | 13.63 | 11.02 |
| accent on accent-tint | 7.19 | 6.39 |
| ok on bg | 5.48 | 8.35 |
| ok on ok-bg | 4.99 | 6.36 |
| near on bg | 5.29 | 8.45 |
| near on near-bg | 4.79 | 6.27 |
| over on bg | 6.74 | 6.23 |
| over on over-bg | 5.73 | 5.13 |
| rule-strong on bg (non-text) | 3.39 | 3.21 |
| rule on bg (decorative only) | 1.37 | 1.5 |

Theme: follows `prefers-color-scheme`, with a manual System / Light / Dark switch (in the sidebar footer, or the More sheet on phone) stored in `localStorage["rectifi.theme"]`. `/theme-init.js` loads synchronously in `<head>` and sets `data-theme` before first paint. Add `<meta name="theme-color">` for both schemes.

### B3. Spacing, radius, borders, elevation

- **Spacing** (4 px base): `--s-1` 4, `--s-2` 8, `--s-3` 12, `--s-4` 16, `--s-5` 24, `--s-6` 32, `--s-7` 48, `--s-8` 64, `--s-9` 96.
  - Sections: s-7 apart on desktop, s-6 on phone.
  - Rows: 12 px vertical padding, with a minimum height of 44 px when interactive.
  - Eyebrow to content: s-2.
- **Radius:** `--r-1` 2 px (stamps), `--r-2` 4 px (inputs, buttons, slips), `--r-3` 10 px (sheet top corners and dialogs only). No pills, apart from the 2 px stamps.
- **Borders:** 1 px `--c-rule` for separators, 1 px `--c-rule-strong` for controls, 1 px `--c-text` for the total rule, double 3 px (`border-bottom: 3px double`) for the final total.
- **Elevation:** none in the page. Overlays only:
  - light: `0 1px 0 var(--c-rule), 0 16px 40px rgb(29 27 22 / .18)`
  - dark: a 1 px `--c-rule-strong` border plus `0 16px 40px rgb(0 0 0 / .5)`
  - Sticky bars use a bottom hairline, not a shadow.
- **Focus:** `:focus-visible { outline: 2px solid var(--c-accent); outline-offset: 2px }`.
- **Targets:** every tappable element is at least 44×44 px at every width.

### B4. Layout and navigation

**Desktop (1024 px and up):**
- Fixed 240 px sidebar (`--c-bg`, right hairline). From top to bottom:
  - wordmark "Rectifi" (Fraunces 24/600) and tagline "every rupee, accounted for" (`--t-sm` muted),
  - full-width primary button "Add entry" (to `/add`),
  - nav in three groups with eyebrows: DAILY (Overview, Review, Insights, Ask), RECORDS (Ledger, Budgets, Accounts), SETUP (Phone),
  - footer: theme switch and model line.
- Active item: accent-tint background, a 3 px accent bar on the left, and `aria-current="page"`.
- Main column: 48 px padding, max width 1080 px. Page header has an eyebrow, an h1 and right-aligned controls.

**Below 1024 px** (designed at 390 px):
- Sticky top bar, 56 px: page title in Fraunces 20, plus the page's control (month or week stepper, or "New conversation").
- Bottom nav, 64 px plus `env(safe-area-inset-bottom)`, five slots: **Home, Ledger, Add (centre; accent square with "+" and the word "Add"), Ask, More**.
  - More opens a bottom sheet with Review, Insights, Budgets, Accounts, Phone and the theme switch.
  - Rationale: Add, Ledger and Ask are the daily phone actions. Review and Insights are reached from Home's "Worth a look" and "This week" entry points.
- Hide the bottom nav while a text input is focused: `@media (max-width:1023px){ body:has(input:not([type=checkbox],[type=radio]):focus, textarea:focus, select:focus) .bottomnav {display:none} }`.
- 16 px side padding. Content max width is 720 px on tablet.
- Viewport meta: `width=device-width, initial-scale=1, viewport-fit=cover`.
- PWA: manifest (`display: standalone`, the paper colours, 192 and 512 px PNG icons showing a Fraunces "R" monogram with a double rule under it). **No service worker**, so stale financial data is never served.

**Shared controls:**
- **Month stepper:** "‹ [October 2026] ›". The two arrows are 44 px buttons. The label is a native `<select>` over `meta.months`, styled to look like text with a caret. Next is disabled at the current month.
- **Week stepper:** the same pattern over `meta.weeks`, labelled "This week · 19–25 Oct".

### B5. Page wireframes

**Overview** (`/` and `/overview?month=`)
- Header: eyebrow OVERVIEW, h1 "October 2026", month stepper.
- **Hero**, full width with a bottom hairline:
  - Left: eyebrow SAVINGS RATE, numeral "27.5%" (the "%" at .38em, muted), and a caption. Use the three Streamlit caption variants verbatim.
  - Right: three "passbook cells" separated by vertical rules: Money in, Money out, Saved. Mono 22 px, whole rupees. A negative Saved shows "−₹…" in over colour plus the word "short".
  - On phone the numeral sits above three equal cells in one row.
- **Worth a look:** up to 3 compact insight rows (stamp, title, then a link or apply button), then "All insights →". The section is hidden when empty. When `insights_error` is true, show the muted caption "Suggestions could not be worked out this time. The rest of the page is fine."
- **Desktop 7/5 split.**
  - Left: **Budgets**, then **Where it went**, then **Spending vs budget pace**.
    - Budgets: BudgetRow ×n. The legend reads "│ marks day 21 of 31". Empty state offers a "Set budgets" link.
    - Where it went: CategoryBars.
    - Spending vs budget pace: PaceChart, shown only when `total_limit > 0`.
  - Right: **Latest entries** (8 LedgerRows, then "Open ledger"), then **Proof of completeness**.
    - Proof is one ReconSlip per account that can reconcile.
    - Its empty state is "Not proven complete yet" with buttons "Record a balance" (to `/accounts`) and "Import a statement" (to `/add?tab=import`).
- **Phone order:** hero, worth a look, budgets, proof (moved up because it is the identity), latest entries, where it went, pace chart.
- **Empty month:** EmptyState "Nothing recorded for this month yet." with Streamlit's copy, plus buttons "Add an entry" and "Import a statement".

**Review** (`/review?week=`)
- Header with the week stepper and a caption: "counted through Saturday 24 October · the week isn't over yet".
- Hero: eyebrow SPENT THIS WEEK, a whole-rupee numeral, a caption, and cells Money in, Saved, Savings rate.
- **Compare slip:** money out this week, money out on the same days last week, and the "= change" row. The change row is hidden when `prev_spending` is 0, as in Streamlit. Verdict stamp: UP 12.4% / DOWN / SAME / NOTHING TO COMPARE, plus a sentence.
- **Needs attention:** a checklist of rows, each with a stamp, a sentence, and an inline action:
  - "Fix in Ledger" goes to `/ledger?category=Other&period=all`.
  - Unlinked goes to `/ledger?account=none&period=all`.
  - Budgets and accounts go to their pages.
  - When nothing needs attention, show the ALL CLEAR stamp and Streamlit's sentence. Show it only when `all_clear` is true.
- **Where it went** (top 3) and **Biggest payments** (3 LedgerRows).
- **Worth acting on:** the act-level insights.
- **One commitment:** last week's commitment in a slip ("You said: …" / "Did you keep it?"), then a text input (at most 280 characters, with a live count shown after 240) and a Save button. Once saved it reads as text with an "Edit" button.

**Insights** (`/insights`)
- h1 and the intro sentence (Streamlit copy).
- If `span.days < 28`, a note callout (near tint, hourglass icon, words).
- Groups with eyebrows: FIX THESE FIRST, ACT NOW, KEEP AN EYE ON, GOOD TO KNOW.
- **InsightRow:**
  - stamp (ACT, WATCH, NOTE) in a left column 6.5 rem wide on desktop, stacked on phone,
  - title (16/600), detail (muted),
  - actions row:
    - primary: "Set a ₹3,000 budget", which POSTs apply, then shows the success toast "Budget set for Food & Dining." and refetches; or a link button to `link`,
    - tertiary: "Explain this", shown only if `/ask/status.ok`. It renders a note block labelled "Assistant's note · every figure checked against the numbers above". The dropped case gives a near-tinted row: "Not shown: the assistant used ₹X, which Rectifi did not calculate…". The error case gives a muted row.
- Empty: "Insights need about a month of data." plus "Go to Add". Nothing found: "Nothing to flag right now." Footer caption when the model is off.

**Add** (`/add?tab=quick|sms|import&inbox=<id>`)
- Tabs (ARIA tablist). On phone they become a full-width 3-segment control, 44 px tall. The tab is reflected in the URL.
- **Inbox banner** (when `?inbox`): "Filled in from a bank SMS your phone sent. Check every field, then save." The raw SMS is shown in a mono block, with a "Forget this SMS" button.
- **Quick entry:**
  - AmountInput: "₹" prefix, mono 32 px, `inputmode="decimal"`, autofocus on desktop only.
  - SegmentedControl "Money out / Money in" (a radio group with words).
  - Date: native input with `max=today`, plus quick buttons "Today" and "Yesterday".
  - "Paid to / received from".
  - Category select: "Auto (from payee)" first, then categories filtered by direction plus transfer.
  - Account select: `default_account_id` preselected, "No account" last, with the hint "Entries with no account aren't covered by the balance check."
  - Note.
  - Primary "Save entry" (full width on phone).
  - On success: the form resets and a **receipt line** appears above the form: "Saved ₹250.00 · Swiggy · Food & Dining", with **Undo** (DELETE that id, offered for 10 seconds, only when status is `added`). A duplicate shows the info line "Already recorded (same UPI reference). Nothing was added."
- **Paste SMS:** a textarea. Typing triggers a debounced (300 ms) POST `/sms/preview`, which renders a **parsed slip** (Amount, Type, Date, Payee, UPI ref, Account match or "No account matched", and warnings) and a "Save from SMS" button. A non-transaction gets a near callout with the Streamlit sentence.
- **Import statement:**
  - FileDrop (click or drop; accepts .csv, .xlsx, .xls, .pdf; shows file name and size), the checkbox "Let me check the columns and choose the account myself", and IMPORT_HELP as the caption.
  - Uploading state: "Reading statement…" with the button disabled. No fake progress bar.
  - On `pdf_password_needed`: a password field (`autocomplete="off"`), "Open the PDF", and the Streamlit hint. The client keeps the `File` object and clears the password from state right after the request.
  - **Result slip:** the filename as eyebrow, rows Added / Merged with your entries / Already recorded / Needs review, an IMPORTED stamp plus "Went into SBI Savings (matched by the account digits)", the needs-review warning and list, the **ReconSlip** "Balance check", and a secondary "Undo this import" (or the caption "added nothing, nothing to undo").
  - After undo: an "Import undone." slip and "Import this file again".
  - **Preview mode:**
    - a reasons callout,
    - "Columns Rectifi detected": 8 selects in a 2-column grid (4 on desktop); each change POSTs `/imports/preview`,
    - a review-rows table,
    - a totals strip (Rows, Period, Money out, Money in),
    - the first 12 rows in a table (Date, Narration, Out, In, Balance),
    - the problems list,
    - an "Import into account" select, and primary "Import N rows".

**Ledger** (`/ledger?period=&category=&account=&search=`)
- Filter bar: Period (All time + 12 months), Category (All + categories), Account (All, each account, "Not linked"), Search (debounced 250 ms; the "/" key focuses it on desktop). On phone the filters collapse into a "Filters (2)" button that opens a sheet. Every filter lives in the URL.
- Count line: "214 entries · newest first · changing a category teaches Rectifi that payee". Truncation notice: "Showing only the newest 500. Pick a month or search to see older ones."
- **Desktop table**, columns: Date (mono muted), Payee (with the description small below), Category (inline native select), Account (inline select; disabled with an explanation for statement rows), double hairline, Amount (right, mono; a credit shows "+" in the credit colour plus a small "IN"), Source (muted), Note (inline input; saves on blur or Enter, Esc reverts), and a row "⋯" (Delete).
  - Each edit sends PATCH at once.
  - Row states:
    - saving: the cell shows `aria-busy` and the muted text "Saving…",
    - saved: the row tint fades over 600 ms and the cell says "Saved" for 2 s,
    - error: the select reverts, and a full-width message sits under the row in the over colour (`role="alert"`), e.g. the category-kind message.
- **Phone:** sticky day headers. Each row has two lines: payee (600) with the amount on the right, then "category · account or Not linked" in muted text. A tap opens the **Entry sheet**: all fields, category and account selects, the note, and Delete.
- **Delete:** a confirm dialog "Delete this entry? ₹250.00 · Swiggy · 21 Oct. This can't be undone." with Cancel and a danger Delete.
- Empty: "No transactions match." plus "Clear filters".

**Budgets** (`/budgets?month=`)
- Header with the month stepper. Summary eyebrow: "OCTOBER · 68% OF THE MONTH GONE · ₹9,200 OF ₹14,000 USED".
- Budgeted rows (BudgetRow), each with an inline limit editor: an AmountInput showing the current limit. When the value changes, "Save" and "Cancel" appear. "Remove limit" is a tertiary action. After saving, the bar width animates to the new value.
- "No budget yet" rows: the category with "Set a limit" (input and save).
- Empty month: "No budgets for this month yet." plus "Copy budgets from September". If nothing is copied, toast "No budgets in September to copy."

**Accounts** (`/accounts`)
- Per account section: name (Fraunces 25) with "··1234" in mono muted, a meta line ("42 entries · last bank balance 18 Oct 2026"), then the **ReconSlip** or the reason text.
- Under each account, a secondary "Record a balance" that opens an inline form: date (`max=today`), balance (decimal; a minus sign is allowed), Save. On save, refetch and, when the slip turns balanced, play the stamp animation once.
- At the bottom (the right column on desktop): "Add an account" (name; last 4 digits with `inputmode="numeric"`, maxlength 4, hint "matches bank SMS").
- Empty: "No accounts yet." with the Streamlit copy.

**ReconSlip** (`--c-raised`, 1 px rule, 4 px radius, mono 14 px):
1. Opening balance · 01 Oct 2026, then the amount.
2. "+ credits Rectifi recorded".
3. "− debits Rectifi recorded (51 entries)".
4. "= expected closing", with a single ink rule above.
5. "Bank's closing · date", with a double rule under it.
6. Verdict: a BALANCED stamp (ok) and "Every rupee is accounted for.", or a GAP ₹500.00 stamp (over) with the explanation and actions "Open this account in the Ledger" and "Import a statement".

**Phone setup** (`/phone`)
- Sections, each with an eyebrow:
  1. Open Rectifi on your phone: stamp (PUBLISHED, NOT PUBLISHED YET, TAILSCALE IS OFF, NOT SET UP), the URL as a CodeValue with copy, and the "Add to Home screen" hint.
  2. "How this works, honestly." note.
  3. Gateway: ON/OFF stamp; when off, `.env` lines and commands as CodeValues with copy.
  4. Pair a phone: name input and "Create a pairing token".
     - Pairing panel: a near warning ("shown once…"), primary **Download setup file** (a Blob built from `setup_file.content`, disabled with the reason when null), the numbered steps, "Send a test message" with its result line, a "Set it up by hand" disclosure (all the manual values with copy), an "iPhone (Shortcuts): untested" disclosure, and "Done: hide the token" (clears state).
  5. Paired phones: name, ACTIVE/REVOKED stamp, "Last message: 3 min ago (24 Oct, 14:02)", and Revoke (confirm dialog).
  6. "Needs review (n)": raw SMS in mono, a meta line, and the buttons Add (to `/add?inbox=id`) and Dismiss.
  7. Good to know (Streamlit copy).
- Show the "Signed in to Tailscale as …" line when `tailscale_login` is present.

**Ask** (`/ask`)
- Centred column, max width 720 px.
- Header: h1 "Ask Rectifi", "New conversation" button, and the line "Model: … · amounts are checked against what you type before anything is saved".
- Off state: a near callout with `why`, EmptyState "The assistant is off — the rest of Rectifi works.", and "Go to Add". Show the notice callout when present.
- Empty thread: eyebrow TRY and five example buttons, which send immediately.
- **Transcript** (no chat bubbles):
  - User turns: a "YOU" eyebrow, then the text on `--c-sunk` with a 3 px left rule in `--c-rule-strong`.
  - Assistant turns: a "RECTIFI" eyebrow, then rendered markdown.
  - Under an assistant reply: when `unverified` is non-empty, the row "⚠ Not verified: ₹77,777. I can't match this to anything you typed or to a number Rectifi worked out, so check it before you rely on it." (near tint, icon, and words). Then a disclosure "What Rectifi did · 2 steps" listing tool names and mono outputs; error steps get a REFUSED stamp.
- **Composer:** sticky at the bottom, an auto-growing textarea (up to 6 lines), Send (44 px). Enter sends, Shift+Enter adds a newline.
- **Pending:** the user message appears at once. Below it, "Rectifi is working…" with live step lines ("✓ log_transaction") and, after 5 s, an elapsed counter. The composer is disabled during the turn.
- **Error:** an inline over-tinted block with the message and "Try again". A 409 conflict shows "Still answering your last message."

### B6. Component inventory

AppShell (SkipLink, Sidebar, TopBar, BottomNav, MoreSheet), PageHeader, MonthStepper, WeekStepper, Eyebrow, Stamp, Money (`value`, `paise`, `sign`, `tone`; renders `₹` / integer / `.paise` spans; `data-testid` passthrough), Hero + HeroCell, LedgerRow / LedgerTable / LedgerList (+ DayHeader), ReconSlip, CompareSlip, ImportSlip, BudgetRow (+ BudgetBar with pace tick), CategoryBars, PaceChart, InsightRow (+ ExplainNote), Callout (note / near / over with icon and words), Button (primary, secondary, tertiary, danger; `loading`; icon-left), IconButton (requires `aria-label`), Field (label, hint, error with `aria-describedby`), TextInput, AmountInput, Select, SegmentedControl, Tabs, Checkbox, FileDrop, Dialog (`<dialog>`, a `sheet` variant below 1024 px), ConfirmDialog, ToastRegion, Disclosure, CodeValue (+ copy, with "Copied" feedback), EmptyState, DbDownPage, OfflineBanner, Skeleton (ledger lines, hero bar), InlineStatus, Transcript, Composer.

### B7. Designed states

All pages share these states:
- **Loading:** a skeleton shaped like the page layout, shown only if loading takes longer than 200 ms.
- **db_unavailable:** a full-page DbDownPage with Streamlit's copy, "Try again", and an automatic retry every 10 s.
- **Network failure:** a sticky OfflineBanner: "Rectifi on your laptop isn't answering. Is the laptop on and `dev.py up` running?" with Retry.
- **4xx:** inline next to the action that caused it.
- **Disabled buttons during mutations:** `aria-disabled` and the button's own loading label.

Per page:

| Page | Empty | Partial or warning | Success | Error |
|---|---|---|---|---|
| Overview | empty month; no budgets; no spending; not proven | insights_error caption; pace chart hidden when no budgets | n/a | section retry |
| Review | empty week | not-current caption; nothing-to-compare | all clear; commitment saved | commitment 422 inline |
| Insights | no data; nothing to flag | under 28 days note; model off caption; explain dropped or error | budget applied toast | apply 404 ("This suggestion changed. Refreshed.") |
| Add | no accounts (callout linking to Accounts) | inbox prefill; SMS not a transaction; PDF password; preview reasons; review rows | receipt + Undo; duplicate; imported slip; undone | 422 inline; 413 "File is larger than 10 MB" |
| Ledger | no matches | truncated; statement-row account disabled | row saved | row-level revert + message |
| Budgets | no budgets + copy | near/over stamps | saved | 422 inline |
| Accounts | no accounts; can't reconcile reason | gap slip | balanced stamp | 422 inline |
| Phone | no phones; inbox empty | gateway off; not published; no setup file | test ok; revoked | test failed line |
| Ask | model off; empty thread | notice; unverified; refused steps | reply | model error + Try again; 409 |

### B8. Motion

Tokens:
- `--d-fast` 120 ms, `--d-base` 180 ms, `--d-slow` 240 ms, `--d-exit` 140 ms.
- `--ease-out` cubic-bezier(.2, .8, .2, 1), `--ease-in` cubic-bezier(.4, 0, 1, 1).
- CSS only, no motion library.

Used only for:
- press feedback on buttons: `scale(.98)`, fast;
- hover colour changes: fast;
- dialogs: opacity and `scale(.98→1)`, base;
- sheets: `translateY(100%→0)` over slow with the backdrop fading over base; exits use `--d-exit` and ease-in;
- toasts: `translateY(8px)` plus opacity, base;
- the saved-row tint fade, 600 ms;
- budget bar width, slow, **only when the value changes after an edit** (never a grow-on-load);
- the BALANCED stamp: `scale(1.06→1)` plus opacity over base, **once**, when a slip turns balanced after the user's own action (import or record balance);
- skeleton: a static block with a 1.2 s opacity pulse between .6 and 1.

No page transitions, no parallax, no animated counters.

Reduced motion (`@media (prefers-reduced-motion: reduce)`): every transform transition is removed and every duration token becomes 0.01 ms. The pulse and the stamp do not play. The state is still conveyed by text ("Saved", "Balanced").

### B9. Data visualisation rules

- At most 2 series. No pie or donut charts. Data uses `--c-accent`, reference marks use `--c-muted`, the track uses `--c-sunk`, gridlines use `--c-rule`, and state colours are used only for budget state (always paired with a stamp word).
- **CategoryBars** (HTML):
  - Category label, a bar whose width is `share / maxShare` (drawing maths only), the whole-rupee amount in mono on the right, and "34.1% of money out" muted below. On phone the label sits above the bar.
- **BudgetBar**:
  - 10 px track, fill to `min(percent, 100)`, and a 2 px ink pace tick (at .55 opacity) at `pace%`.
  - Over budget shows a full bar, the OVER stamp, and "over by ₹x" text.
  - `role="img"` with `aria-label` "Food: 78.0% of budget used, 68.0% of month gone".
- **PaceChart** (SVG):
  - x is days, y is rupees. "You" is a solid accent line, 2 px. "Budget pace" is a dashed muted line, 1.5 px, `4 4`.
  - Labels sit directly at the line ends, with no legend.
  - Three y gridlines with compact Indian ticks (₹5K, ₹10K, ₹1L), computed from numbers for drawing only.
  - Pointer and touch show a crosshair and a tooltip (date plus both values as formatted server strings).
  - Accessibility: wrapped in `<figure>`, with a `<figcaption>` summary built from the last day's server values, and a "Show as table" disclosure holding the full data table.
- **Number display:** whole rupees in the hero, cells, charts and budgets. Paise in ledgers, slips, previews and receipts. Always tabular mono, with U+2212 for negatives. Credits always carry "+" plus a word or label, never colour alone.

### B10. Money formatting rule

`lib/money.ts` `formatInr(value: string, {paise=true, sign=false})` is string-only and mirrors `fmt_inr` exactly:
- Strict regex `^-?\d+\.\d{2}$`; anything else throws in development and shows "—" in production.
- Indian grouping.
- `paise=false` *truncates*, mirroring the existing `fmt_inr` behaviour listed in PROGRESS "Not verified". Do not fix it on one side only.
- An all-zero value is never negative.
- `sign` adds "+" only when the value is non-zero and positive.

`web/src/lib/money.cases.json` holds about 25 cases (0, -0.00, 999, 1000, 100000, 12500000.5, negatives, sign, paise=false). It is checked from both sides:
- `tests/test_web_money_cases.py` asserts each case equals Python `fmt_inr` (runs without Node),
- `money.test.ts` (vitest) asserts the TypeScript output matches.

---

## C. Phased build plan (one long session)

**Phase 0: Baseline (about 10 min).**
- Run `cd /home/claude/rectifi && python3 -m pytest -q` and record the real count.
- If Postgres is down: `pg_ctlcluster 16 main start`.
- Read the files listed below.

**Phase 1: Additive backend (about 45 min).**
- Build A3 items 1–6, plus `tests/test_tools_web_additions.py`:
  - `set_account`: ok, unlink, unknown account, statement row refused, missing transaction.
  - `list_transactions(unlinked=True)`.
  - `spending_pace`: values are Decimal, the last pace equals `total_limit` on the last day of a past month, empty for a future month.
  - `preview_pasted_sms` and `save_pasted_sms`: account match, duplicate UPI reference, non-SMS error.
  - `preview_totals`.
  - `chat_events` with ScriptedModel: one tool event, then done; the guard still refuses an invented amount inside the stream.
- Acceptance: all old tests plus the new ones pass.

**Phase 2: API skeleton and security (about 60 min). STOP POINT 1.**
- Write `app.py`, `security.py`, `errors.py`, `wire.py`, and the meta, overview and health routes, plus static serving with a `RECTIFI_WEB_DIST` override.
- Pin `python-multipart==0.0.32` in `requirements.txt`.
- dev.py: `app`/`up`/`demo --streamlit`, the uvicorn command, `web_source_hash`, the `build-web` and `web` stubs, the doctor checks.
- Tests:
  - `test_api_security.py`:
    - bad Host gives 400; a rebinding Host on GET `/api/meta` gives 400;
    - a missing `X-Rectifi-Request` gives 403;
    - a cross-site Origin on POST gives 403; an Origin of `https://laptop.tail1.ts.net` is allowed; `Sec-Fetch-Site: cross-site` gives 403;
    - OPTIONS gives 403 with no ACAO header;
    - a JSON body over 64 KB gives 413;
    - CSP, nosniff and no-store headers are present;
    - importing `rectifi.api.app` in a subprocess does not load `rectifi.agent` or `langchain`.
  - `test_api_static.py`, using a temporary dist with a fake `index.html`: `/ledger` gives index with no-cache, `/assets/x.js` gets immutable, `/api/nope` gives JSON 404, `/../etc` gives 404, a missing dist gives 503 with advice.
  - **`test_api_overview.py` (parity):** seed income, spending, budgets, an account with two balances, and a few transactions. For several months, assert every money string in the response equals `format(tools.spending_summary(...)[k], "f")`, and do the same for `budget_status`, `reconcile` and `spending_pace`. Also: an empty month, an income-only month, and an insights failure (monkeypatch `top_insights` to raise ValueError) giving `insights_error` true and a 200.
  - Update `test_dev_script.py`: `up` without the flag runs uvicorn `rectifi.api.app:create_app` on 127.0.0.1:8501; `up --streamlit` runs Streamlit; a missing dist gives exit 1 with the message.
- Run the full suite, then stop and report the counts.

**Phase 3: The rest of the API (about 90 min). STOP POINT 2.**
- Routes: review, insights, transactions, sms, inbox, imports, budgets, accounts, phone, ask.
- Tests, one file per area:
  - **Insights:** apply uses the server's limit and ignores any client field; a stale key gives 404; explain with a fake LLM covers the text, dropped and error cases.
  - **Transactions:** add; duplicate; an inbox add takes `raw_text` from the database and marks the item added; PATCH with a category-kind mismatch gives 422 with the message; PATCH with `account_id: null` unlinks; DELETE; filters (account=none, category, search, truncated).
  - **Imports:**
    - HDFC CSV fixture auto-imports; uploading it again gives `already_imported`; `manual=1` gives a preview, then commit, then undo.
    - The encrypted PDF gives `pdf_password_needed`; with "demo1234" it imports; with a wrong password it gives `pdf_password_wrong`; and `caplog` plus the PendingUploads contents never contain "demo1234".
    - 10 MB + 1 byte gives 413. An `upload_id` past its TTL gives 409 (monkeypatch the clock).
  - **Phone:**
    - POST devices returns the token exactly once, and GET devices and status never contain it;
    - `setup_file` is null with a reason when there is no gateway URL; with `tailscale_info` monkeypatched to give a name, the file contains the URL and `Bearer`;
    - revoke; inbox dismiss; `/phone/test` with the gateway down returns a non-200 "said".
  - **Ask** (`LLM_PROVIDER=deepseek`, `DEEPSEEK_API_KEY=sk-test`, monkeypatch `graph_module.get_llm` to ScriptedModel, call `ask.reset()` in a fixture):
    - status ok/false;
    - NDJSON gives step and then done, with `unverified==["77777.00"]` for the made-up figure;
    - the history endpoint matches;
    - a non-UUID thread gives 404; a concurrent turn gives 409.
  - **Budgets and accounts:** basic round trips.
- Acceptance: full suite green.

**Phase 4: Web scaffold, shell and Overview (about 90 min). STOP POINT 3 (design review).**
- In `web/`: `npm create vite` (react-ts), then add wouter, @tanstack/react-query, lucide-react, the @fontsource packages and vitest with `--save-exact`.
- Build: `tokens.css`, `base.css`, `fonts.css`, `theme-init.js`, the manifest and icons, `api/client.ts` (headers, ApiError, 503 handling, network handling), the query hooks, the shell and the B6 primitives, `money.ts` plus the cases, and the Overview page.
- Run `python scripts/dev.py build-web` and commit `web/dist`.
- Add `tests/test_web_dist.py`:
  - `index.html` exists and references existing assets;
  - `build-info.json` `source_hash == web_source_hash()`;
  - JS gzipped at most 150 KB, CSS at most 25 KB.
- Add `tests/test_web_e2e.py`, which skips unless the Playwright import works, `web/dist` exists and Chromium launches:
  - a module-scoped uvicorn thread on a free port;
  - the overview test asserts `[data-testid=hero-income|hero-spending|hero-saved]` text equals `fmt_inr(tools.spending_summary(...)[k], paise=False)`;
  - no console errors;
  - at 390 px, `scrollWidth <= innerWidth`;
  - every `button, a, input, select, textarea, [role=tab]` is at least 44 px in both dimensions (skip hidden ones);
  - a keyboard check: Tab reaches the skip link first, then nav, with a visible focus outline;
  - dark mode via `color_scheme="dark"`.
- **Screenshots** (only when `RECTIFI_SCREENSHOTS` is set, to `/tmp/rectifi-web-shots/`): `overview-{390,1280}-{light,dark}.png`, `overview-empty-390-light.png`, `more-sheet-390.png`, `db-down-1280.png` (stop Postgres or monkeypatch), `offline-390.png` (route-abort `/api`).
- Stop, show the screenshots, and get design sign-off before cloning the patterns.

**Phase 5: Capture and proof loop (about 120 min).**
- Build Ledger (table, list and sheet, inline edits, link account), Add (all three tabs, inbox prefill, receipt + Undo, import slip, preview, password), Accounts and Budgets.
- E2E smoke tests, at 390 and 1280:
  - quick entry saves a row in the database, and Undo removes it;
  - pasting the SMS sample saves with the right category;
  - the CSV import shows IMPORTED and the slip, and Undo gives "Import undone";
  - in the ledger, changing a category (`select_option`) is reflected by `tools.get_transaction`, and linking an unlinked entry makes `account_id` non-null;
  - setting a budget is reflected by `budget_status`;
  - recording a second balance shows BALANCED.
- Screenshots: each page at 390 and 1280 in light mode, plus the ledger sheet open, the import preview, and the gap slip.

**Phase 6: Review, Insights, Phone, Ask (about 90 min).**
- E2E:
  - Review saves a commitment;
  - Insights applies a suggested budget (seed three months so the unbudgeted rule fires);
  - Phone pairs a device, the download button is disabled with its reason, and the token text disappears after "Done";
  - Ask with ScriptedModel in-process (same monkeypatches) shows the reply, "Not verified ₹77,777" and the steps disclosure.
- Screenshots for every page at 390 and 1280, light and dark.

**Phase 7: Hardening and docs (about 60 min).**
- Run the states audit against the B7 table: offline banner, reduced motion (`reduced_motion="reduce"` context, check the stamp does not animate), the optional `RECTIFI_TAILSCALE_USER` lock with tests, and a contrast spot-check.
- **Self-critique:** the screenshot test (squint at each 390 and 1280 shot: is the hierarchy obvious? does anything look like a kit default?), then a 5-minute critique, fixing at least 5 concrete issues and listing them in PROGRESS.
- Docs: README ("New web app", "Fallback: `--streamlit`", "Rebuilding the web app needs Node; running it doesn't"), the CLAUDE.md rule 13 plus Map and Commands, and PROGRESS.md (what was built, the real test count, Not verified).
- Final full `pytest -q`.

---

## D. Risks and uncertainty

- **UNSURE: Tailscale Serve's handling of the Host header and X-Forwarded-\* headers, and whether it buffers or times out streaming responses or limits body size.** The Serve docs page does not cover any of these. Mitigations are already in the design:
  - the Origin rule accepts https `*.ts.net`, and Host is checked by hostname only;
  - NDJSON degrades to "everything arrives at the end" and sends 5 s ticks;
  - for the 10 MB upload over Serve, test on the real phone.
- **Verified:** the identity headers, that Serve strips spoofed copies, and that they are not set for tagged devices or Funnel but are set for shared devices.
- **UNSURE: whether ₹ (U+20B9) is covered by IBM Plex Sans and Mono**, and whether `@fontsource-variable/fraunces` ships the opsz axis (it may only ship wght). Phase 4 verifies with `document.fonts.check('16px "IBM Plex Mono"', '₹')` in Playwright. If coverage is missing, render the `₹` span in a font that has it (for example a system font stack); the visual weight changes a little.
- **UNSURE: exact wouter v3 APIs** (`useSearch` / `useSearchParams`, `useLocation`). Check the installed version's docs before use. **UNSURE:** pick Vite 6 or 7 from what installs cleanly on Node 22; record the versions in PROGRESS.
- **Verified in this container:** LangGraph `stream_mode="updates"` yields `{node: update}`. Starlette 1.7 `Request.form(max_files, max_fields, max_part_size)`. **UNSURE:** whether `StreamingResponse` with a sync generator uses the threadpool in 1.7 (it did in 0.x). The queue-plus-thread design avoids depending on it.
- **UNSURE (Windows):** running `npm.cmd` through `subprocess` with a list (expected to work), uvicorn's Ctrl+C with the API as a child (the same pattern as today), and `--reload` on Windows. None of this was tested on Windows; mark it in PROGRESS "Not verified".
- **The `fmt_inr(paise=False)` truncation** is copied into `money.ts` on purpose, for parity. If it is fixed later, fix both sides and `money.cases.json` together.
- **Behaviour change (small, deliberate):** `save_pasted_sms` uses `_sms_account`, which differs from Streamlit in one edge case (described in A3 item 4). Document it in PROGRESS.
- **Committed `web/dist`** can go stale. `test_web_dist.py` and `doctor` catch it. Anyone who edits `web/src` must run `build-web` (needs Node).
- **In-memory caches** (pending uploads, recent imports, Ask locks) assume one uvicorn worker. Never add `--workers`.
- **No passcode in v1** is a conscious trade-off. A lost but unlocked phone that is still on the tailnet has full access, the same as the Streamlit app today. The optional login lock and revoking the device in the Tailscale admin console are the mitigations.
- The Streamlit app's own tests (`test_app.py`) stay. Both UIs share `tools.py`, so the new additive tools must not change Streamlit behaviour, and they don't.

Worth saving to memory for the main session: Rectifi is getting a React + Vite + TypeScript web UI served by a Starlette API in `rectifi/api` on port 8501. The built `web/dist` is committed so the laptop needs only Python. The Streamlit UI stays as the `dev.py up --streamlit` fallback.

### Critical Files for Implementation
- /home/claude/rectifi/rectifi/tools.py
- /home/claude/rectifi/app/streamlit_app.py (behaviour and copy reference for every page)
- /home/claude/rectifi/scripts/dev.py
- /home/claude/rectifi/rectifi/agent/graph.py
- /home/claude/rectifi/rectifi/importer.py
- /home/claude/rectifi/app/theme.py and /home/claude/rectifi/.streamlit/config.toml (existing tokens and allowedHosts to carry over)
- /home/claude/rectifi/tests/conftest.py and /home/claude/rectifi/tests/test_dev_script.py

Sources:
- [Tailscale Serve docs (kb/1312/serve)](https://tailscale.com/kb/1312/serve)
- [Tailscale Serve identity headers](https://tailscale.com/s/serve-headers)