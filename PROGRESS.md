# PROGRESS.md

Last updated: 2026-10-25. Tests: **822 passed, 1 skipped** (`python -m pytest -q`, about 4 minutes; 455 was the count before the web app, the real baseline at its start was 487). Front-end unit tests: 81 (vitest). The 1 skipped test parses the Windows setup script and needs `pwsh` (PowerShell 7) installed.

## Built

Everything below was run and tested on Linux with Postgres 16.

**Core (already there, kept):** Decimal/NUMERIC money, dedupe in the database, reconciliation, SMS and statement import, budgets, the LangGraph agent with the amount guard, a read-only MCP server, the warm-paper Streamlit app.

**Added in this pass**

- **Database fails fast.** If Postgres is down, you wait about 5 seconds and get a friendly "Rectifi can't reach its database" page. It is not cached, so it recovers as soon as Postgres is back. The message never shows the password.
- **Masking.** `rectifi/privacy.py` hides long account, card, UPI-reference and phone numbers (keeps the last 4). The agent only sees masked text. MCP results are masked too.
- **Raw SMS never goes through the model.** For `log_sms`, Rectifi finds the SMS the user really pasted and parses that. If the model sends text the user never pasted, the call is refused.
- **Ask page degrades.** If DeepSeek has no key, or Ollama is off or missing the model, the page says why and the rest of the app works. (`llm_status`)
- **MCP fixes.** Every tool is marked read-only. Percentages show as `27.5%`, not `₹27.50`. New `list_accounts` and `weekly_review` tools.
- **Overview crash fixed** for a month with income but no spending.
- **Packaging.** Pinned `requirements.txt` (with a test that it matches what is installed), `docker-compose.yml`, `db/init.sql`, `.env.example`, `.gitignore`, `scripts/dev.py`, README, CLAUDE.md.
- **Weekly review.** `tools.weekly_review`, `save_commitment`, a Review page (second in the menu), an agent tool, an MCP tool. Compares with the same days last week while the week is still running.
- **Unverified figures.** If the assistant's reply has a ₹ figure that is not in what you typed or in a tool result, the app shows a "Not verified" note under the reply.
- **Category must match direction.** You cannot put money out under an income category (or the reverse). The Ledger shows an error per row.
- **Importer.** A friendly message for `.xls`. Two identical rows on the same day both import, and importing the file again still changes nothing. Old fingerprints still match.
- **Demo data is safe.** `seed_demo.py` refuses unless the database name ends in `_demo`, and does nothing if run twice. `dev.py demo` uses `rectifi_demo` on port 8502.
- **Streamlit hygiene.** `width="stretch"`, budgets compared as Decimals, a notice when the Ledger list is cut at 1000 rows, the app listens on localhost only, the tests refuse to run on a database not ending in `_test`.

**Added in the free-model and Windows pass**

- **More model providers.** `LLM_PROVIDER` now accepts `ollama`, `deepseek`, `openrouter`, `huggingface`, `openai_compat`. The last three use `langchain_openai.ChatOpenAI` (temperature 0, 60 second timeout, 2 retries). Settings are in `rectifi/config.py`; nothing has a guessed default except Hugging Face (`deepseek-ai/DeepSeek-V3.1:cheapest`). OpenRouter has no default model on purpose (the free list changes), and `llm_status` says how to pick one.
- **Honest about cost.** Hugging Face free accounts have $0 credit. `llm_status` does not block it, but the Ask page shows a notice (`llm_notice`), `doctor` warns, and the README says so. OpenRouter shows its 20 a minute / 50 a day limit.
- **Plain error messages.** `explain_error` turns HTTP 401, 402, 404 and 429 from a provider into one sentence of advice on the Ask page. Keys are never printed.
- **`dev.py free-models`.** Lists OpenRouter `:free` models that support tool calling (stdlib `urllib`). The filter is unit-tested on a fixture.
- **`dev.py up` works without Docker.** It skips Docker when Postgres is already reachable. `dev.py doctor` and `setup` accept Python 3.11 and newer (all pinned packages allow it; tests only ever ran on 3.13).
- **`scripts/setup_windows.ps1`.** One command for Windows with PostgreSQL already installed: finds psql, checks the service, creates the login and databases from `db/init.sql` (asks for the `postgres` password with `-AsSecureString`, `PGPASSWORD` cleared in a `finally`, 3 tries), makes `.venv`, installs packages, writes `.env` (UTF-8 no BOM, never overwrites a value you set), helps choose a model (Ollama detection by name prefix, OpenRouter, Hugging Face or skip), then `dev.py init` and `doctor`. Run as `powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1`.
- **Tests.** Every provider's `llm_status` and `get_llm` (base URL, model, temperature, timeout, retries), a fake OpenAI HTTP server that drives the real agent through `ChatOpenAI` (tool call, amount guard and masking still work), the free-models filter, `up` and `doctor` for the new providers, two Ask page tests, and static checks of the PowerShell script.

**Added in the "honest All clear" pass** (found when the app was run on Windows with an empty database)

- **"All clear" is earned.** `tools.weekly_review` now returns `all_clear`, `account_count` and `unlinked_count`. It is true only when at least one account exists, every account has two balances and balances and is fresh, no budget is near or over, nothing is in "Other", and no entry this week is missing an account. With no account, the Needs attention list says "No bank account added yet — Rectifi can't prove nothing is missing until you add one and record its balance". An account with no balance at all says "No bank balance recorded yet".
- **Unlinked entries are counted.** Entries this week with no account can never be reconciled, so the Review page says how many, with a link to the Ledger. The agent and MCP text carry the same count (`entries_not_linked_to_an_account`, `all_clear`).
- **Entries get an account by default.** `tools.default_account()` (the only account, else the most recently used, else the first) preselects the Account picker on Add. "No account" is still there, as the last option. `tools.only_account_id()`: when exactly one account exists, chat-logged entries (`log_transaction`, and `log_sms` when the SMS names no account) are attached in code, not by the model. An SMS that names a different account number is never attached by guess. Dedupe and the amount guard are untouched.
- **No more "Nothing to compare" plus "+₹150.00".** When the earlier period had no spending, the Review page hides the "= change" row and the percentage. The text sent to the agent and MCP says "nothing to compare" instead of a "+" amount.
- **Not done:** the Ledger's Account column is still read-only, so an unlinked entry cannot be fixed there (delete and add again; importing the statement also attaches the account to a matching hand entry, which is how `importer.py` reads, not tried in this pass). An empty week still shows only "Nothing recorded this week yet", without the no-account warning.

**Added in the PDF statements and phone SMS pass**

- **PDF statements (Add > Import statement).** `.pdf` is accepted next to csv/xlsx. `rectifi/pdf_statement.py` reads it with `pdfplumber` (pinned 0.11.10): every table on every page joined into one, the header repeated on each page dropped, blank rows dropped, a narration wrapped inside a cell or onto its own row joined back (`join_wrapped` glues a number cut in two and keeps spaces between words), a summary box ignored, a date printed once per day carried down. The table then goes through the existing column detection, preview, dedupe, merge and reconciliation, unchanged.
- **Text fallback.** If a PDF has no transaction table: lines that start with a date, trailing numbers with two decimals = amount (+ balance). Money in or out is decided by how the balance moved (a newest-first statement is turned around first), else by a Dr/Cr mark on the amount. If neither proves it the row gets `review` and `import_rows` skips it (`ImportResult.needs_review`); the balance check shows it as a gap. The Add page lists those rows with the reason.
- **Friendly errors.** Encrypted with no password, wrong password, scan or photo (no text layer), damaged file, no transactions found. The password field is shown for PDFs ("If your bank's email gave a password for this PDF, enter it"), sits in a form that empties itself, is only passed to pdfplumber, and the session keeps the table but never the password. Nothing in the PDF path imports any model code.
- **Phone SMS gateway.** `rectifi/sms_gateway.py` (Starlette): `GET /health` and `POST /sms` (bearer token, SHA-256 hashes compared with `hmac.compare_digest`, 8 KB limit, tolerant JSON keys and timestamps, 60 messages a minute per phone, wrong-token throttle, 200 for added/duplicate/ignored/needs_review, 401/413/422/429/503 otherwise). `starlette==1.7.0` and `uvicorn==0.54.0` are pinned now. `python scripts/dev.py phone` runs it; `dev.py up` also starts it as a child when `RECTIFI_SMS_GATEWAY=1` and stops it in a `finally`. `doctor` reports it. Off by default.
- **Deterministic SMS handling.** `parsing.classify_sms` + `tools.ingest_sms`: OTP, offers, personal-number senders and non-transactions are ignored (not stored); a clear debit/credit becomes a transaction (source `sms`, account by last 4 digits, else the only account, else unlinked); a bank-looking SMS the rules are not sure about goes to the new `sms_inbox` table ("Needs review"). New tables: `phone_devices`, `sms_inbox`, `sms_seen` (hashes only, so a retried SMS with no UPI reference is still one transaction). `add_transaction` got an internal `_conn` argument so the seen-hash and the transaction commit together.
- **Phone page** (after Accounts): gateway status with the exact `.env` lines and command, pair a phone (token shown once, every value to paste into the Android app, the laptop's private IPv4 addresses with a Tailscale-range address first), a test message, paired phones with last seen and Revoke, the Needs review list (Add pre-fills the Add page, Dismiss), plain-English network notes. iPhone steps are in a collapsed section marked untested.
- **Tests: 111 new** (`test_pdf_import.py`, `test_sms_gateway.py`, `test_phone_page.py`, more in `test_dev_script.py`; `phone` added to the every-page render test). Fake PDFs are committed in `tests/fixtures/`; `make_pdf_fixtures.py` (needs reportlab and pypdf, development only) regenerates them.

**Added in the auto-import, Insights and phone-over-Tailscale pass**

- **A statement upload imports at once.** `importer.assess_statement` decides whether it is sure (date, description and amount columns found, nearly every row readable, an account known). If so, Add > Import statement imports immediately and shows a result card: added, merged, already recorded, needs review with reasons, the account used and why, and the balance check. If not, the old manual preview. A "Let me check the columns..." tick forces the preview. Review rows never import; a statement whose last 4 digits match no account is not put in the default account. A per-session hash of the file bytes stops a Streamlit rerun importing twice.
- **Undo this import.** New table `import_batches` and a nullable `transactions.import_batch_id` (see schema changes below). `import_rows` makes a batch, tags only the rows it inserted, and records which balance snapshots it created or changed. `tools.undo_import(batch_id)` deletes exactly those rows and puts the snapshots back only if nothing changed them since. Merged and duplicate rows are never tagged, so undo never touches them.
- **Insights page** (after Review). `rectifi/insights.py`: eight deterministic rules (data quality first, budget pace from day 5, spike against the 3-month average, recurring payments with new and price-rise flags, large payment, savings-rate trend, unbudgeted categories with a suggested limit, small leaks). Each `Insight` has `kind, severity (info|watch|act), title, detail, numbers, action_label, action_page`; a test checks every rupee figure in the text is in `numbers`. Overview shows "Top 3 suggestions"; Review shows the act-level ones. Agent tool `insights`, read-only MCP tool `insights`. Optional "Explain this" (`rectifi/agent/explain.py`) sends only that insight's masked numbers to the model and drops any reply with a rupee figure not in `numbers`. Designed empty states ("Insights need about a month of data", "Nothing to flag right now").
- **Phone and desktop through Tailscale Serve.** Streamlit and the SMS gateway now bind to `127.0.0.1` (`RECTIFI_SMS_HOST`, default `127.0.0.1`; `0.0.0.0` is an advanced option that prints a warning). `dev.py tailscale` / `tailscale off` / `tailscale status` (list arguments, no shell; finds `tailscale` on PATH or in `C:\Program Files\Tailscale`; reads the machine name from `tailscale status --json`). `rectifi/tailscale.py` (standard library only, loaded by path from `dev.py`). The Phone page shows the HTTPS gateway URL first (`https://<name>:8443/sms`), the LAN URL only when the host is `0.0.0.0`, and has an "Open Rectifi on your phone" card. `dev.py doctor` reports Tailscale and the bind address. `.streamlit/config.toml`: address `127.0.0.1`, XSRF and CORS protections on, `allowedHosts` limited to localhost, 127.0.0.1 and `*.ts.net`. If Tailscale is running when `dev.py up` starts, its `https://<name>` origin is added through `STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS`.
- **Mobile pass at 390 px** (Playwright, Chromium, touch emulation, screenshots in `/tmp/rectifi-mobile`): no page scrolls sideways on Overview, Add (with the import card), Insights, Review or Phone. Fixed in `app/theme.py` (phone breakpoint only, existing tokens): sidebar opener and nav rows 28 px to 44 px, buttons, page links, tabs, text and select inputs, checkboxes and the file-remove button to at least 44 px, the side padding tightened. Tap targets under 44 px went from 12 + 5 + 16 + 9 + 6 to 9 + 1 + 0 + 2 + 1 (what is left is drawn by Streamlit itself, listed below).
- **Docs.** README: "Use Rectifi on your phone", updated SMS steps with the HTTPS gateway URL, the import flow with Undo, "What the Insights page does and doesn't do", "Why there's no 'link by phone number'", troubleshooting rows. CLAUDE.md: rules 10 (insights are deterministic), 11 (bind to localhost) and 12 (auto-import and undo). `.env.example`: `RECTIFI_SMS_HOST`.
- **Tests: 177 new** (278 to 455 passed, same 1 skipped): `test_auto_import.py` (auto-import, undo, low-confidence fallback, rerun idempotency, review rows, account choice, a database made before `import_batches` upgrades without losing rows), `test_insights.py`, `test_insights_page.py`, `test_tailscale.py` (argument builders and DNS parsing from fixture JSON), more in `test_phone_page.py` and `test_dev_script.py`. `test_pdf_import.py` was adapted to the no-Import-button flow.

**Schema changes** (`rectifi/schema.sql`, all `IF NOT EXISTS`, run by `init_db` on start, nothing to do by hand; existing rows are kept): new table `import_batches (id, account_id, filename, created_at, added, merged, duplicates, needs_review, snapshot_changes JSONB, undone_at)`; new column `transactions.import_batch_id INT NULL REFERENCES import_batches(id) ON DELETE SET NULL`; new partial index `idx_txn_batch`. Imports done before this change have `import_batch_id` NULL and cannot be undone. **No change to `requirements.txt`.**

**Data state:** the `rectifi` database is empty (tables exist, nothing in them). `rectifi_demo` has the demo data.

## Web app (React + Starlette API)

Built from `docs/WEB_PLAN.md`, phases 0 to 7. Everything below ran on Linux with Postgres 16 and Chromium.

**What exists**

- `rectifi/api/`: a Starlette JSON API over the existing `tools.py` / `importer.py` / `insights.py` functions (routes: meta, overview, review, insights, transactions, imports, budgets, accounts, phone, ask). Money is a string on the wire; the browser only formats. Ask streams NDJSON (step, tick, done, error). One port, 8501, serves the API and the built app. Middleware, in order: security headers/CSP, Host allow-list, optional Tailscale-login lock (`RECTIFI_TAILSCALE_USER`), API guard (`X-Rectifi-Request`, no CORS, Origin/Sec-Fetch-Site on writes), body limits (64 KB JSON, 10 MB upload).
- `web/`: React 19, Vite 7, TypeScript strict, wouter, TanStack Query 5, CSS Modules with `tokens.css`, lucide-react, native `<dialog>`. Ten routes: Overview, Review, Insights, Add (quick entry, paste SMS, import), Ledger, Budgets, Accounts, Phone, Ask, plus the shell (sidebar at 1280, bottom nav at 390, More sheet). `web/dist` is committed (including woff2 fonts; the CSP allows no `data:` fonts).
- `scripts/dev.py`: `up` serves the web app (`--streamlit` for the old one), `build-web`, `web` (API with reload plus Vite). Still stdlib only, list arguments, no `shell=True`. `doctor` reports whether `web/dist` is built.
- Streamlit still works (`dev.py up --streamlit`); its tests are unchanged and pass (one test, in `test_tailscale.py`, was adapted with `Namespace(streamlit=True)`).
- New pin: `python-multipart==0.0.32` (uploads). starlette and uvicorn pins were already in `requirements.txt`.
- Additive backend changes, each with a test: `tools.py` additions listed in `tests/test_tools_web_additions.py`; `importer.py` and `agent/graph.py` small additions the API needs; `insights.py` grammar fix ("1 month", not "1 months").

**Decisions**

- Baseline measured at the start: 487 passed, 1 skipped (this file said 455).
- NDJSON is an `asyncio.Queue` fed by a worker thread; one running turn per conversation (409 otherwise).
- An import's outcome `skipped` carries the first 5 problems; a preview carries `preview_error` if the table cannot be read.
- `save_pasted_sms` uses `_sms_account` (same account choice as the gateway).
- `money_field` validates with `parse_amount` and names the field, so a bad amount is a field error beside the input, not a generic one.
- `spike:` insights link to the ledger filtered to that category.
- Percentages are shown as the server's one-decimal strings (67.7%), never rounded again in the browser.
- Budget stamps only for NEAR and OVER. The ledger "..." menu opens the same entry sheet as a row tap. Month, week and tab changes push browser history.
- Two doctor tests depend on `web/dist` being built.
- Versions: Vite 7.3.7, plugin-react 5.2.0, TypeScript 5.9.3, vitest 5.0.5. Fonts are our own `@font-face` (the fontsource latin-ext CSS has no `unicode-range`).
- The phone bottom nav hides while a text field is focused, using `visibility` and a 400 ms reveal delay, so the Save tap is not stolen.
- Dark `--c-rule-strong` raised to #7a7260 (contrast was 2.93:1 on raised surfaces; now tested).
- The reconciliation slip puts the "adds up N entries" count on its own line.
- The Ask thread id is in `localStorage` (`rectifi.ask.thread`); the PDF password and pairing token are component state only.

**Phase 7 self-critique** (found by looking at the screenshots in `/tmp/rectifi-web-shots/`, all fixed)

1. The phone bottom nav reappeared under the finger and swallowed the quick-entry Save tap.
2. The ledger Account select was cut to "Demo S..." (column widths rebalanced).
3. The Budgets summary ended in a dangling "·" with its figures on another line (one eyebrow now).
4. The page subtitle crowded the account heading on a phone (compact header lost its bottom margin).
5. The import preview ran "In" and "Balance" together and wrapped dates.
6. Review on a phone: sections had no gap and Needs attention touched the rule above it.
7. The hero label "SAVINGS RATE" wrapped and pushed its figure out of line (subgrid).
8. Empty-state buttons touched the text and each other, and the Disclosure chevron never rotated (literal class names inside CSS modules never match).
9. Dark budget and category bar tracks were nearly invisible.
10. Dark rule-strong contrast was 2.93:1.
11. The Ask header controls wrapped under the subtitle.
12. Insights would have crashed on the server's "info" severity (client said "note").
13. The Review numeral used the mono Money style instead of the display serif.
14. The slip's "(43 entries)" sat on the debits line though it counts credits too.
15. A bad quick-entry amount showed only a generic error.
16. On a phone the Phone page stamp and text were squeezed side by side.
17. Quick-entry: the Note input stretched and the Type control was misaligned.
18. The long import help pushed the file drop down on a phone (now a Disclosure).
19. Grammar: "last 1 months with data".

**Tests added:** API tests per area (`test_api_*.py`, including 7 Tailscale-lock tests), `test_web_money_cases.py` (browser and Python format money identically), `test_web_dist.py`, `test_web_contrast.py`, and three Playwright suites (`test_web_e2e.py`, `test_web_e2e_capture.py`, `test_web_e2e_pages.py`) that prove what the browser did by reading the database. They skip themselves without Playwright, Chromium or `web/dist`.

**Not verified, web app**

- Windows: `npm.cmd`, `dev.py web`/`build-web`, uvicorn reload and Ctrl+C, the 400 ms nav reveal on real touch.
- A real phone or iPhone; only Chromium at 390 px with touch emulation. No Safari or Firefox.
- Tailscale Serve: whether the Host header survives, whether it buffers the NDJSON stream (Ask would then show no live steps until the end), a 10 MB upload through it, and the Tailscale-User-Login lock on a real tailnet. The lock is tested with headers only.
- A real LLM provider: Ask was tested with a scripted model.
- Real bank statements and the real SMS forwarder app, as in the items below.
- Google Fonts are blocked in the sandbox; the app uses local font files, so this does not matter, but older shots in `/tmp/rectifi-mobile` used fallback fonts.
- `web/dist` was built here with Node 22.22.0; Node 20.19+ is required to rebuild and only 22 was tried.

## Why

The audit found three kinds of problems: the privacy promise (no account-number masking), things that crash or hang a new user (database down, no Ollama, income-only month), and a project nobody else could run (no README, no pinned packages, no database setup). The weekly review is the first feature that makes someone come back each week, which is the point of a tracker.

## Not verified. Be careful here.

- **Tailscale, never run.** `tailscale serve --bg --https=443 http://127.0.0.1:8501`, `... --https=8443 http://127.0.0.1:8765`, `tailscale serve --https=<port> off`, `tailscale serve reset` and `tailscale status --json` (Self.DNSName) are written from Tailscale's documentation. Nothing here ran a real Tailscale, on Linux or Windows. The two JSON fixtures in `tests/fixtures/` are written from the documented shape, not captured from an install: if the real `status --json` differs, `dns_name` and `read_served_ports` are what to fix. HTTPS Certificates must be switched on in the admin console once. The Windows install path, `tailscale.exe` on PATH and how the Tailscale app behaves on the hostel network are untried. Whether Tailscale Serve keeps the browser's Host header is not confirmed (see the proxy note below).
- **Behind a proxy, only simulated.** A small local reverse proxy (not Tailscale) in front of a real Streamlit, with a real Chromium using a fake `*.ts.net` name over plain HTTP: with the Host header kept, the page, the websocket and a file upload (XSRF on) work with the config as shipped. With the Host rewritten to `127.0.0.1:8501`, Streamlit answers 403 to the websocket unless the browser's origin is in `server.corsAllowedOrigins`; with it added through `STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS` it works, and an unrelated origin is still refused. `dev.py up` sets that variable only if Tailscale is running at that moment. The real thing uses `https://`, port 443 and a real certificate, none of which were simulated.
- **Phone and Windows for this pass.** Nothing ran on Windows or on the real Android phone: not `dev.py tailscale`, not the phone opening the app, not the SMS forwarder posting to the `https://...:8443/sms` address. The mobile layout was checked in Chromium at 390 px with touch emulation, not on a real phone. Google Fonts were blocked in the sandbox, so the screenshots use fallback fonts and line breaks may differ a little. Dark mode was not looked at at 390 px.
- **Tap targets left under 44 px**, all drawn by Streamlit: the chart hover toolbar (24 px), the small remove "x" on an uploaded file chip, the "?" help icon, the arrow inside a select box (the whole box is 44 px and opens it), one inline link in the Phone page text. Not overridden, to avoid fighting Streamlit's own markup.
- **Auto-import on real statements.** The confidence rules were tried on the fake HDFC-style CSV and the fake PDFs only. A real bank file can be judged "not sure" and fall back to the preview (safe), or, less likely, be judged sure with a wrong date format. The preview still exists for both, and Undo covers a wrong import. Undo of a very old batch after later imports changed the same balance snapshots leaves those snapshots alone (tested), so the balance check may then still show the imported opening and closing balances.
- **Insights on real data.** Thresholds are rules of thumb tested on small fake data. "Explain this" was only tested with a fake model; no real Ollama or cloud model was asked.
- **Phone SMS capture with a real phone.** Never done. The gateway was tested with Starlette's TestClient, over a real local socket (uvicorn in a thread), with curl, and through `dev.py phone` and `dev.py up` on Linux. The "SMS to URL Forwarder" app's real field names, its JSON headers box and what its "Local network mode" does are written from its documented behaviour, not from running it. Whether the app's `%text%` placeholder escapes quotes in an SMS is not known: if it does not, the gateway rescues the exact template the Phone page gives out (tested), but a different template with a quote in the SMS would be refused with 422. Real bank SMS wording varies: only the formats in the tests were tried.
- **iPhone Shortcuts steps.** Untested (marked so on the page and in the README).
- **Windows for the new parts.** `dev.py up` stopping the gateway on Ctrl+C is tested with fake processes and on Linux with a real interrupt, not on Windows. Windows Firewall behaviour, `private_ipv4s()` on a real Windows machine (several adapters, hotspot, Tailscale) and uvicorn's Ctrl+C handling there are untried.
- **PDFs from real banks.** Only the fake PDFs in `tests/fixtures/` were read. Real statements differ (merged cells, one table per page, split headers, Hindi text, unusual date formats). A PDF that imports wrong rows must be caught in the preview. Encrypted fixtures use AES-128 (made with pypdf); RC4 and AES-256 PDFs were not tried.
- **Same statement in two formats.** A CSV and a PDF of the same month spell some narrations differently, so card/NEFT rows (no UPI reference) can import twice. The README says to import each period once, in one format.
- **Browser checks:** the Phone, Add and Insights pages and the Overview and Review pages were looked at in Chromium at 390 px (light mode). The wide layout in a browser, and dark mode anywhere, were not looked at in this pass.
- **`fmt_inr(..., paise=False)` cuts the paise instead of rounding** (₹17,334.90 shows as ₹17,334 in the import preview). It was already like that; not changed.

- **DeepSeek live call.** No API key in the build environment. Thinking-off is set (`extra_body`) and unit-tested, but never sent to the real API. Do one live call on a laptop.
- **Docker.** There is no Docker daemon here. `docker compose config --quiet` passes. The compose file, the `init.sql` run by the container, and `dev.py db` (`docker compose up -d --wait`) were never run end to end.
- **Windows and macOS.** Only Linux was tested. The macOS steps in the README are written from knowledge, not run.
- **`setup_windows.ps1` has never run on Windows.** It was parsed and run under PowerShell 7 on Linux (real psql, a scratch Postgres, a real venv and pip install, the real `dev.py init` and `doctor`; fake `ollama` and `py` programs; the password and menu prompts replaced by scripted answers). Not exercised: Windows PowerShell 5.1 itself, the Windows service check and `Start-Service` (no `Get-Service` on Linux), `Read-Host -AsSecureString` typed by a human, the EDB installer's folders, `py.exe`, a real Ollama. The script avoids PowerShell 7-only syntax and a test checks for it, but that is not the same as running 5.1.
- **OpenRouter, Hugging Face and Groq live calls.** No network to those APIs here. The HTTP shape was tested against a fake OpenAI-style server only. Whether a given free model really supports tools, and whether `deepseek-ai/DeepSeek-V3.1:cheapest` works on a funded account, is untested.
- **Claude Desktop.** The MCP server was tested with the MCP Python client over stdio. The Claude Desktop config was not tried in Claude Desktop.
- **Ollama.** `llm_status` is tested with fakes. No real Ollama was available.
- The Ledger per-row error display is not covered by a UI test (Streamlit's test tool cannot edit a data table). The rule behind it is tested in `tools`.
- Page screenshots were checked by eye for the Review page and Overview in light mode only. Dark mode was not looked at.

## Next

0. Open http://127.0.0.1:8501 on the laptop (`python scripts/dev.py up`), then on the phone through Tailscale, and use the web app for a week; fix what feels wrong.
1. Run the "Not verified" items above on a real laptop, starting with `setup_windows.ps1` on the Windows PC, then one live Ask page question with the chosen model.
2. P2 from the audit: cleaner payees from POS/ATM/NEFT narrations; refunds as negative spending; one typed amount backs only one write; validate account ids; move budget pace into `tools.py`; checkpointer reconnect; console script for the MCP server.
3. On the Windows laptop: install Tailscale on laptop and phone, run `python scripts/dev.py tailscale`, open the https address on the phone, then point the SMS forwarder app at `https://<name>:8443/sms` and fix the steps wherever reality differs. After that: a signature check for the gateway.
3b. Try a real bank statement through the auto-import and Undo; tell me where it fell back to the preview or got a column wrong.
4. Roadmap: RAG categoriser (pgvector), spending forecast, MCP write tools (optional). Details in CLAUDE.md.
