# CLAUDE.md

Notes for any AI assistant (or human) changing Rectifi. Read this first. Then read PROGRESS.md.

## What this is

Rectifi is a local-first personal expense tracker. Python 3.13, Postgres 16 (psycopg 3, raw SQL, no ORM), a LangChain/LangGraph stack that runs on Ollama by default (OpenRouter, Hugging Face, DeepSeek or any OpenAI-compatible service are optional): an import classifier, a chart-code harness, a CRAG + Self-RAG library, and a chat with memory and traces. A web app (React + a Starlette JSON API, one port; free hosting with a login is in `docs/HOSTING.md`), the old Streamlit app as a fallback, and a read-only MCP server.

## The rules. Never break these.

1. **The LLM never decides a number.** An amount the agent passes to a write tool must appear in the user's last 3 messages. This is `graph._guard`. Do not weaken it. If you add a write tool, add it to `_guard` and write a test.
2. **Money is `Decimal` / `NUMERIC`. Never `float`.** Floats are allowed only to draw a chart or a bar. Never to add, compare or store money.
3. **Dedupe lives in the database.** Partial unique indexes in `schema.sql` plus `ON CONFLICT DO NOTHING`. App code may check first, but the database is the real guard.
4. **Completeness is proven by reconciliation** (`tools.reconcile`), not claimed.
5. **One deterministic tools layer: `rectifi/tools.py`.** It has two front doors: the agent (`rectifi/agent/tools.py`) and the read-only MCP server (`rectifi/mcp_server.py`). The Streamlit app calls `tools.py` too. No money maths anywhere else. Nothing in `tools.py` talks to an LLM.
   - **Addendum (charts).** Chart code the model writes may add up the prepared frame's **integer paise** to draw and to label, inside the sandbox. A rupee figure in a chart's text must trace to something the harness can see: a plotted value, a series sum, or a `tools.chart_reference_totals` figure. The validator refuses a typed money amount in the code; `charts/checks.py` checks the figure's text after it is drawn. A pinned chart is redrawn from its saved code with no model call.
6. **Local-first, and mask before you send.** Account, card, UPI-reference and phone numbers go through `privacy.mask_sensitive` before anything reaches a model. The agent never carries raw SMS text: `graph._original_sms` swaps the user's real text back in on the way to `log_sms`.
7. **The MCP server is read-only.** Every tool has `ToolAnnotations(read_only_hint=True, ...)`. Do not add a write tool without the user asking.
8. **The phone SMS gateway is deterministic, token-hashed and off by default.**
   - No model ever sees an SMS or a PDF. `sms_gateway.py`, `pdf_statement.py` and `importer.py` must not import `rectifi.agent` or langchain (tests check this). An SMS is decided by `parsing.classify_sms` and written by `tools.ingest_sms`: fixed rules only.
   - Device tokens are stored only as SHA-256 hashes (`phone_devices.token_hash`). The plaintext exists once, in the return value of `tools.create_device`. Never log or print it, never log SMS text. Compare with `hmac.compare_digest`.
   - Off by default: it runs only with `RECTIFI_SMS_GATEWAY=1` (`dev.py up`) or `dev.py phone`. It listens on `127.0.0.1` (see rule 11); the phone reaches it through Tailscale Serve, and the token is a second lock. Do not add a route that returns data. `/health` returns `{"ok": true}` and nothing else.
   - Answer `200` for added / duplicate / ignored / needs_review so the phone stops retrying. Errors (401, 413, 422, 429, 503) are for things the phone should retry or fix.
   - Auto-capture is stricter than the Paste SMS box: OTP, offers and plain phone-number senders are ignored; an SMS the rules are not sure about goes to `sms_inbox` ("Needs review"), never guessed. Dedupe is in the database (`uq_txn_upi_ref`, `sms_seen`, `uq_sms_inbox_hash`).
   - **Addendum (knowledge uploads).** The Library accepts `.md` and `.txt` notes only. A file that `importer.assess_statement` recognises as a bank statement is refused there, and no PDF is accepted. So "no model ever sees an SMS or a PDF" still holds for bank data: the only text a model reads from a file is a note you chose to add (masked first). Widening this (for example reading PDFs in the Library) needs the user's OK first.
9. **A PDF row whose direction is not proven is never imported by guess.** `pdf_statement.decide_directions` uses the balance movement, then a Dr/Cr mark, otherwise the row gets `review` and `import_rows` skips it. The balance check then shows the gap.
10. **Insights are deterministic.** `rectifi/insights.py` is fixed rules, SQL and `Decimal`. It must not import `rectifi.agent` or langchain, and a test checks that. Every rupee figure in an insight's `title`, `detail` or `action_label` must also be in `Insight.numbers` (a test enforces it). The agent tool and the MCP tool only repeat what `insights()` returned. The one place a model touches an insight is the optional "Explain this" (`rectifi/agent/explain.py`): it gets only that insight's masked numbers, and a reply with a ₹ figure that is not in `numbers` is dropped. The page must work with no model. New insight rules go in `insights.py` with a test; thresholds go in the tunables block at the top.
11. **Bind to localhost; reach the phone through Tailscale Serve.** Streamlit (`--server.address 127.0.0.1`, also in `.streamlit/config.toml`) and the SMS gateway (`config.sms_host()`, default `127.0.0.1`) never listen on `0.0.0.0` unless the user sets `RECTIFI_SMS_HOST=0.0.0.0` (advanced; it prints a warning and the Phone page shows the LAN URL only then). Do not hard-code `0.0.0.0`. Keep `enableXsrfProtection` and `enableCORS` on. `rectifi/tailscale.py` is standard library only and builds command lists for `subprocess` (never `shell=True`); `dev.py` loads it by path. Do not weaken `allowedHosts` to `*`.
   - **Hosting (rule 11 addendum).** `RECTIFI_BIND_HOST` defaults to `127.0.0.1`; only the Docker image sets `0.0.0.0`, and compose publishes the port as `127.0.0.1:8501:8501` (a test checks it). When the bind host is not loopback or `RECTIFI_PUBLIC=1`, `create_app` refuses to start (`StartupRefused`) without `RECTIFI_PASSWORD_HASH` and `RECTIFI_SESSION_SECRET`. The login is `api/auth.py` (scrypt, a signed cookie, a limiter) and `security.SessionLock`, which `create_app` installs and which also sets `request.state.uid` (the memory namespace; always `"owner"` today). `dev.py set-password` makes the two values.
12. **A statement upload imports at once only when `importer.assess_statement` is sure,** and every import is one `import_batches` row so it can be undone. Undo (`tools.undo_import`) deletes only rows whose `import_batch_id` is that batch and restores balance snapshots only if nothing changed them since. Rows that were merged or already there are never tagged. Review rows never import. A statement whose last 4 digits match no account is never put in the default account by guess.

13. **The web app is a thin layer: the browser only formats, the API only calls `tools.py`.**
   - **No money maths outside `rectifi/*.py`.** Money crosses `/api` as strings (`"1250.50"`), percentages as one-decimal strings. `web/src` never adds, subtracts, compares or rounds money. It formats (`web/src/lib/money.ts` mirrors `fmt_inr`, and `tests/test_web_money_cases.py` proves both give the same text from one shared case file).
   - **`rectifi/api/*` is a wrapper.** A route validates input, calls one existing function in `tools`/`importer`/`insights`, and shapes the answer (`api/wire.py`). Behaviour changes belong in `tools.py` with a test. Importing `rectifi.api` must not load langchain (a test checks); the model is imported only inside the Ask and Explain routes.
   - **The guards stay.** `api/security.py`: Host allow-list (DNS rebinding), `X-Rectifi-Request` on every `/api` call, no CORS ever (OPTIONS is 403), Origin and Sec-Fetch-Site checks on writes, 64 KB JSON and 10 MB upload limits, the optional `RECTIFI_TAILSCALE_USER` lock, CSP with no inline script or style and `connect-src 'self'`. Do not loosen any of them, do not add an `Access-Control-*` header, do not bind to `0.0.0.0`.
   - **One worker.** Uploads waiting for a column check, recent imports and the Ask locks live in memory. Run one uvicorn worker.
   - **A PDF password and a pairing token** are held in component state only: not in the URL, storage or the query cache, never logged.
   - **`web/dist` is committed**, so the laptop needs only Python. After any change under `web/src`, `web/public`, `package*.json`, `index.html` or `vite.config.ts` run `python scripts/dev.py build-web` and commit `web/dist`. `tests/test_web_dist.py` fails when the build is older than the sources. Node is needed only to rebuild.
   - **The Streamlit app keeps working** (`dev.py up --streamlit`) and its tests keep passing. Do not delete it.
   - No fake data or stub endpoints in shipped code; mocks live in tests. Fonts are files in `dist` (the CSP allows no `data:` fonts).

14. **Classification never touches money fields.** It may set only a row's category and the `category_source / _confidence / _reason / suggested_*` columns. Never amount, direction, date, account or any dedupe key (a test snapshots those). A row with `category_source = 'user'` is never overwritten. The layers run in this order and the first sure one wins: your correction, a rule you taught, a built-in merchant rule, a word heuristic, a look-alike past payment (layers 1 to 4 are deterministic: `rectifi/categorize.py`, standard library only, no langchain). Only after the import, and only for rows still in Miscellaneous, `agent/classify.py` asks the local model, through `agent/jobs.py`, and writes through `tools.apply_classifications`. The model's category list is an enum limited to the right kind (a credit never gets an expense category). "Cash Withdrawal" is an expense on purpose. Built-in rules of four letters or fewer match at the start of a word. Settings: `RECTIFI_AUTO_CLASSIFY`, `RECTIFI_CLASSIFY_APPLY_MIN` (80), `RECTIFI_CLASSIFY_SUGGEST_MIN` (50), `RECTIFI_CLASSIFY_BATCH`.
15. **Chart code is untrusted.** Model-written code runs only after `charts.validate` (an AST allow-list), only inside `charts.sandbox` (a child process with a scrubbed environment, no database URL, no network libraries, CPU, memory and wall-clock limits, a temp folder it owns), and what comes back is images and JSON that get checked (PNG signature, sizes, the figure's text), never a pickle. Threat model, so nobody over-trusts it: audit hooks are **not** a security boundary; native code is not network-sandboxed; the Windows Job Object path has never run on Windows; `RLIMIT_AS` is unreliable on macOS; code inside the plotting libraries is allowed to import what it needs on purpose. It is a harness against a confused model, not against a determined attacker, which is why the Docker deployment adds a read-only filesystem, no capabilities and no published port. The sandbox needs only a writable temp dir: `usable_cache_dir()` falls back to `/tmp` when the home folder is read-only.
16. **The chat may not invent a number, and memory never supplies one.** Every reply passes `verify_amounts` (the same `unverified_amounts` check as the agent); a ₹ figure that traces to nothing is shown as "Not verified". Long-term memory (`agent/memory.py`) is text the person approved, amounts are redacted from it, and it is never a source for a figure. The knowledge graph's ₹ check is plain code and runs before any model grader. Self-RAG here is prompt-based: a normal small model answers short structured questions. It is not the fine-tuned reflection-token model of the paper, and its checks can be wrong. The README says so; do not remove that line.

## Map

| File | Job |
|---|---|
| `rectifi/tools.py` | All reads and writes of money. Summaries, budgets, reconciliation, weekly review. |
| `rectifi/db.py`, `schema.sql` | Pool (fails fast with `DatabaseUnavailable`), tables, default categories and merchant rules. |
| `rectifi/importer.py` | Statement CSV/Excel/PDF to rows. Dedupe order: UPI ref, fingerprint (with occurrence counter), merge with hand entry. A PDF becomes a table first (`pdf_to_table`), then follows the same path. Rows with `review` set are skipped. |
| `rectifi/pdf_statement.py` | pdfplumber reading: tables of every page, wrapped cells (`join_wrapped`), the text-line fallback and `decide_directions`. Friendly `RectifiError` for password, scan, damaged file, no rows. The password is only passed to pdfplumber. |
| `rectifi/parsing.py` | Regex parsers for SMS and narrations. `classify_sms` decides transaction / review / ignored for forwarded SMS. `best_effort_sms` pre-fills the Add form. |
| `rectifi/sms_gateway.py` | Starlette app for the phone (`GET /health`, `POST /sms`), rate limits, `private_ipv4s`, `gateway_reachable`, `send_test_sms`. Started by `dev.py phone` / `dev.py up`. |
| `rectifi/insights.py` | The Insights rules. Returns `Insight(kind, severity, title, detail, numbers, action_label, action_page)`. Deterministic. |
| `rectifi/agent/explain.py` | Optional "Explain this" for one insight; checks every ₹ figure against `numbers`. |
| `rectifi/tailscale.py` | Tailscale Serve command builders, DNS-name parsing, status reading. Standard library only. |
| `rectifi/privacy.py` | `mask_sensitive`. |
| `rectifi/money.py` | `parse_amount`, `fmt_inr`, `amounts_in_text` (the guard uses this). |
| `rectifi/agent/graph.py` | LangGraph: agent node, guarded tool node, masking, `unverified_amounts`. |
| `rectifi/agent/tools.py` | Tools the model may call. Thin wrappers over `tools.py`. |
| `rectifi/agent/llm.py` | `get_llm(role)` (roles: chat, classify, code, grade, each with its own `RECTIFI_MODEL_*`), `get_embeddings`, `llm_status(role)`, `describe(role)`, `probe` (a real round trip that says which step failed: reachable, model, capabilities, chat, tools), `llm_notice` (warnings such as Hugging Face needing credits), `explain_error` (HTTP 401/402/404/429, timeouts, text-written tool calls in plain words). Providers: `ollama`, `deepseek`, and `openrouter` / `huggingface` / `openai_compat` through `langchain_openai.ChatOpenAI`. Settings are in `rectifi/config.py`. Masking and the amount guard live in `graph.py`, so they are the same for every provider. |
| `rectifi/categorize.py`, `rectifi/textvec.py` | Layers 1 to 4 of classification and the look-alike index. Pure: standard library only. Names of the catch-all categories live here. |
| `rectifi/agent/classify.py`, `rectifi/agent/jobs.py` | The model pass after an import (layer 5) and the one background worker that runs and resumes those jobs. `app.py` calls `requeue_unfinished()` at start. |
| `rectifi/charts/` | Chart harness: `validate.py` (AST allow-list), `sandbox.py` + `_child.py` (the limited subprocess), `checks.py` (the figure), `theme.py` (Passbook colours, fonts in `fonts/`), `run.py`, `prompts.py`, `graph.py` (write, validate, review, run, check, repair; the only module here that loads langchain). |
| `rectifi/rag/` | Library: `loaders.py`, `index.py`, `store.py` (`PgVectorStore`, vectors in Postgres, no extension), `retrieve.py` (hybrid, MMR, multi-query, refine), `graders.py`, `graph.py` (CRAG + Self-RAG), `web.py` (optional, off), `embeddings.py`. Guides are in `rectifi/knowledge/*.md` and contain no rupee figures. |
| `rectifi/agent/supervisor.py`, `memory.py`, `checkpoint.py`, `tracing.py`, `contracts.py` | The chat: one graph that routes to the spending agent, knowledge or chart subgraph; short-term trimming and summary, long-term memory in a `PostgresStore`, the pooled `PostgresSaver`, local trace spans (LangSmith only if `RECTIFI_LANGSMITH=1`). `contracts.py` holds the shapes the subgraphs share. |
| `rectifi/evals/` | `python -m rectifi.evals classify|charts|rag [--live]` (also `dev.py eval`). Scripted runs are in pytest; `--live` needs your Ollama. |
| `rectifi/mcp_server.py` | Read-only MCP server. `_json` masks and formats. Percentages must be turned into text before `_json`. |
| `rectifi/api/` | The web app's server (Starlette): `app.py` (routes, static files, start-up work), `security.py` (guards, `SessionLock`), `auth.py` (password login), `errors.py`, `wire.py` (shapes, money as text), `inputs.py`, `ask.py` (the chat turn, NDJSON, interrupts), `uploads.py`, `routes/*` (one file per page). Thin over `tools.py`. |
| `web/src/` | The browser app: React 19, Vite, TypeScript strict, wouter, TanStack Query, CSS Modules. `lib/money.ts` formats money. `pages/` one file per page (Overview, Review, Insights, Ask, Add, Ledger, Charts, Budgets, Accounts, Phone, Library, Memory, Traces, Login), `components/` shared pieces (`rag/` Citations, ReflectionTrace, WebConsent; `ModelCheck`). `web/dist` is the committed build. |
| `app/streamlit_app.py` | The old Streamlit app, the `--streamlit` fallback. Pages: Overview, Review, Insights, Ask, Add, Ledger, Budgets, Accounts, Phone. |
| `app/theme.py` | Design tokens and CSS classes. |
| `scripts/dev.py`, `scripts/devtools/` | Stdlib-only helper: setup, db, init, doctor (`--quick` skips the model round trip), app, up, build-web, web, phone, tailscale (on, off, status), demo, free-models, test, mcp, mcp-config, and the devtools commands `llm-check`, `ollama-setup`, `classify`, `eval` (`devtools/models.py`) and `set-password`, `deploy-check` (`devtools/deploy.py`). A devtools command gets its raw arguments (`dev.py` hands over `argv[2:]` before argparse runs), so `--dry-run` and `--print` just work; add a new one to that module's `COMMANDS`. `up` skips Docker when Postgres is already reachable, and also starts the gateway as a child process when `RECTIFI_SMS_GATEWAY=1` (stopped in a `finally`, so Ctrl+C cleans up on Windows too). |
| `deploy/`, `docs/HOSTING.md` | Free hosting on an Oracle Always Free VM: `Dockerfile`, `compose.oracle.yml` (read-only root, tmpfs `/tmp`, port on 127.0.0.1 only, daily backups), `cloud-init.yaml`, `Modelfile.classify` / `Modelfile.chartcoder`. |
| `scripts/setup_windows.ps1` | One-command Windows setup for a native PostgreSQL (psql, init.sql, .venv, .env, model choice, init, doctor). Plain ASCII, PowerShell 5.1 syntax only. Guarded by `tests/test_setup_windows.py`. |
| `scripts/seed_demo.py` | Fake data. Only into a database ending in `_demo`. |

## Commands

```
python scripts/dev.py doctor        # is everything ready? (--quick skips the model round trip)
python scripts/dev.py ollama-setup  # pull a model set that fits this laptop's RAM and create rectifi-classify / rectifi-chartcoder (--tier, --dry-run)
python scripts/dev.py llm-check     # real model round trip (reachable, model, tools); --quick for no network
python scripts/dev.py classify      # sort entries still in Miscellaneous with the local model
python scripts/dev.py eval classify # or charts, rag (add --live for your real models; classify also runs offline)
python scripts/dev.py set-password  # make RECTIFI_PASSWORD_HASH and RECTIFI_SESSION_SECRET for hosting
python scripts/dev.py deploy-check  # check the hosting files and .env.deploy
python scripts/dev.py test          # or: python -m pytest -q
python scripts/dev.py up            # db + tables + web app on :8501 (+ phone gateway if RECTIFI_SMS_GATEWAY=1); add --streamlit for the old app
python scripts/dev.py build-web     # rebuild web/dist (needs Node 20.19+; commit the result)
python scripts/dev.py web           # develop the web app: API with reload on :8501, Vite on http://127.0.0.1:5173
python scripts/dev.py phone         # only the phone SMS gateway, port RECTIFI_SMS_PORT (default 8765), on 127.0.0.1
python scripts/dev.py tailscale     # publish the app (https :443) and the gateway (https :8443) on your tailnet; `tailscale off` undoes it
python scripts/dev.py demo          # fake data on :8502 (database rectifi_demo)
python scripts/dev.py mcp-config    # JSON for Claude Desktop
python scripts/dev.py free-models   # OpenRouter :free models that support tools (needs internet)
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1   # Windows, PostgreSQL already installed
```

Tests need Postgres with a database whose name ends in `_test` (default `rectifi_test`). Each test drops all tables. `conftest.py` refuses to run on any other name. There is no API key and no Ollama in tests: the agent is driven by `ScriptedModel` in `tests/test_agent.py`.

## Conventions

- Plain English in docs and UI text. Short sentences. The reader is a second-year student.
- Every new behaviour gets a test. Do not delete existing tests.
- New test files use the autouse `fresh_db` fixture. Async MCP calls go through `asyncio.run(...)` inside normal tests. We do not use pytest-asyncio.
- Tests set `RECTIFI_NO_AUTO_REINDEX=1` (conftest), so starting the app or importing a file does not start an embedding thread. A test that wants it unsets the variable.
- Real graphs, scripted models: `tests/test_chat_real_subgraphs.py` and `tests/test_chat_api.py` build the real knowledge and chart subgraphs through `agent/contracts.py`; only models and embeddings are fakes (`tests/rag_support.py::RuleModel`, `tests/support/fakes.py`). `tests/support/chat_fakes.py` has the quick fake subgraphs for pure supervisor unit tests.
- Interrupt words the chat window switches on: `memory`, `web_consent` (the RAG graph says `type: "web_search"`; `api/ask.interrupt_kind` maps it), `review_code`. Resume values are booleans (the chart review gate also accepts `{"run": true, "code": "..."}`).
- Browser tests (`tests/test_web_e2e*.py`) drive Chromium through Playwright against a real server in a thread of the test process (`tests/web_support.py`) and prove what the browser did by reading the database. They skip themselves without Playwright, Chromium or `web/dist`. `RECTIFI_SCREENSHOTS=1` saves screenshots to `/tmp/rectifi-web-shots/`; `python web/scripts/screenshots.py` makes the full matrix on scratch `_demo` databases. Front-end unit tests: `cd web && npm test`.
- To test a Streamlit page, use `run_page("<url_path>")` from `tests/test_app.py` (it uses a private AppTest field; fix it there if Streamlit changes).
- UI: reuse the classes in `app/theme.py` (`.hero`, `.chip`, `.ledger`, `.recon`, `.empty`, `.eyebrow`, `.brow`). Do not add a colour palette. Colour is never the only signal: a chip always has a word.
- Do not wrap `st.navigation(...).run()` in a bare `except Exception`. Catch only `DatabaseUnavailable`, `psycopg.OperationalError`, `PoolTimeout`.
- No `shell=True`. No `print` on the MCP server path (stdout is the protocol). Never print a password or an API key.
- The agent needs a model with tool calling. A new provider goes in `config.py` + `llm.py` (`get_llm`, `llm_status`, `describe`), `dev.py doctor`, `.env.example`, the README table, and gets tests in `tests/test_llm.py` (there is a fake OpenAI server in that file for end-to-end checks).
- `setup_windows.ps1` must stay ASCII (Windows PowerShell 5.1 reads BOM-less files as ANSI), must not use PowerShell 7 syntax, must write `.env` without a BOM, and must clear `PGPASSWORD` in a `finally`.
- `requirements.txt` is pinned. `tests/test_requirements.py` fails if it drifts from what is installed. Change both together.
- Do not put real bank files in the repo. `.gitignore` blocks csv/xls/xlsx/pdf outside `tests/fixtures/`.
- Test PDFs in `tests/fixtures/` are fake and committed. `tests/fixtures/make_pdf_fixtures.py` makes them; it needs reportlab and pypdf, which are development-only and NOT in `requirements.txt`. The app itself needs only `pdfplumber`.
- `starlette` and `uvicorn` are direct dependencies now (the gateway), so they are pinned in `requirements.txt` too.

## Deferred (do not build unless asked)

1. ~~RAG categoriser with pgvector~~ Replaced: the classifier uses a look-alike index of your past labels (`textvec.py`, no extension) and the local model. The Library stores vectors in plain Postgres (`rag_chunks`), also without pgvector. Plotly charts (`RECTIFI_CHARTS_PLOTLY=1` is read by the validator but plotly is not installed or shown), receipt photos (vision) and GitHub login are not built.
2. Spending forecast (deterministic baseline first; learned model only if it beats it in backtests; needs 6+ months of data).
3. MCP write tools (would need the same amount guard plus confirmation).
4. Own Android capture app (the "SMS to URL Forwarder" app does the job for now), an HMAC signature check, an iPhone flow that has actually been tested.
5. From the audit, P2: cleaner payees from POS/ATM/NEFT narrations; refunds as negative spending; one typed amount backs one write; validate account ids; move budget pace into `tools.py`; checkpointer reconnect; console script for the MCP server.

## Every session

Update **PROGRESS.md** before you finish: what you built, why, what is next, and the real test count. Only write what is true. Run the tests first.
