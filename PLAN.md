# PLAN.md — Rectifi: make it something a student can clone and run today

Audit date: 2026-10-21 (Opus, high effort). Baseline: 27/27 tests pass. Python 3.13.16.
Implementer budget: about 2 hours. Do **all of P0** first, in order. Then do P1 from the top. Don't touch P2 tonight.

## A. Verdict

### Solid (leave alone)
- Rule 2 (no float money): schema.sql uses NUMERIC; money.parse_amount → Decimal; totals via SQL SUM/Decimal. Floats only in UI display.
- Rule 3 (DB dedupe): partial unique indexes in schema.sql back the `ON CONFLICT DO NOTHING` in tools.py; importer ref → fingerprint → merge order is right; re-import is idempotent.
- Rule 4 (reconciliation): tools.reconcile correct and tested.
- No SQL injection: all queries parameterised.
- DeepSeek non-thinking: get_llm() sets `extra_body={'thinking': {'type': 'disabled'}}`, temperature 0. Live API acceptance UNVERIFIED (no key in build container) — check with one live call on the laptop.
- Rule 1 write guard: graph._guard blocks write amounts not in the user's last 3 messages.
- MCP server is read-only (verified over stdio).
- UI theme is well built — no redesign.

### Broken / risky (verified)
| # | Severity | Where | Problem |
|---|---|---|---|
| 1 | Rule 6 violation | agent path, mcp_server.py | No account-number masking anywhere; raw SMS/payees (can carry card numbers) go to the LLM and MCP. |
| 2 | Bug | mcp_server.py `_json` | Every Decimal formatted as rupees, so percentages show as "₹27.50". |
| 3 | Bug (crash) | streamlit_app.py overview chart | Income-only month → empty DataFrame → Altair ValueError. |
| 4 | Bug | streamlit_app.py `_boot` + db.py pool | DB down waits 30 s, and the failure is cached until Streamlit restarts. |
| 5 | Risk | Ask page | Default provider ollama; with no Ollama you get a raw traceback after sending. |
| 6 | Risk | graph.py + agent/tools.py log_sms | log_sms trusts SMS text echoed back by the LLM; after masking that breaks dedupe. LLM must not carry raw SMS. |
| 7 | Risk | tools.add_transaction / set_category | Category kind not checked against direction; such rows vanish from summaries. |
| 8 | Risk | importer .xls | xlrd not installed → raw ImportError. |
| 9 | Risk | importer fingerprint | Two identical same-day rows without a balance column collapse into one. |
| 10 | Risk | scripts/seed_demo.py | Seeds whatever DB is configured, including the real one; doubles on rerun. |
| 11 | Risk | tests/conftest.py | TEST_URL read before .env; DB down → 27 × 30 s timeouts. |
| 12 | Hygiene | streamlit_app.py | `use_container_width` deprecated. |
| 13 | Hygiene | db.py | read_text without encoding (Windows cp1252). |
| 14 | Gap | — | No weekly review loop anywhere. |
| 15 | Gap | repo root | Missing requirements.txt, docker-compose.yml, .env.example, README, CLAUDE.md, PROGRESS.md, .gitignore, launcher, MCP/app tests. |

Privacy caveat for README: with LLM_PROVIDER=deepseek, chat text and the agent's (masked) tool results go to DeepSeek; with Claude Desktop, MCP results go to Anthropic. Statement files, stored raw SMS and the database never leave the laptop. With Ollama nothing leaves.

## B. Work items

Conventions: new test files use the autouse `fresh_db` fixture; async MCP calls via `asyncio.run(...)` inside sync tests (don't add pytest-asyncio). Run `python3 -m pytest -q` after each item.

### P0 — tonight (in order)

**P0-1 Fail fast when Postgres is down.** db.py: `DatabaseUnavailable(RuntimeError)`; pool with `connect_timeout: 3`, `open=False`, then `pool.open(wait=True, timeout=5)`; on PoolTimeout/OperationalError close and raise DatabaseUnavailable with `host:port/dbname` (never the password) and the hint "Start it with `python scripts/dev.py db`, or check DATABASE_URL in .env." `init_db` reads schema with `encoding="utf-8"`. streamlit_app.py: `_boot(url)` cached by URL and raising (exceptions aren't cached); `db_down(exc)` page with title "Rectifi can't reach its database"; wrap `st.navigation(...).run()` catching only DatabaseUnavailable / psycopg.OperationalError / psycopg_pool.PoolTimeout. Test `tests/test_db.py::test_db_unreachable_fails_fast` (raises, < 8 s, password not in message).

**P0-2 Masking; raw SMS never via the LLM.** New `rectifi/privacy.py`:
```python
import re
_CARD = re.compile(r"(?<![\w])(\d{4})([ -])(\d{4})\2(\d{4})\2(\d{3,7})(?![\w])")
_LONG = re.compile(r"(?<![\w])([\dXx*]{5,}\d{4})(?![\w])")

def mask_sensitive(text: str) -> str:
    """Account/card/UPI-ref/phone numbers -> keep last 4. Amounts, dates and 'XX1234' untouched."""
    if not text:
        return text
    text = _CARD.sub(lambda m: f"XXXX{m[2]}XXXX{m[2]}XXXX{m[2]}{m[5]}", text)
    def repl(m):
        s = m[1]
        if sum(c.isdigit() for c in s) < 9 and not re.search(r"[Xx*]", s):
            return s
        return "X" * (len(s) - 4) + s[-4:]
    return _LONG.sub(repl, text)
```
graph.py: send masked copies of the window to the model, keep state raw (guard still reads raw). For `log_sms` calls, substitute `sms_text` with the raw text of the most recent of the last GUARD_LOOKBACK user messages where `parse_sms(raw)` is not None and `mask_sensitive(raw)` contains the LLM's text (whitespace-normalised); if none, return `ToolMessage("GUARD: I can only record an SMS the user pasted. Ask them to paste it again.", status="error")`. Update log_sms docstring. mcp_server `_json` returns masked JSON. Tests: `tests/test_privacy.py::test_mask_sensitive` (parametrised: `"A/c 0012345678901 debited"`→`"A/c XXXXXXXXX8901 debited"`; `"POS 416021XXXXXX4417 DMART"`→`"POS XXXXXXXXXXXX4417 DMART"`; `"card 4111 1111 1111 1111"`→`"card XXXX XXXX XXXX 1111"`; `"UPI Ref No 429183746521."`→`"UPI Ref No XXXXXXXX6521."`; unchanged: `"spent 1,25,000.50 on 21-10-2026"`, `"Rs.12500000"`, `"A/c XX1234"`, `"Avl Bal INR 19,750.00"`), `test_masking_keeps_amounts`; tests/test_agent.py: `seen` list on ScriptedModel, `test_model_never_sees_account_numbers` (no `\d{9,}` in anything the model saw; saved upi_ref is the real one; raw_text keeps the real account locally), `test_log_sms_rejects_text_user_never_pasted`.

**P0-3 Ask page degrades gracefully.** config.ollama_base_url() (OLLAMA_BASE_URL, default http://localhost:11434) passed to ChatOllama. `llm.llm_status() -> (ok, why)`: deepseek without key; ollama unreachable (httpx GET /api/tags, timeout 1.5); model not pulled (match name or name before ':'); unknown provider. page_ask: if not ok → st.warning(why) + empty card "The assistant is off — the rest of Rectifi works." + page_link to Add, then return. Tests `tests/test_llm.py`: deepseek no key; ollama unreachable at 127.0.0.1:9 (< 3 s); unknown provider; `test_deepseek_thinking_disabled` (extra_body, model_name deepseek-flash, temperature 0).

**P0-4 MCP fixes + tests.** ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False) on every tool; percentages as "x%" in spending_summary share and budget_status pace/percent; new read-only `list_accounts()`; no print() on the server path. Tests `tests/test_mcp.py`: read-only names/annotations, percentages not rupees, card numbers masked, stdio start lists ≥ 5 tools.

**P0-5 UI crash + guard tests.** Overview bar chart only if `summary["by_category"]`, else empty state. test_agent.py: invented budget blocked; "2.5k" accepted as 2500; amounts older than lookback forgotten; non-numeric amount rejected.

**P0-6 App smoke tests** `tests/test_app.py` with streamlit AppTest: empty DB renders; income-only month renders (regression for #3); DB down shows message in < 15 s without exception.

**P0-7 Packaging, launcher, docs.** requirements.txt pinned to installed versions (streamlit 1.66.0, altair, pandas 3.0.5, openpyxl, psycopg[binary] 3.3.6, psycopg-pool 3.3.3, python-dotenv, httpx, langchain-core 1.6.12, langgraph 1.2.16, langgraph-checkpoint-postgres 3.2.0, langchain-deepseek 1.1.1, langchain-ollama 1.1.0, mcp 2.3.0, pytest 9.1.1) + `tests/test_requirements.py::test_pins_match_installed`. docker-compose.yml (pgvector/pgvector:pg16, port bound to 127.0.0.1, healthcheck, volume, mounts db/init.sql). db/init.sql (idempotent: role rectifi, DBs rectifi, rectifi_test, rectifi_demo; no vector extension yet). .env.example. .gitignore (incl. statements, csv/xls/xlsx except test fixtures). scripts/dev.py (stdlib-only; setup, db, init, doctor, app, up, test, mcp, mcp-config, demo on :8502 using rectifi_demo; list args, no shell; PYTHONUTF8=1). README.md (what it is, what leaves your laptop, prerequisites, install Windows/Mac, DB path A Docker / path B native, choose model, run, first 10 minutes, importing a statement, weekly review, Claude Desktop MCP config, tests, troubleshooting, layout). CLAUDE.md (rules, architecture map, commands, conventions, deferred list, "update PROGRESS.md every session"). PROGRESS.md (Built / Why / Next). conftest: load .env before TEST_URL; session check that exits with a clear message if the test DB is unreachable.

P0 done when: all tests pass, `python3 scripts/dev.py doctor` exits 0, `docker compose config --quiet` exits 0.

### P1 — next, in order
- **P1-1 Weekly review loop**: `weekly_reviews` table (week_start Monday PK, commitment); `tools.weekly_review(day)` (week income/spending/saved/savings_rate, prev week spending + change, top categories, 3 biggest debits, uncategorised count, budgets needing attention, accounts needing attention with reasons, last week's commitment); `save_commitment`; agent tool + read-only MCP tool; **Review** page second in nav using existing classes; tests (`test_weekly_review` with today = 2026-10-21, `test_commitment_roundtrip`, AppTest render).
- **P1-2 Verify numbers in the agent's reply**: `graph.unverified_amounts(...)`; caption under the reply when a ₹ figure isn't in the user text or this turn's tool results; test.
- **P1-3 Category kind must match direction** in add_transaction/set_category; per-row errors in Ledger; test.
- **P1-4 Importer**: friendly .xls error; occurrence counter so identical rows both import (and re-import stays idempotent); tests.
- **P1-5 Seed safety**: refuse unless DB name ends in `_demo` or `--force`; no-op if already seeded.
- **P1-6 Streamlit hygiene**: `width="stretch"`; Decimal comparison in budget save; ledger truncation notice.

### P2 — later
Cleaner payees from POS/ATM/NEFT narrations; refunds as negative spending; one typed amount backs one write; validate account ids; move pace computation into tools; checkpointer reconnect; pyproject console script for MCP.

## D. Next (deferred roadmap)
1. **SMS auto-capture webhook** — POST /sms with bearer token, off unless RECTIFI_WEBHOOK=1; done when the same SMS twice gives added then duplicate, and no token → 401.
2. **RAG categoriser (pgvector)** — local embeddings suggest categories for "Other" rows, user confirms; done when top-1 accuracy beats merchant rules on held-out data.
3. **Spending forecast** — deterministic baseline first; a learned model only if it beats the baseline in backtests (needs ≥ 6 months of data).
4. **MCP write tools** (optional) — same amount guard plus confirmation.
5. **Android app** — offline capture syncing to the webhook exactly once.
