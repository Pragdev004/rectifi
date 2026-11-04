# PROGRESS.md

Last updated: 2026-11-04. Tests on Windows 11, Python 3.12, Postgres 16: **2116 passed, 44 skipped, 6 deselected** (`python -m pytest -q -m "not ollama" --ignore-glob="tests/test_web_e2e*"`, about 25 minutes). Browser and multi-user files, run one at a time: **156 passed, 0 skipped** (`test_rls` 16, `test_isolation_routes` 14, `test_dump_no_plaintext` 5, `test_multiuser_e2e` 4, `test_web_e2e*` 117). Front-end: **196 passed** (vitest, 20 files). The 44 skips need tools or a platform not here (POSIX sandbox limits 31, `dockerfile_parse` 6, `brotli` 2, `hadolint`, `pwsh`, Java, POSIX file modes, one superseded test); the deselected ones need a real Ollama.

Everything built before the multi-user work is in [docs/PROGRESS_HISTORY.md](docs/PROGRESS_HISTORY.md).

## Multi-user (docs/MULTIUSER_PLAN.md, all 7 stages of docs/AGENT_WORKFLOW.md)

- **What it is.** Friends join by invite (`dev.py invite`). Every per-person table has `user_id`, row-level security with FORCE, and an app role. Text fields are sealed with the person's data key (`rx1.`); text that arrives while they are signed out is sealed to their public key (`rs1.`). Dedupe keys are keyed hashes (`b1:`). The owner's old laptop data is encrypted by a sweep when the owner claims the account. AI pages stay owner-only. Rules 18 to 20 in CLAUDE.md.
- **Review and fixes.** A fresh Opus review found 1 HIGH, 5 MED and 8 LOW issues (`docs/handoffs/review-round1.md`). Three fix rounds followed, each re-reviewed (`fix-round1..3.md`, `review-round2..4.md`). The final review found no blocker: no way for one person to read or change another's data, no data loss, no key-handling bug.
- **Fixed:** the owner claim re-keys every dedupe key in the same transaction as the confirm, so an old payment can't be saved twice and the sweep finishes. The name "owner" is reserved. There is no account lock (failures are counted per account and client, with a per-account backoff before hashing, and recovery has its own limit). The password hash cap has three lanes (known device, recovery, everyone else). The gateway checks the token first. `RECTIFI_BLIND_KEY` is a server secret kept only in `.env` (never in the database or its backups); the hashes are remade at the next sign-in if it changes. `RECTIFI_TRUSTED_PROXIES` (default empty), with uvicorn's own proxy headers off on every launch path. No key shows in any `repr` (a meta-test checks every class).
- **Migration.** `init_db` changes only `_test` and `_demo` databases. Your real `rectifi` database needs `dev.py migrate-tenancy` (a typed `yes`). Until then the app refuses to start on it, on purpose (decision D10).

## Known gaps (from the final review; login hardening and edge cases, no data exposure)

- N11: a junk-name flood can push an account's backoff out of the 10,000-entry limiter table, so guesses rise to about 11 per 10,000 junk requests.
- A stranger's flood fills the 4-slot "other" lane: new browsers, sign-up, the owner claim, change password and new recovery code get 429.
- A stranger who keeps failing holds NEW browsers of that account at 429 `login_wait`; a known device or the recovery code gets in.
- An invited person can fill the 2-slot device lane or the 1-slot recovery lane with their own sign-ins.
- The device cookie has no server-side expiry or epoch, survives a password change or recovery, and skips the pre-hash backoff (still 10 misses per 15 min per pair).
- N9: only the first `X-Forwarded-For` line is read; behind a trusted proxy that adds its own line, the client picks its limiter address.
- What Tailscale Serve and Funnel put in `X-Forwarded-For` is unconfirmed (it matters only with `RECTIFI_TRUSTED_PROXIES` set).
- N6: each login runs `purge_unconfirmed` and a user lookup before any limiter, on a 5-connection pool.
- A claim expiring during the owner re-key makes sign-in purges wait on row 1; a double-submitted confirm answers `signup_expired`.
- N10: the repr meta-test is vacuous for NamedTuple and attrs and skips modules that fail to import without a word.
- The global sign-up limit (10 per hour, failed tries count) lets anyone block every sign-up, the owner claim too.
- Before the owner claim, the owner's phone gets an error instead of 503 with Retry-After.
- `/api/recover` timing differs a little between real and unknown names.
- The session cookie is `Secure` only in public mode (`RECTIFI_PUBLIC=1`, which the Funnel steps set).
- The last person's key stays on an idle pooled connection's row factory until the next checkout (memory hygiene only).
- Friends can call `/api/phone/status` (the server's MagicDNS name and published ports).
- **Owner account delete leaves the LangGraph chat memories and unlisted thread checkpoints**, so PRIVACY.md's "every row" claim is not true for the owner. Only the operator can issue a new owner invite. Fix this before a server ever changes hands.
- N3: the signed-out alerts sweep raises `VaultLocked` on an unsealed custom-merchant name, so the owner's alerts wait for the seal sweep.
- N5 (suspected): the SMS inbox digest day uses the DB timezone in the re-blind and the process timezone at ingest; a same-day resend after a key change can land in the inbox twice.
- An SMS with no UPI reference, resent during a blind-key change before that person signs in, can be added twice (nothing lost).
- In a sweep dedupe clash, the dropped duplicate's `note` is not carried over (unreachable since the re-key became atomic).
- Docs: `docs/AI_PLAN.md:832` and `docs/WEB_PLAN.md:244` show uvicorn without `--no-proxy-headers`; `docs/HOSTING.md` is 334 lines (over the 200-line limit for docs).

## Why

The audit found three kinds of problems: the privacy promise (no account-number masking), things that crash or hang a new user (database down, no Ollama, income-only month), and a project nobody else could run (no README, no pinned packages, no database setup). The weekly review is the first feature that makes someone come back each week, which is the point of a tracker.

## Not verified. Be careful here.

- **Multi-user, never run outside tests.** No Docker, Oracle VM, Tailscale Funnel or real phone. The owner claim, the seal sweep and `migrate-tenancy` have never run on the real database (decision D10). No load test of the login limits under real concurrency. The crypto composition is standard primitives from `cryptography`, combined by us; no outside expert has reviewed it.

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

-1. Before using multi-user on the laptop: Prag tests `migrate-tenancy` on a restored copy of the real database (row counts before and after), then decides when to run it on `rectifi`. Back up `RECTIFI_BLIND_KEY` apart from the database backups.

0. Open http://127.0.0.1:8501 on the laptop (`python scripts/dev.py up`), then on the phone through Tailscale, and use the web app for a week; fix what feels wrong.
1. Run the "Not verified" items above on a real laptop, starting with `setup_windows.ps1` on the Windows PC, then one live Ask page question with the chosen model.
2. P2 from the audit: cleaner payees from POS/ATM/NEFT narrations; refunds as negative spending; one typed amount backs only one write; validate account ids; move budget pace into `tools.py`; checkpointer reconnect; console script for the MCP server.
3. On the Windows laptop: install Tailscale on laptop and phone, run `python scripts/dev.py tailscale`, open the https address on the phone, then point the SMS forwarder app at `https://<name>:8443/sms` and fix the steps wherever reality differs. After that: a signature check for the gateway.
3b. Try a real bank statement through the auto-import and Undo; tell me where it fell back to the preview or got a column wrong.
4. Roadmap: RAG categoriser (pgvector), spending forecast, MCP write tools (optional). Details in CLAUDE.md.
