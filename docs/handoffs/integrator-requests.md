# Integrator requests from workstreams (apply during F3)

Test DBs: conftest needs names ending `_test` → per-worktree DBs are `rectifi_<ws>_test`.

## WS-LLM (ws/llm, d6257e3) — 983 passed
1. Apply `docs/handoffs/ws-llm-dev.patch` to scripts/dev.py (doctor uses `load_devtool("models").doctor_checks(setting, quick=...)`; `doctor --quick`; `parse_known_args` so `ollama-setup --dry-run` / `llm-check --quick` work).
2. tests/test_dev_script.py `dev()` helper: append `--quick` when args start with "doctor".
3. tests/test_foundation.py: delete `test_probe_is_left_to_ws_llm`; parametrize list of stub commands becomes `["classify","eval","set-password","deploy-check"]` (then drop classify/eval/set-password/deploy-check entries as their WS make them real).
4. Mount `<ModelCheck role="chat" hideWhenOk />` (web/src/components/ModelCheck.tsx) on the chat/Ask page and after any model error.
5. .env.example: RECTIFI_MODEL_CHAT/CLASSIFY/CODE/GRADE (empty = chat model), RECTIFI_EMBED_MODEL=nomic-embed-text, OLLAMA_NUM_CTX=8192 (short ctx drops tool list), OLLAMA_TIMEOUT=120, OLLAMA_KEEP_ALIVE=30m, Windows tip OLLAMA_BASE_URL=http://127.0.0.1:11434; comment: `dev.py ollama-setup` downloads a RAM-tier set.
6. Docs/CLAUDE.md: `dev.py llm-check`, `ollama-setup`, `doctor --quick`; deploy/Modelfile.*; /api/models/status + /probe; ModelCheck; llm.describe/llm_status/model_name take a role; explain_error v2.

## WS-CLASSIFY (ws/classify, f39b6ae) — 969 passed; fixed layers eval 83.1% acc, macro-F1 0.906, 29.9% → Miscellaneous
1. Add `rectifi/evals/__main__.py` dispatching `classify` → `rectifi.evals.classify.main(argv)` (supports --live; returns 2 if model not ready). Other evals (charts, rag) dispatch the same way.
2. API lifespan (app.py): lazily call `rectifi.agent.jobs.requeue_unfinished()` at startup if DB reachable.
3. scripts/devtools/models.py `classify` command: `tools.queue_miscellaneous_resort(); jobs.queue_for_batch(None)` (or blocking `classify_pending(None)`).
4. Ledger `?source=` filter: `tools.list_transactions(source=...)` → `AND t.category_source = %s` (IS NULL for "earlier"); pass through routes/transactions.py and web useTransactions/TxnParams.
5. web/src/api/types.ts: add optional ClassifyFields to Txn (category_source, category_confidence, category_reason, suggested_category, suggested_confidence); remove the cast in WS-CLASSIFY code.
6. Docs/CLAUDE.md: "Cash Withdrawal" is an expense on purpose; layers 1–4 deterministic, model only after import (agent/classify.py, agent/jobs.py); env RECTIFI_CLASSIFY_APPLY_MIN / _SUGGEST_MIN / _BATCH / RECTIFI_AUTO_CLASSIFY. Short built-in rules (≤4 letters) now match at word start.

## WS-CHARTS (ws/charts, 8931b2a) — 1064 passed
1. `rectifi/evals/__main__.py`: add `charts` → `rectifi.evals.charts.main(argv)`.
2. Plotly stretch NOT built (validator allows plotly only with RECTIFI_CHARTS_PLOTLY=1; status returns plotly:false). Leave as stretch.
3. build-web + commit dist; PROGRESS.md entry; CLAUDE.md A3 rule text (charts: validator refuses typed money; figure check; sandbox limits; pinned rerun makes no model call).
4. Sandbox threat model (for docs): audit hooks are not a security boundary; native code not network-sandboxed; Windows Job Object path never run; macOS RLIMIT_AS unreliable; library-internal exec allowed deliberately.

## WS-RAG (ws/rag, 78c060f) — 974 passed
1. agent/contracts.py: RagInput.mode comment → `"auto"|"rag"|"crag"|"self_rag"|"full"` (other words mean auto); RagOutput comment: extra keys route, mode, abstained; citations carry n, corpus, label.
2. Supervisor: pass the pooled saver from agent.checkpoint to build_rag_subgraph (web-consent interrupt needs it); resume with Command(resume=True|False); interrupt payload `{"type":"web_search","query","message","choices"}` (TS WebConsentRequest matches).
3. Indexing: call `rectifi.api.routes.knowledge.start_reindex()` on app start and after an import (thread; one worker only), or `rectifi.rag.index.reindex(get_rag_embeddings())` from the bg worker.
4. Config/docs: RECTIFI_RAG_REFINE_TAU (0.2, rag/retrieve.py), RECTIFI_EMBED_PREFIXES (1, rag/embeddings.py) → move to config.py/.env.example; config.web_search() is a bool (RECTIFI_WEB_SEARCH=1); ddgs optional/unpinned.
5. CLAUDE.md map, PROGRESS, README; honesty line: "Self-RAG here is prompt-based: a normal small model answers short structured questions. It is not the fine-tuned reflection-token model of the paper, and its checks can be wrong. The ₹ check is plain code."
6. Chat (WS-CHAT) should use components/rag {Citations, CitationMark, ReflectionTrace, WebConsent}, type RagAnswer (api/knowledge.ts), wire.rag_answer(out).
7. CONCEPTS.md: toolkit test is tests/test_rag_toolkit.py.

## WS-CHAT (ws/chat, 4c7db78) — 982 passed (+4 e2e pass vs a scratch build)
1. build-web so dist has chat/Memory/Traces (e2e skips unless dist contains "Search conversations").
2. WS-HOST: set `request.state.uid` in auth (memory namespace; default "owner").
3. Charts page must read `/charts?chart=<id>` (chart slips link there); optionally show the chart image inline in chat.
4. supervisor.ANSWER_STYLES: mode = auto|rag|crag|self_rag|full (matches WS-RAG). Update contracts comment (see WS-RAG 1).
5. Interrupt kinds: "memory" (text), "web_consent" (payload.query), "review_code" (payload.code); resume values booleans. Streamed token nodes: "generate" (RAG), "agent", "smalltalk". Check real RAG/chart subgraph node names & payloads match.
6. web/src/api/queries.ts `useAskStatus` now unused — remove.
7. Swap minimal Sources/"How checked" in chat Message view for WS-RAG Citations/ReflectionTrace.
8. Run test_supervisor.py + test_chat_api.py against REAL subgraphs (drop contracts monkeypatch) and fix state-key mismatches.
9. Known limit: on phone width the "assistant is off" state hides the whole chat page incl. thread history — fix: show history read-only + ModelCheck.
10. First two WIP commits carry an older trailer — squash on merge is fine.

## WS-HOST (ws/host, b960069) — 955 passed
1. app.py: import `install_session_lock`; build `app = Starlette(...)`, call `install_session_lock(app)`, return app (raises StartupRefused in public mode without hash+secret). Also set `request.state.uid` (WS-CHAT 2).
2. web/src/api/client.ts: on `err.code === "auth_required"` dispatch `window.dispatchEvent(new Event("rectifi:auth-required"))` before throwing.
3. test_foundation: drop set-password/deploy-check from stub list (real now).
4. dev.py: before `parse_args`, if argv[1] in devtool_commands() call it with argv[2:] and return (lets `set-password --print` through). Reconcile with WS-LLM's parse_known_args patch — keep one mechanism.
5. .gitignore: add `.env.deploy`, `backups/`.
6. App.tsx: render `/login` outside AppShell.
7. AppShell + More sheet: "Sign out" when auth required & signed in (useAuthStatus/useSignOut in api/auth.ts; useSignOutEverywhere exported).
8. .env.example + README: RECTIFI_PASSWORD_HASH, RECTIFI_SESSION_SECRET, RECTIFI_PUBLIC.
9. CLAUDE.md: rule 11 addendum (A3); map/commands for set-password, deploy-check; docs/HOSTING.md.
Unverified: docker build/run, aarch64 wheels, read_only FS vs chart sandbox writes (chart sandbox needs a writable tmp dir — check against compose read_only+tmpfs), Oracle, Tailscale Serve Host header, Funnel on free plan, ollama image tag.


## WS-FOUNDATION (ws/foundation, multi-user plan stage 2)

For the integrator:
1. PROGRESS.md and CLAUDE.md were not touched here, so seven branches do not conflict. Add the foundation entry and CLAUDE.md rules 18 to 20 (plan section 9). Map rows for `rectifi/userctx.py`, `vault.py`, `sealed_fields.py`, `merchant_allowlist.py`, `hooks.py`, `tests/support/tenants.py`, `scripts/devtools/users.py`.
2. docs/HOSTING.md (WS-DEPLOY): the compose files now have two logins, `postgres` (superuser: set-up and backups) and `rectifi` (the app, a plain role), and a new required `RECTIFI_DB_PASSWORD` in `.env.deploy` (different from `POSTGRES_PASSWORD`). Every `-U rectifi` for backup, restore and `psql` in HOSTING.md becomes `-U postgres` (a plain role cannot `pg_dump` forced tables; the login_lock fix too). An old Docker volume keeps its superuser login: document export, `down -v`, `up`, import.
3. scripts/devtools/deploy.py (WS-DEPLOY): `deploy-check` should require `RECTIFI_DB_PASSWORD` (same rules as `POSTGRES_PASSWORD`, and not equal to it). Not edited here.
4. No Docker on this laptop: `deploy/compose.oracle.yml`, `deploy/db-init/10-app-role.sh` and `docker-compose.yml` have static checks only. Run them once where Docker exists.
5. Prag's real database was never touched. After merge his laptop app will not start until he backs up and runs `python scripts/dev.py migrate-tenancy`. `dev.py init`, `up`, `phone`, the web server and Streamlit print the plain message; `dev.py doctor` shows a WARN.

For other streams:
- WS-AUTH: call `db.seed_user(uid)` at sign-up and `hooks.after_sign_in(ctx)` after the cookie is set. `UserContext` has one extra field, `private_key: bytes | None = None` (the row factory needs it to open `rs1.` values); every field but `uid` has a default. `vault.py` has the key helpers (`derive_password_keys`, `wrap_key`, `unwrap_key`, `new_recovery_code`, `recovery_key`, `new_keypair`). `tests/support/tenants.py::make_user` builds a context with fresh keys.
- WS-CORE: `authenticate_device` must look the token up under `db.connect_system(...)` (policy `device_lookup` lets a system connection READ `phone_devices`), then bind the person for every write. `pattern_blind` / `target_blind` are filled from the plain column by a trigger when an INSERT omits them; an encrypted person must pass `vault.blind(...)` or `enforce_sealed` refuses. That trigger also refuses plain text in any `sealed_fields.SEALED` column for `users.encrypted = true`; on UPDATE only changed columns are checked.
- WS-AI, WS-ALERTS: `tests/test_userctx.py::KNOWN_BARE_THREADS` lists files still using `threading.Thread`. Remove your entries when you move to `userctx.spawn`; the list may only shrink.
- WS-ALERTS: `alerts.py` watermark, prefs and digest marker are now `app_settings (user_id, key)`, so they are per person already. The advisory lock ids are still global.

Choices Prag may overrule:
- Allow-list: a name of 4 letters or fewer must be a whole word (`ola`, `uber`, `pvr`, `ajio`), stricter than the plan's start-of-word. A wrong plain tag leaks a payee name; a missed one only loses a signed-out alert.
- `enforce_sealed` also guards blind columns (must start `b1:`). `alert_rules.target` is not trigger-guarded (depends on the allow-list; WS-ALERTS enforces in code). `conversations.thread_id` stays a global primary key (random UUIDs, owner-only). Non-unique indexes do not start with `user_id` (fine for a few friends).
