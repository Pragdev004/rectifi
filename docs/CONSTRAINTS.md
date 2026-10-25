# User constraints (from the whole conversation) — the integrator and reviewer check every item

## Who / where
- Prag: 2nd-year student, pet project, **₹0 budget**. Windows 11 laptop, Python 3.12 venv, native PostgreSQL 16 (no Docker on laptop), Ollama installed (`qwen2.5-coder:7b` pulled), Android phone, hostel Wi-Fi (client isolation) → Tailscale.
- Laptop cannot run 24/7. Project lives at `C:\Users\praga\Desktop\projects 2\rectifi`; started with `Start Rectifi.bat` / `python scripts/dev.py up`; web app on :8501; Tailscale Serve URL `https://pragadeeshwar.tail94c6aa.ts.net/`.
- Never print/log secrets; never hand-edit his `.env` values except LLM_PROVIDER-type switches he asked for.

## Models / process
- Planning, architecture, creative thinking and review → **Opus 5.5, high effort**. Coding → **Sonnet 5.5, high effort**.
- Work in token-efficient parallel/sequential/iterative/conditional workflows (worktrees, file ownership, narrow briefs).

## Product requirements (current phase)
1. Upload a passbook/statement (PDF/CSV/XLSX) → transactions import and are **auto-classified** into Food & Dining, Groceries, Entertainment, Travel, Fuel, Rent and other common categories; anything not confidently classified → **Miscellaneous**.
2. **Host as a website for free**; it's a pet project; free-hosting limits accepted. Public hosting requires a login (free; password/session now, GitHub OAuth later).
3. An **LLM writes Python (matplotlib/seaborn/pandas/numpy; plotly optional) to draw charts of his spending**, wrapped in a **harness** (validate, sandbox, repair loop, checks).
4. **Ollama** for all LLM work for now (₹0, local).
5. **LangChain + LangGraph**; build **CRAG** and **Self-RAG**.
6. Use **all concepts from CampusX LangChain playlist (esp. the final Ollama masterclass)** and **all CampusX LangGraph concepts except MCP**, explicitly including **short-term memory, long-term memory, observability, persistence, streaming, HITL, subgraphs, and sequential/parallel/conditional/iterative workflows** — wherever genuinely appropriate. Provide a concept → file → test table (docs/CONCEPTS.md).
7. **ChatGPT-style chat window**: previous conversations sidebar, persistence, resume with history, streaming — in the Passbook design (not a clone look).
8. Fix/diagnose "**API not responding**" so LLM failures are never silent (doctor round-trip, clear errors on the page).

## Non-negotiable engineering rules (CLAUDE.md 1–13 + plan additions)
- LLM never decides a number; ₹ figures come from tools/SQL/code and pass `unverified_amounts`. Charts: totals computed by code from data, never typed by the model.
- Decimal money, DB-enforced dedupe, reconciliation proves completeness, deterministic insights, masking before any model call, no model sees raw SMS/PDF.
- Web: no money maths in the browser, thin API, guards stay (Host allow-list, X-Rectifi-Request, no CORS, CSP), bind 127.0.0.1 locally, one worker, `web/dist` committed (laptop needs only Python), Streamlit fallback stays.
- Every feature tested; no live Ollama in CI (fakes); live checks under `-m ollama`; requirements pinned; existing tests stay green.
- Plain-English UI/docs for a 2nd-year student; Passbook design system; accessibility (44 px targets, words on stamps, keyboard, light/dark, 390/1280 layouts).
- Honesty: never claim something works without running it; keep a "Not verified" list (Windows sandbox job object, real Ollama quality/latency, Oracle VM, Funnel, real bank statements).

## Deferred by the user
- Android app (native, phone-first, own parsers) — later. iPhone out of scope.
- Paid LLM APIs (DeepSeek/Gemini/HF) remain optional providers, not defaults.
