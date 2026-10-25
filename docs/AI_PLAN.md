# Rectifi next phase: architecture and build plan (classifier, chart harness, CRAG + Self-RAG, chat + memory, Ollama, free hosting)

This plan was written from a read of the code. It covers everything you asked for, including the three later additions: Self-RAG; short- and long-term memory, observability and the chat window; and parallel workstreams.

I read: CLAUDE.md, PROGRESS.md, docs/WEB_PLAN.md (part B), rectifi/{tools,importer,parsing,privacy,money,config,db,insights}.py, rectifi/schema.sql, rectifi/agent/{graph,tools,llm,explain}.py, rectifi/api/{app,ask,security,wire,inputs}.py, routes/{ask,imports,meta,transactions}.py, tests/{conftest,test_agent}.py, scripts/dev.py (doctor), .env.example, web/src layout, nav.ts and Ask.tsx/ndjson.ts.

I also checked the installed versions of the APIs the plan relies on, using read-only `python -c` calls. All of these exist in what is installed now:
- langchain_core 1.6.12: BaseToolkit, InjectedToolArg, InMemoryVectorStore, maximal_marginal_relevance, BaseLoader, BaseRetriever, all Runnables, the three output parsers, FewShotChatMessagePromptTemplate, load_prompt, trim_messages.
- langgraph 1.2.16: ToolNode, tools_condition, create_react_agent, interrupt, Command, Send, RetryPolicy.
- langgraph.store.postgres.PostgresStore and InMemoryStore.
- ChatOllama has `num_ctx`, `keep_alive`, `format`, `client_kwargs` and `reasoning`, and its `with_structured_output` defaults to method `json_schema`.

What is installed but not pinned: matplotlib 3.11.2, seaborn 0.13.2, numpy 2.5.3, langsmith 0.14.11 (comes with langchain-core) and itsdangerous.

What is missing: langchain_text_splitters (1.1.3 on PyPI), plotly (7.1.0 on PyPI), langchain-classic and langchain-community.

Docker CLI and buildx are installed, but there is **no Docker daemon** (no /var/run/docker.sock). hadolint-py 2.15.1.2 and dockerfile-parse 2.0.1 are on PyPI.

---

## 0. Findings the plan depends on

**1. Hugging Face Docker Spaces now need a paid account.** The HF docs say today: "Static Spaces are free for everyone. Gradio and Docker Spaces run on compute and require a paid plan to create: PRO for personal accounts." CPU Basic hardware (2 vCPU, 16 GB, 50 GB disk that does not persist) has no hourly cost, but you can't create a Docker Space without PRO at $9 a month. **HF Spaces is rejected for ₹0.**

**2. Oracle Always Free was cut on 2026-06-15** to 2 OCPU, 12 GB RAM and 1,500 OCPU-hours a month on VM.Standard.A1.Flex (Arm).
- It includes 200 GB of block storage, and the instance must be in your home region.
- Oracle may take back an "idle" instance. Idle means that over 7 days the 95th-percentile CPU is under 20%, network is under 20%, **and** memory is under 20% (the memory test applies to A1 only).
- "Out of host capacity" errors happen.

**3. Other free hosts:**
- **Neon free:** 1 GB per project, 100 CU-hours, sleeps after 5 minutes idle (can't be turned off), 5 GB egress.
- **Render free:** 512 MB, 0.1 CPU, spins down after 15 minutes (about 1 minute to wake), free Postgres expires after 30 days.
- **Koyeb:** no free compute for new users since Feb 2026.
- **Fly.io:** trial only.
- **Railway:** $1 a month after the trial.
- **None of these can run Ollama.**

**4. Ollama facts.**
- `qwen2.5-coder` has the `tools` capability tag (sizes 0.5b to 32b; 7b is 4.7 GB).
- `qwen3` has tools and thinking (4b is 2.5 GB, 8b is 5.2 GB). `llama3.2` (3b, 2.0 GB) has tools.
- `nomic-embed-text` is an Ollama embedding model: 274 MB, 2K context.
- `/api/show` returns `capabilities` (for example `["completion","vision"]`), so "does this model support tools?" can be checked from data.
- `/api/chat` `format` accepts a JSON schema. `/api/embed` takes `input` as a string or a list. The default `keep_alive` is 5 minutes.
- The Ollama search page also lists 2026 model families I can't judge (qwen3.8, ornith, bonsai-2, …). I don't know how good they are. The doctor and the evals pick models from measurements, not from my memory.

**5. Google "AQ." keys.** Google staff on the Gemini forum say AI Studio "will now only generate Authentication Key (AQ) keys". People report that AQ keys fail on generativelanguage REST with "API key not valid". Other threads are titled "401 ACCESS_TOKEN_TYPE_UNSUPPORTED".

**6. Plotly under a strict CSP.** plotly.js injects `<style>` elements. PR #7109 (a static `plotly.css` plus a `no-inline-styles` marker) was **unmerged** when I read it. So plotly in the SPA would break the current CSP (`style-src 'self'`).

**7. Self-hosted Langfuse** needs Postgres, ClickHouse, Redis/Valkey and S3 storage. **Rejected** for a 12 GB VM that also runs Ollama.

**8. CampusX playlists** are in section C.

**9. Code facts that matter:**
- Rule 8 forbids langchain in `importer.py`, and rule 5 forbids an LLM in `tools.py`. So **the deterministic classification layers must live in a new pure module, and the LLM layer must run after the import** (it can't run inside `import_rows`).
- `wire.to_wire` turns **every Decimal into a money string**. Classifier confidence must therefore be stored as a `SMALLINT` percent, not `NUMERIC`.
- `TailscaleLock` refuses Funnel traffic. That is correct: a public request carries no `Tailscale-User-Login` header and its host isn't loopback. Public mode therefore needs its own login.
- `merchant_rules` matches substrings, longest first. Risky words like "rent" (which is inside "parent" and "current") must be word-boundary heuristics, not substring rules.
- The existing default rules map `petrol` and `fuel` to Transport. `DEFAULT_RULES` uses `ON CONFLICT DO NOTHING`, so existing databases keep that mapping unless a migration moves it.
- `PostgresSaver` is built on one connection (`ask.get_graph`). Several graphs on several threads need a **pool**.

---

## A. Architecture and data model

### A1. Module map (new and changed)

```
rectifi/
  categorize.py          NEW, pure (stdlib + numpy, NO langchain): fallback names, merchant_key(), HEURISTICS,
                         NgramIndex (char 3-5-gram hashed TF cosine), decide(conn, payee, narration, direction) -> Decision
  tools.py               CHANGED: _categorize uses categorize.decide; set_category -> source 'user' + learned rule;
                         new sections (append points, see F): classify, charts, chat, rag
  importer.py            CHANGED: records category_source/confidence/reason/classify_state per inserted row
  db.py                  CHANGED: new DEFAULT_CATEGORIES/RULES, _migrate_categories() BEFORE the default inserts
  schema.sql             CHANGED: additive (A2)
  textvec.py             NEW, pure numpy: hashed n-gram vectors (used by categorize + tests)
  knowledge/*.md         NEW: built-in personal-finance guides (no ₹ figures; a test enforces it)
  agent/
    llm.py               CHANGED: roles, probe(), capabilities, timeouts, num_ctx, explain_error v2, redact()
    checkpoint.py        NEW: pooled PostgresSaver + PostgresStore (one ConnectionPool), shared by all graphs
    tracing.py           NEW: LocalTraceHandler (BaseCallbackHandler -> trace_spans), LangSmith guard
    contracts.py         NEW: TypedDicts/Protocols that every workstream codes against (F0)
    classify.py          NEW: LLM layer (batched, structured output, few-shot from neighbours)
    jobs.py              NEW: one background worker thread (classification, re-indexing)
    supervisor.py        NEW: chat router graph with subgraphs (agent | knowledge | chart | smalltalk)
    memory.py            NEW: short-term trimming/summary + long-term Store helpers
    graph.py             CHANGED: build_graph(llm, tools=AGENT_TOOLS, ...) param; READ_ONLY_TOOLS
  charts/                NEW: validate.py (ast), sandbox.py, _child.py, _winjob.py, theme.py, checks.py, prompts.py, graph.py, fonts/
  rag/                   NEW: embeddings.py, store.py (PgVectorStore), loaders.py, splitting.py, index.py,
                         retrieve.py, graders.py, prompts.py, graph.py (CRAG + Self-RAG), web.py (optional)
  evals/                 NEW: __main__.py, classify.py, charts.py, rag.py (runners; data in /evals/*.json)
  api/
    auth.py              NEW: scrypt hash check, signed session cookie, login rate limit
    security.py          CHANGED: SessionLock middleware, upload paths set, chart-frame CSP route
    routes/{classify,charts,chat,memory,traces,models,auth,knowledge}.py   NEW
deploy/ Dockerfile, compose.oracle.yml, cloud-init.yaml, Modelfile.* ; .dockerignore ; docs/HOSTING.md ; docs/CONCEPTS.md
scripts/devtools/{models.py,deploy.py}   NEW stdlib-only, loaded by path from dev.py (like tailscale.py)
```

**Import-boundary rules** (each one gets a test):
- `categorize.py`, `textvec.py`, `importer.py`, `insights.py`, `charts/validate.py`, `charts/sandbox.py` and `charts/_child.py` import no `rectifi.agent` and no langchain.
- `rectifi.api` must still import without loading langchain. Every new route imports `rectifi.agent.*`, `rectifi.rag.*` and `rectifi.charts.graph` lazily, inside the handler.

### A2. Schema changes (all additive, inside `schema.sql`, safe to run again)

```sql
-- classification audit (confidence is a PERCENT SMALLINT on purpose: wire.to_wire treats Decimal as money)
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_source TEXT
  CHECK (category_source IN ('user','learned','rule','heuristic','retrieval','llm','fallback'));
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_confidence SMALLINT CHECK (category_confidence BETWEEN 0 AND 100);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_reason TEXT;              -- "rule 'swiggy'", "like #812 SWIGGY BLR", "qwen2.5:3b"
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS suggested_category_id INT REFERENCES categories(id) ON DELETE SET NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS suggested_confidence SMALLINT CHECK (suggested_confidence BETWEEN 0 AND 100);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS classify_state TEXT CHECK (classify_state IN ('pending','done'));
CREATE INDEX IF NOT EXISTS idx_txn_classify_pending ON transactions (import_batch_id) WHERE classify_state = 'pending';

ALTER TABLE merchant_rules ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'default' CHECK (source IN ('default','learned'));
ALTER TABLE merchant_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS classify_jobs (
  id SERIAL PRIMARY KEY, batch_id INT REFERENCES import_batches(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','cancelled','no_model')),
  total INT NOT NULL DEFAULT 0, done INT NOT NULL DEFAULT 0, by_source JSONB NOT NULL DEFAULT '{}'::jsonb,
  model TEXT, error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS charts (
  id SERIAL PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL, code TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('png','plotly')), image_light BYTEA, image_dark BYTEA, plotly_json JSONB,
  period_start DATE, period_end DATE, pinned BOOLEAN NOT NULL DEFAULT false, model TEXT, attempts INT NOT NULL,
  trace JSONB NOT NULL DEFAULT '[]'::jsonb, checks JSONB NOT NULL DEFAULT '{}'::jsonb, thread_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), refreshed_at TIMESTAMPTZ);

CREATE TABLE IF NOT EXISTS rag_documents (
  id SERIAL PRIMARY KEY, corpus TEXT NOT NULL CHECK (corpus IN ('guide','note','upload')), title TEXT NOT NULL,
  source TEXT NOT NULL, sha256 TEXT NOT NULL, content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (corpus, source));
CREATE TABLE IF NOT EXISTS rag_chunks (
  id BIGSERIAL PRIMARY KEY, document_id INT REFERENCES rag_documents(id) ON DELETE CASCADE,
  txn_id BIGINT REFERENCES transactions(id) ON DELETE CASCADE,          -- corpus 'txn' rows (undo_import cascades)
  corpus TEXT NOT NULL CHECK (corpus IN ('guide','note','upload','txn')), chunk_index INT NOT NULL DEFAULT 0,
  content TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb, content_hash TEXT NOT NULL,
  embed_model TEXT NOT NULL, embedding REAL[] NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS uq_chunk ON rag_chunks (corpus, COALESCE(document_id,0), COALESCE(txn_id,0), chunk_index, embed_model);

CREATE TABLE IF NOT EXISTS conversations (
  thread_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'New conversation', preview TEXT,
  search_text TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations (updated_at DESC);

CREATE TABLE IF NOT EXISTS trace_spans (
  id BIGSERIAL PRIMARY KEY, run_id UUID NOT NULL, parent_run_id UUID, root_run_id UUID NOT NULL, thread_id TEXT,
  name TEXT NOT NULL, run_type TEXT NOT NULL, started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'running', model TEXT, input_tokens INT, output_tokens INT,
  input_preview TEXT, output_preview TEXT, error TEXT);                 -- previews masked + truncated to 2 KB
CREATE INDEX IF NOT EXISTS idx_spans_root ON trace_spans (root_run_id); CREATE INDEX IF NOT EXISTS idx_spans_time ON trace_spans (started_at DESC);

CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
-- session_epoch (sign out everywhere), login_lock; nothing secret is stored here
```

LangGraph's own tables (`checkpoints*` and `store*`) are created by `saver.setup()` and `store.setup()` in `agent/checkpoint.py`.

**The category migration** is `db._migrate_categories(conn)`. It **must run before** the default-category inserts, or a fresh "Miscellaneous" row gets created next to "Other":
1. If "Other" exists and "Miscellaneous" doesn't, run `UPDATE categories SET name='Miscellaneous' WHERE name='Other'`. The id stays the same, so transactions, budgets and rules carry over untouched. If both exist (the user made one by hand), leave both alone and log it. The fallback then uses "Miscellaneous".
2. Insert the new defaults with `ON CONFLICT (name) DO NOTHING`.
3. Run `UPDATE merchant_rules SET category_id=<Fuel> WHERE pattern IN ('petrol','fuel') AND category_id=<Transport> AND source='default'`.
4. Mark user-taught rules: `UPDATE merchant_rules SET source='learned' WHERE source='default' AND pattern <> ALL(<every default pattern>)`.
5. Insert the new default rules.

**Existing transactions are never re-categorised silently.** Instead the UI offers "Sort my Miscellaneous entries again", which queues a classify job on rows where `category_source IS NULL OR category_source='fallback'` and the category is Miscellaneous.

**Categories:** "Other" becomes **Miscellaneous**. "Other income" stays as the income catch-all.

| kind | categories |
|---|---|
| expense | Food & Dining, Groceries, Entertainment, Travel (trips), Transport (local commute), **Fuel**, Rent, Bills & Utilities, Shopping, Health, Education, Subscriptions, **Personal Care**, **EMI & Loans**, **Fees & Charges**, **Gifts & Donations**, **Cash Withdrawal**, **Miscellaneous** |
| income | Salary & Allowance, Refunds, **Interest & Cashback**, Other income |
| transfer | Transfers, **Investments** (SIP, stocks, FD, PPF; money you still own, so it isn't counted as spending) |

Cash Withdrawal is an expense on purpose: for a student, ATM cash is spending. This choice is debatable and goes in the README.

**Code that names the catch-all** must use the constants `categorize.FALLBACK_EXPENSE = "Miscellaneous"` and `FALLBACK_INCOME = "Other income"`:
- tools.py:146 and :770
- insights.py:184–190 and :518
- api/wire.py:113
- web Review.tsx:38–39
- streamlit_app.py:403
- seed_demo.py comment

**Existing tests whose expected strings change** (only "Other" becomes "Miscellaneous"; no test is deleted):
- test_tools.py:61
- test_app.py:111, :281
- test_insights.py:386, :439–441
- test_insights_page.py:208–252
- test_weekly_review.py:30, :63, :172
- test_api_transactions.py:167

### A3. CLAUDE.md changes (proposed; the integrator writes them)

- **Rule 5 addendum.** Generated chart code may aggregate the prepared frame's **integer paise** for drawing and for figure text, inside the sandbox. A ₹ figure in a chart's text must match a value the harness can trace: a plotted value, a series sum, or a `tools.chart_reference_totals` figure (B2.6).
- **Rule 8 clarification (needs your OK).**
  - Today: "No model ever sees an SMS or a PDF."
  - Proposed: "No model ever sees a bank SMS or a bank statement."
  - Knowledge uploads (.md/.txt, and .pdf only if you approve) are a separate opt-in path. A PDF that `importer.assess_statement` recognises as a statement is refused there.
  - Without your OK, uploads are .md/.txt only.
- **Rule 11 addendum.** `RECTIFI_BIND_HOST` defaults to 127.0.0.1. Only the Docker image sets 0.0.0.0, and compose publishes the port as `127.0.0.1:8501:8501` (a test checks this). If the bind host isn't loopback or `RECTIFI_PUBLIC=1`, the app refuses to start without `RECTIFI_PASSWORD_HASH` and `RECTIFI_SESSION_SECRET`.
- **New rule 14 (classification).** Classification never changes amount, direction, date, account or dedupe keys. A row with `category_source='user'` is never overwritten. Only the LLM layer runs after import, and only through `tools.apply_classifications`.
- **New rule 15 (chart sandbox).** Model-written code runs only after `charts.validate`, only in `charts.sandbox`, with a scrubbed environment and no database URL. Its output is images or JSON that get validated, never pickles.
- **Deferred item 1** ("RAG categoriser with pgvector") is replaced: it is built without pgvector.

---

## B. Per-feature design

### B1. Classification pipeline (feature 1)

**Layers, in order. The first confident layer wins.** Layers 1–3 are deterministic and run synchronously inside `import_rows` and `add_transaction`.

| # | Layer | Where | Confidence | Source |
|---|---|---|---|---|
| 0 | Category given by the user, chat or form | tools.add_transaction | 100 | user |
| 1 | Learned rules (`merchant_rules.source='learned'`), longest first | categorize.decide (SQL) | 98 | learned |
| 2 | Default merchant rules (substring), longest first | same | 95 | rule |
| 3 | Heuristics: word-boundary regex table with direction (below) | categorize.HEURISTICS | 80–90 fixed per entry | heuristic |
| 4 | Retrieval: char n-gram cosine kNN over **trusted** labelled rows (sources user, learned, rule; never llm or fallback, so mistakes don't feed themselves) | categorize.NgramIndex | weighted vote; accept if top-1 cosine ≥ 0.82 and vote share ≥ 0.7 | retrieval |
| 5 | Local LLM, enum-constrained structured output, background job | agent/classify.py | model's self-reported value (0–1) times 100 | llm |
| 6 | None of the above | — | — | fallback (Miscellaneous / Other income) |

Learned rules move ahead of default rules: `ORDER BY (r.source='learned') DESC, length(r.pattern) DESC`.

**Heuristics table** (`categorize.py`, ordered, each entry is `(regex, direction, category, confidence)`):
- `\b(atm|cash\s*wdl|nwd)\b` → Cash Withdrawal
- `\b(salary|sal\s*cr)\b` (credit) → Salary & Allowance
- `\b(int\.?\s*pd|interest|cashback)\b` (credit) → Interest & Cashback
- `\b(chrg|charges?|annual\s*fee|gst\b.*chrg|sms\s*alert)\b` (debit) → Fees & Charges
- `\b(emi|loan|nach.*(bajaj|hdb|fin))\b` → EMI & Loans
- `\b(sip|mutual\s*fund|groww|zerodha|nps|ppf)\b` → Investments
- `\b(rent|nobroker)\b` → Rent
- `\b(hpcl|bpcl|iocl|indian\s*oil|nayara|petrol|fuel)\b` → Fuel
- `\b(pharma|chemist|medical)\b` → Health
- `\b(self|own\s*a/?c)\b`, or the narration carrying the account's own last 4 digits → Transfers
- `\b(refund|reversal)\b` (credit) → Refunds

**Retrieval:**
- `NgramIndex` is built per request from at most 5,000 trusted rows. It is cached by `(max(id), count, rule-count)`.
- Text is `merchant_key(payee, narration)`. That key lowercases, drops digit runs of 4 or more, dates, UPI refs, IFSC codes and words like "upi/dr/cr/p2m", and keeps the VPA handle before `@` plus the first merchant-like segment.
- No model and no network are involved, so layers 1–4 work with Ollama off, and import stays instant.

**Rows still unresolved after layer 4** get `category = Miscellaneous` (or Other income), `category_source='fallback'` and `classify_state='pending'`.

**Layer 5 (LLM), `agent/classify.py`:**
- **Batching:** pending rows are grouped by `merchant_key`, so a 300-row statement becomes about 60 keys. Groups are split by direction. Each LLM call takes 10 keys (`RECTIFI_CLASSIFY_BATCH`, default 10).
- **Chain (LCEL):**
  - `RunnableLambda(mask_rows)` → `RunnableBranch((is_debit, debit_chain), credit_chain)`.
  - Each chain is `ChatPromptTemplate[system + FewShotChatMessagePromptTemplate(static examples + up to 5 nearest trusted neighbours per key) + human]` → `llm.with_structured_output(BatchLabels, method="json_schema", include_raw=True)`.
  - Fallback chain: `PydanticOutputParser` with `format_instructions`, for providers without native structured output.
- **Schema:** `BatchLabels.labels: list[RowLabel(i:int, category: Literal[<names of this direction's kinds>], confidence: float 0..1, why: str ≤ 80)]`.
  - The Literal is built at runtime with `pydantic.create_model`.
  - A model can't pick an income category for a debit, so `_check_kind` can't fail.
  - Missing or duplicate `i` values count as fallback.
- **Model sees:** the direction, the amount (rule 6 allows amounts), and the payee and narration after `mask_sensitive`. It never sees account names, UPI refs or raw_text.
- **Thresholds:**
  - At least 0.70 (`RECTIFI_CLASSIFY_MIN_CONF`): apply with source `llm`.
  - 0.40 to 0.70: store as `suggested_category_id`/`suggested_confidence` only. The row stays in Miscellaneous and the UI says "Maybe Food & Dining · Accept".
  - Below 0.40: nothing changes.
  - Self-reported confidence from small models is poorly calibrated. The eval (B1, below) sets the threshold.
- **Applying labels:** `tools.apply_classifications(items)` runs one UPDATE per row:
  `UPDATE transactions SET category_id=…, category_source='llm', category_confidence=…, category_reason=…, classify_state='done' WHERE id=%s AND classify_state='pending' AND category_source='fallback'`
  This guards against races: a correction you make mid-job wins. The UPDATE touches no other column.

**Background job (`agent/jobs.py`):**
- One daemon `ThreadPoolExecutor(max_workers=1)` named `rectifi-bg` (CLAUDE says one worker).
- After a confident import the job auto-queues, if `RECTIFI_AUTO_CLASSIFY` (default 1) is set and `llm_status()` is ok. Otherwise the job status is `no_model`, and the UI offers "Sort the rest when the model is on".
- Progress goes into `classify_jobs.done/total/by_source`. At startup, jobs that were `queued` or `running` are requeued; this is idempotent thanks to the guard above.
- `undo_import` sets the job to `cancelled`.
- The job is wrapped in the LangGraph `RetryPolicy` (ConnectError/Timeout, 2 tries).
- CLI: `python scripts/dev.py classify` runs the pending rows. Streamlit users can use this.

**Correction ("correct-me"):**
- The existing `PATCH /api/transactions/{id}` sends `set_category(remember=True)`. That now sets `category_source='user'`, `confidence=100`, `reason='you chose'`, clears the suggestion, and upserts the learned rule with `pattern = merchant_key(...)` and `source='learned'`.
- Today the pattern is `payee.lower()`; when the payee is None (most statement rows) no rule is learned. Using `merchant_key` fixes that.
- New `tools.apply_category_to_similar(txn_id) -> int` re-labels rows with the same merchant_key whose source is in (fallback, llm, retrieval, heuristic). It never touches `user` or `learned` rows.

**Interfaces:**
- `categorize.decide(conn, payee, narration, direction) -> Decision(category_id, source, confidence, reason, pending: bool)`
- `tools._categorize` keeps its `int` return type for callers. New: `_categorize_detail`.
- `importer.import_rows` writes the 5 new columns.
- `wire.txn` gains `category_source`, `category_confidence` (int), `category_reason`, `suggested_category`, `suggested_confidence`.

**Eval** (`evals/classify_cases.json`, about 150 hand-written fake Indian narrations with gold labels, both directions, including adversarial person-to-person UPI rows whose correct answer is Miscellaneous):
- Report per-layer coverage and accuracy, macro-F1, a confusion matrix and the share sent to Miscellaneous.
- Sweep the threshold from 0.5 to 0.9 and pick the lowest threshold where LLM-layer precision is at least 0.9.
- Command: `python -m rectifi.evals classify --live`.

### B2. Chart-code harness (feature 2)

**B2.1 Data the model sees.**
- `tools.chart_frame(start, end)`, which is SQL only, returns:
  - transactions: `date, month, weekday, amount_paise:int64, amount:float (drawing only), direction, category, category_kind, payee (mask_sensitive, max 40 characters), account`
  - budgets: `month, category, limit_paise`
  - categories
- The prompt carries only the **schema** (names and dtypes) plus **5 synthetic sample rows** with the right dtypes and fake payees. No real row ever reaches the model, even with a cloud provider, and no payee text can inject a prompt.
- The real frame goes only into the sandbox.

**B2.2 Graph** (`charts/graph.py`, StateGraph). Every model-calling node has `RetryPolicy`.

```
START → prepare (deterministic: frame, schema, synthetic sample, reference totals)
      → write_code (LLM role=code; ChatPromptTemplate + rules + 3 few-shot examples; StrOutputParser + code-fence extractor)
      → validate (ast)  ──fail──► repair ─┐
      → [review gate: interrupt() if user ticked "Review code before it runs"; resume Command(resume={"run":bool,"code":edited?})]
      → execute (sandbox) ──error/timeout──► repair ─┤
      → check_figure (deterministic) ──fail──► repair ─┤    repair = LLM with masked traceback + failed rule; attempts += 1
      → done                                            └► attempts ≥ RECTIFI_CHART_MAX_ATTEMPTS (3) → give_up
```

- **State:** `request, period, schema, sample, code, attempts, trace: Annotated[list, operator.add], result, checks, error, review`.
- Checkpointed with the shared PostgresSaver, `thread_id = "chart-<uuid>"`.
- Progress is streamed with `get_stream_writer()` (custom stream mode).
- **Time travel:** "Try again from attempt N" uses `get_state_history` and the checkpoint_id before that repair, plus `update_state({"feedback": …})`.

**B2.3 AST validator** (`charts/validate.py`, stdlib `ast`, pure):
- **Size:** at most 8 KB, 200 lines, 3,000 AST nodes.
- **Imports:** allow-list only, as `import x [as y]` or `from x import names`: pandas, numpy, matplotlib, matplotlib.pyplot, .ticker, .dates, seaborn, math, datetime, calendar, collections, itertools, textwrap. Plotly (.express and .graph_objects) is allowed only when `RECTIFI_CHARTS_PLOTLY=1`. No relative imports and no `*`.
- **Forbidden names:** eval, exec, compile, open, input, `__import__`, globals, locals, vars, getattr, setattr, delattr, breakpoint, exit, quit, help, memoryview, type (with 3 arguments).
- **Forbidden attributes:** any attribute starting with `_`, plus `to_csv, to_excel, to_pickle, to_parquet, to_sql, to_json, to_html, read_*, query, eval, savefig, show, write_image, write_html, write_json, tofile, fromfile, load, save, memmap, ctypeslib, system, popen, environ, use`.
- **Contract:** the code must assign `fig` at module level. Names it may use: `df, budgets, categories, inr(paise) -> str, PALETTE, PERIOD`.
- **Money-literal rule** (so the model can't type totals):
  1. String constants, including the literal parts of f-strings, are rejected if they match `(?i)(₹|rs\.?|inr)\s*\d`, a grouped number `\d{1,3}(,\d{2,3})+`, or 4 or more digits. Exceptions: ISO `^\d{4}-\d{2}(-\d{2})?$`, a year `^(19|20)\d{2}$`, and hex colours `^#[0-9a-fA-F]{3,8}$`.
  2. Numeric constants are allowed only if one of these holds:
     - |v| ≤ 100;
     - v is the value of a layout keyword (`figsize, dpi, rotation, fontsize, size, s, linewidth, lw, markersize, ms, bins, pad, labelpad, zorder, alpha, width, height, top, n`) and |v| ≤ 1000;
     - v is an int in 1990–2100;
     - v appears in `money.amounts_in_text(user_request)` (the same idea as rule 1: a number the user typed).
  - Rejection message: "Numbers over 100 can't be typed into chart code (they could be made-up rupee amounts). Compute them from df and format them with inr()."

**B2.4 Sandbox** (`charts/sandbox.py`, stdlib; child is `charts/_child.py`):

- **Launch:** `subprocess.Popen([sys.executable, "-I", "<abs>/_child.py", job_dir], cwd=job_dir, stdin=PIPE, …)`.
  - The environment is **scrubbed**. Only these are passed: `PATH, SYSTEMROOT (Windows), TEMP/TMP=job_dir, MPLBACKEND=Agg, MPLCONFIGDIR=<persistent ~/.rectifi/mplcache>, OPENBLAS_NUM_THREADS=1, OMP_NUM_THREADS=1, PYTHONNOUSERSITE=1, PYTHONDONTWRITEBYTECODE=1`.
  - No DATABASE_URL, no API keys, no proxies.
  - No `shell=True`. On Windows, `CREATE_NO_WINDOW`; on POSIX, `start_new_session=True`.
- **Inputs:** the parent writes `data.json` (`df.to_json(orient="split")`), `code.py` and `meta.json` into a fresh `tempfile.mkdtemp(prefix="rectifi-chart-")`.
- **Limits:**
  - Linux/macOS: `preexec_fn` sets `RLIMIT_CPU` (10 s), `RLIMIT_AS` (2 GiB), `RLIMIT_FSIZE` (20 MB), `RLIMIT_NOFILE` (64) and `RLIMIT_CORE` (0).
  - Windows: `_winjob.py` (ctypes, kernel32) creates a Job Object with `JOB_OBJECT_LIMIT_PROCESS_MEMORY` (1.5 GiB), `ACTIVE_PROCESS=1` (no child processes), `PROCESS_TIME` (10 s CPU), `KILL_ON_JOB_CLOSE` and `DIE_ON_UNHANDLED_EXCEPTION`, then assigns the child with `OpenProcess`. The child **waits on stdin for "go"** before importing anything, so it is inside the job before any user code runs. If the job object can't be applied, the run continues with the wall-clock timeout only and the trace records `limits:"timeout-only"`; the doctor warns.
  - Both platforms: a wall-clock limit `communicate(timeout=20)`, then kill (Windows: `TerminateJobObject`).
- **In the child, in this order:**
  1. Import pandas, numpy, matplotlib(Agg), seaborn (and plotly if enabled). Load the data and apply the theme rcParams.
  2. Make `socket.socket`, `create_connection` and `getaddrinfo` raise.
  3. `compile(user_code, "<chart>", "exec")`.
  4. Install a `sys.addaudithook` that blocks:
     - `socket.*`
     - `subprocess.Popen`, `os.system`, `os.exec*`, `os.spawn*`, `os.posix_spawn`, `os.fork`, `os.startfile`
     - `ctypes.*`
     - `open` for writing outside job_dir
     - `open` for reading outside the allowed roots (`sys.base_prefix`, the site-packages dirs, `matplotlib.get_data_path()`, the mplcache dir, job_dir)
     - `os.remove`, `os.rename`, `shutil.*` outside job_dir
     - every `exec`/`compile` after the one allowed exec of the pre-compiled code object (identity checked)
  5. `exec(code, namespace)`. The namespace has `__builtins__` minus the forbidden names.
  6. The **harness** saves `fig`: PNG for light and dark themes at 144 dpi, or `fig.to_json()` for plotly. It writes `result.json` with the status, a traceback trimmed to `<chart>` frames, and introspection (axes count, artists per axis, plotted y-values per series, all titles/annotations/bar_label/legend texts, axis labels).
- **The parent validates:** the PNG signature, sizes of at most 2 MB each, JSON of at most 2 MB, and the JSON schema of `result.json`. **Nothing from the child is unpickled.**
- **Threat model:**
  - Protects against: model mistakes, a prompt-injected request, accidental file, network or process use, runaway loops and memory, and reading `.env` or the database.
  - Does **not** protect against: a determined attacker with arbitrary Python execution. Audit hooks are not a security boundary (PEP 578 says so), and an exploit in numpy or matplotlib is out of scope.
  - It is also not a network sandbox: the socket patch and audit hook stop Python-level networking, not native code.
  - On Windows, nothing stops reads of the user's own files outside Python's `open` path except the audit hook.
  - This is acceptable for a single-user app because the person typing the request owns the data. In public (hosted) mode the request comes only from the authenticated owner.
  - In Docker, the container adds `read_only: true`, `tmpfs /tmp`, a non-root user and no `cap_add`.

**B2.5 Output and rendering.**
- **Default:** matplotlib or seaborn as **two PNGs** (light and dark), using the Passbook theme (`charts/theme.py`):
  - paper background `#F6F2E9` / `#17150F`;
  - accent `#1F4E5F` / `#7FBACB`;
  - rules `#D9D0BE` / `#3A352C`;
  - IBM Plex Sans/Mono TTF shipped in `charts/fonts/` (OFL), because matplotlib can't read woff2;
  - at most 2 series by default (B9).
- The JSON response carries base64 PNGs. The SPA shows them as `<img src="data:image/png;base64,…">` (the CSP already allows `img-src data:`), with `<picture>` choosing by theme.
- **Pie charts** appear only when the user explicitly asks for one. The system prompt follows B9, which allows no pie or donut charts.
- **Plotly (stretch, behind a flag):**
  - Shown in `<iframe sandbox="allow-scripts" src="/charts/{id}/frame">`. That route is an HTML page with its **own** CSP: `style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'self'`. It loads `web/public/vendor/plotly-basic-<ver>.min.js` and the figure JSON is inlined as `<script type="application/json">`.
  - Without `allow-same-origin` the frame's origin is opaque, so it can't call `/api` (the X-Rectifi-Request header, the cookie and same-origin are all denied).
  - The main app's CSP stays unchanged; `default-src 'self'` covers `frame-src`.
  - Gate: a Playwright test proves no CSP violation in the main document.

**B2.6 Final deterministic checks** (`charts/checks.py`). Each failure becomes repair feedback.
1. `fig` exists, at least one axis (or plotly trace) holds at least one data point, and nothing is NaN-only.
2. A title is present. x and y labels are present unless the chart is a pie.
3. At least 2 distinct x positions for line/bar, unless the frame has 1.
4. **Provenance:** every ₹ figure (AMOUNT regex) and every grouped number of 100 or more in the title, suptitle, annotations, bar_labels or legend must equal, at paise precision or after rupee truncation or rounding (`fmt_inr(paise=False)` truncates), one of:
   - a plotted value;
   - the sum of a plotted series;
   - a figure from `tools.chart_reference_totals(start, end)`: Decimal totals overall, by category, by month, by category×month, by day.
   - Tick labels are excluded (they are scale marks, not claims).
5. Rendered PNG size is between 5 KB and 2 MB.

The pass result and each check's verdict go into `charts.checks`.

**B2.7 Saved charts.**
- **Pin to gallery** stores the code.
- **Run again with latest data** re-executes the **saved, already validated code** through validate, sandbox and checks **without the LLM**. That is deterministic, and it means pinned charts refresh for free.

**B2.8 Eval** (`evals/chart_cases.json`, about 20 cases).
- Examples: "monthly food spend as a bar chart", "pie of last month by category", "daily spending trend vs budget", "top 5 payees last 3 months", "weekday vs weekend", "income vs spending by month", and an adversarial "say I spent ₹99,999 in the title".
- Each case has: the expected chart type (from artists: Rectangle/Line2D/Wedge), the expected x categories or bar count, which reference total must appear (where asked), attempts ≤ 3, time ≤ 120 s.
- pytest runs the harness with scripted model code (good, broken, malicious and repaired cases). `--live` measures the pass rate per Ollama model.

### B3. Knowledge Q&A: CRAG + Self-RAG (feature 3 and the Self-RAG addition)

**Corpus:**
- `guide`: built-in `rectifi/knowledge/*.md`, about 12 short plain-English guides: budgeting methods, emergency fund, UPI fraud and safety, credit card basics, SIP/mutual fund basics, student money habits, reading a bank statement, Indian tax basics (no figures), rent agreements, subscriptions, saving goals, how Rectifi works.
  - **No ₹ figures anywhere in the guides** (`test_guides_have_no_rupee_figures`). Answers therefore never get rupee amounts from guide text.
- `note`: transaction notes and weekly commitments. This is the user's own text; its ₹ figures count as user-typed.
- `txn`: one chunk per transaction, e.g. `"2026-08-14 · paid ₹1,499.00 · CULT FIT · Health · note: …"`. It is built from the database and masked. Its ₹ figures are database truth.
- `upload`: user-added .md/.txt (.pdf only if rule 8 is amended).

**Embeddings:** `OllamaEmbeddings(model=RECTIFI_EMBED_MODEL or "nomic-embed-text")` in `rag/embeddings.py`.
- Wrapped to add the task prefixes `search_document: ` and `search_query: ` (`RECTIFI_EMBED_PREFIXES=1`). Whether nomic needs them is UNSURE; the eval decides.
- Chunks are at most 1,200 characters because the model's context is 2K.
- Tests use `BowEmbeddings`, a deterministic hashed bag-of-words.

**Vector store:** `rag/store.py PgVectorStore(VectorStore)` over the plain `rag_chunks` table (`REAL[]`). It loads `(id, embedding)` into a numpy float32 matrix, cached by `(embed_model, corpus, max(id), count)`. It does cosine top-k, and MMR through `langchain_core.vectorstores.utils.maximal_marginal_relevance`. It implements `add_texts`, `similarity_search_with_score`, `max_marginal_relevance_search`, `delete` and `from_texts`.

**Why this store and not the others:**
- pgvector isn't installed on your Windows Postgres.
- Chroma and FAISS keep a second store on disk next to Postgres. That means a separate backup, and it vanishes on hosts whose disk doesn't persist.
- A Postgres table rides along with the existing database and backups, and `ON DELETE CASCADE` from `transactions` keeps undo-import correct.
- The corpus is small (guides about 300 chunks, transactions up to tens of thousands). 10k × 768 float32 is 30 MB, and cosine over it takes milliseconds.
- `InMemoryVectorStore` is used in unit tests.

**Loaders and splitters** (no langchain-community):
- `rag/loaders.py`: `GuideDirectoryLoader(BaseLoader)` with `lazy_load`; `NotesLoader` and `TransactionLoader` (database rows → `Document`); `UploadLoader` (.md/.txt; optional `PdfplumberLoader` using pdfplumber, which is already pinned).
- Splitting (`langchain-text-splitters`): `MarkdownHeaderTextSplitter` then `RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=150)`.
- Indexing (`rag/index.py`) is incremental by `content_hash`, runs on the bg worker, and is triggered at startup, on upload and on import.

**Retrievers** (`rag/retrieve.py`):
- `vs.as_retriever(search_type="mmr", search_kwargs={k:6, fetch_k:20, filter:{corpus…}})`.
- A custom `HybridRetriever(BaseRetriever)`: vector (MMR) plus a **keyword** pass (Postgres ILIKE / `NgramIndex` on txn text), fused with reciprocal-rank fusion.
- **Multi-query** is LCEL: `rewrite_prompt | llm.with_structured_output(QueryList) | RunnableLambda(fan-out) | RunnableParallel(per query)`, then union and dedupe.
- **Contextual compression** is CRAG's knowledge refinement: a relevant document is split into sentence strips, and the strips whose embedding similarity to the question is at least τ are kept (an EmbeddingsFilter-style filter).

**Unified graph** (`rag/graph.py`, a subgraph invoked by the supervisor):

```
START → decide_retrieval (Self-RAG "Retrieve?"; deterministic pre-router + LLM RouteDecision{route: tools|retrieve|direct|decline})
   ├─tools────► tools_answer (read-only ReAct SUBGRAPH: ToolNode(READ_ONLY_TOOLS)+tools_condition) ──► reflect
   ├─direct───► generate_direct ──► reflect
   ├─decline──► END ("I can only help with your money and Rectifi's guides")
   └─retrieve─► multi_query → retrieve (RunnableParallel over corpora, RRF)
                 → grade_docs (fan-out: Send("grade_one", doc) per doc → reduce)          [shared: CRAG evaluator = Self-RAG ISREL]
                 → crag_decide ─correct (≥1 'yes')────────► refine (strip filter) ──► generate
                               ├ambiguous (yes/partial mix)─► refine + also rewrite & 1 extra retrieve ──► generate
                               └incorrect (all 'no')───────► rewrite_query → [web_consent interrupt() if web on] → web_search
                                                              or broader re-retrieve (k↑, all corpora)   (max_retrievals=2) ──► grade_docs
                 generate (cited [n]; StrOutputParser) → reflect
reflect = check_support (Self-RAG ISSUP: deterministic ₹ check FIRST via unverified_amounts with allowed = tool outputs ∪ user text
          ∪ figures inside cited note/txn chunks; then LLM GroundCheck{supported: fully|partially|no, unsupported_claims})
        → check_useful (ISUSE: LLM UseCheck{useful: bool, score 1-5})
        → decide: supported∧useful → finalize
                  ¬supported ∧ gen_rounds<2 → generate (strict prompt listing unsupported claims)        [iterative]
                  ¬useful ∧ retrieval_rounds<2 → rewrite_query → retrieve                                [iterative]
                  else → finalize_with_caveat (shows the answer with "Not fully supported" verdict, or abstains)
```

**State:**
```python
class RagState(TypedDict):
    question: str; mode: Literal["auto","rag","crag","self_rag","full"]; route: str
    queries: list[str]; documents: list[Document]; grades: list[dict]           # reset per round
    crag: Literal["correct","ambiguous","incorrect"] | None
    answer: str; citations: list[dict]; support: str | None; unsupported: list[str]; unverified: list[str]; useful: bool | None
    retrieval_rounds: int; generation_rounds: int
    trace: Annotated[list[dict], operator.add]                                    # what the UI draws
```

**How Self-RAG differs from CRAG and how they combine:**
- **CRAG corrects retrieval *before* generation.** It grades documents, then refines them (correct), rewrites and searches again (incorrect), or does both (ambiguous).
- **Self-RAG critiques the generation *after* it is written.** Decide whether to retrieve, check each document's relevance (ISREL), generate, check that the answer is supported (ISSUP), check that it is useful (ISUSE), and regenerate or re-retrieve if not.
- **Shared nodes:** `retrieve`, `grade_docs` (one grader serves as both CRAG's evaluator and Self-RAG's ISREL), `rewrite_query` and `generate`.
  - CRAG-only: `crag_decide`, `refine`, `web_search`.
  - Self-RAG-only: `decide_retrieval`, `check_support`, `check_useful`, and the two loop edges.
- **Modes** are one compiled graph; conditional edges read `state.mode`:
  - `rag` = retrieve → generate → finalize
  - `crag` = always retrieve → grade → crag_decide → generate → finalize
  - `self_rag` = decide_retrieval → retrieve → grade (filter only, no correction) → generate → reflect loop
  - `full`/`auto` = everything; auto lets the router decide whether to retrieve
- **Who picks the mode:** the chat composer has an "Answer style" control (Auto · Plain · CRAG · Self-RAG · Full). The default is **Auto**, so a router decides. This also makes the eval comparison clean.
- **Honesty about Self-RAG:** real Self-RAG uses a model *fine-tuned* to emit reflection tokens ([Retrieve], [IsRel], [IsSup], [IsUse]) and ranks several candidates in parallel by those tokens. Rectifi approximates it with **prompted structured-output graders** on small Ollama models (`role=grade`): `DocGrade` (Pydantic), `GroundCheck` (TypedDict + Annotated), `UseCheck` (JSON-schema dict). It generates one candidate per round. Small-model graders are noisy, and the eval measures how noisy.

**Web search fallback:**
- **Off by default** (`RECTIFI_WEB_SEARCH=off|ddgs`).
- When on, it is used only for `guide`-type questions, never when the route touched `txn` or `note`.
- It sends only the rewritten, masked, generic query.
- It is gated by a **human-in-the-loop `interrupt()`**: "Search the web for 'how does a SIP work'? Only this query is sent to DuckDuckGo. Nothing about your money is sent." with Search / Answer without it.
- `ddgs` is an unofficial, rate-limited scraper with grey-area terms; that's why it is optional and unpinned. If it isn't installed, the route is never offered.

**Rule 1:**
- Any ₹ figure in an answer must trace to a tool result (read-only `tools.py` tools), the user's question, or a cited note/txn chunk. Guides contain none.
- Long-term memories are never in the allowed set (B4.3).
- The deterministic check runs before the LLM grader, and a failure forces regeneration, then a caveat.

**Eval** (`evals/rag_questions.json`, about 30 questions):
- About 12 answerable from guides (gold chunk ids), 6 about the user's data (must route to tools; gold tool names), 6 out-of-scope (must abstain or ask for web consent), and 6 adversarial (invite made-up ₹ figures or claims the guides don't make).
- Metrics: retrieval hit@6, groundedness pass rate (deterministic ₹ check AND grader 'fully' AND required/forbidden keywords), usefulness pass rate (grader plus required keywords), abstention accuracy, routing accuracy, p50/p95 latency.
- Every metric is reported for the modes **plain RAG vs CRAG vs Self-RAG vs Full**.
- pytest runs the graph with a scripted model to prove routing, loop bounds and the ₹ check. `--live` runs on the laptop.

### B4. Chat: supervisor, memory, observability, and the chat window

**B4.1 Supervisor graph** (`agent/supervisor.py`), `MessagesState` plus `{route, mode, summary, memories, artifacts}`:

```
START → load_context  ─┬─ (parallel branch) fetch_memories (Store search, namespace ("users", uid, "memories"))
                       └─ (parallel branch) trim_or_summarize (short-term: trim_messages to ~3k tokens; if >12 turns,
                                              summarise older turns into state.summary with role=grade model)
      → route (deterministic first: explicit chip /do /lookup /chart; regex for "chart|plot|graph" → chart,
               "how much|total|spent|when did I|budget" → agent; else LLM RouteDecision)
      ─conditional─► agent (EXISTING guarded ReAct graph, as a SUBGRAPH node; write tools keep _guard)
                   ► knowledge (B3 graph as SUBGRAPH; receives mode)
                   ► chart (B2 graph as SUBGRAPH; returns chart id; AIMessage carries artifact {chart_id})
                   ► smalltalk (one LLM call, no tools)
      → verify_amounts (deterministic unverified_amounts over the turn) → END
```

**Where each workflow pattern is used:**

| Pattern | Where |
|---|---|
| Sequential | classification layers 1→4; chart prepare→write→validate→execute→check; supervisor load_context→route |
| Parallel | supervisor fetch_memories ∥ trim_or_summarize; RAG multi-query retrieval (RunnableParallel); CRAG doc grading (Send fan-out) |
| Conditional | supervisor route; Self-RAG decide_retrieval; crag_decide; chart validate/check pass-fail; RunnableBranch by direction in the classifier |
| Iterative (loop) | agent↔tools loop; chart repair loop (≤3); Self-RAG regenerate (≤2) and re-retrieve (≤2); classifier batch loop until no pending rows |

**B4.2 Short-term memory:**
- Per-thread state lives in the pooled PostgresSaver; `thread_id` = conversation id.
- `trim_messages(strategy="last", token_counter=approx 4 chars/token, max_tokens=OLLAMA_NUM_CTX*0.4, start_on="human", include_system=True)`.
- Summarisation keeps the full history in the checkpoint and only shortens what is sent. This replaces `_recent_window` for the supervisor; the old agent path keeps it for its tests.

**B4.3 Long-term memory:**
- `PostgresStore` (no semantic index, because that needs pgvector), namespace `("users", uid, "memories")`. uid is `"owner"`, or the Tailscale login / auth subject.
- An item is `{kind: preference|fact|correction, text, created_at, thread_id}`.
- Memories are written only on an explicit "remember …" (a `remember_fact` tool with an interrupt confirmation "Save this to memory?") or from the Memory page. Corrections are not copied into the store; the supervisor reads learned rules directly from `merchant_rules` as context ("You told Rectifi: cult fit → Health").
- **Rule 1:** memory text is masked, and its ₹ figures are replaced with "[amount: check your records]" in prompts (`memory.redact_amounts`). `unverified_amounts` never trusts memory text.
- **UI:** a Memory page lists every memory with edit, delete and "Forget everything".

**B4.4 Observability:**
- **Local (default, ₹0):** `agent/tracing.py LocalTraceHandler(BaseCallbackHandler)` is attached to every graph's config callbacks. It writes spans (chain, LLM, tool, retriever) with latency, model, token counts, status and error, plus **masked, 2 KB-truncated** previews, into `trace_spans`. Retention is 14 days or 20k rows, pruned by the bg worker.
- A **Traces** page (in the SETUP group) shows recent runs and a span tree. It is enough to answer "which node was slow, what did the grader say, why did it fail".
- **LangSmith (optional):** when `RECTIFI_LANGSMITH=1` plus `LANGSMITH_API_KEY`, and `LANGSMITH_TRACING=true` is forwarded, runs get `run_name`, `tags=[workstream, role]` and `metadata={thread_id, mode}`.
- **Privacy guard:** if `LANGSMITH_TRACING` is set in the environment but `RECTIFI_LANGSMITH` isn't 1, the app forces it off at startup and logs one line. The doctor says traces leave the laptop. I don't know the current LangSmith free-tier limits; check before relying on it.
- **Rejected:** Langfuse self-host (needs Postgres, ClickHouse, Redis and S3) and Arize Phoenix (another server and dependency tree). Neither is realistic beside Ollama on 12 GB.

**B4.5 Conversations API** (`routes/chat.py`, using `tools` chat section and `conversations` table):
- `GET /api/chat/threads?q=` searches `title ILIKE` or `search_text ILIKE`, where `search_text` is masked user and assistant text appended per turn and capped at 20 KB.
- `POST /api/chat/threads` creates; `PATCH /api/chat/threads/{id}` renames; `DELETE` calls `saver.delete_thread` and deletes the row.
- `GET /api/chat/threads/{id}` returns the full history; each message has a timestamp in `additional_kwargs.ts`, set when the message is created.
- `POST /api/chat/threads/{id}/messages` streams NDJSON:
  - `{type:"token",text}` from `stream_mode="messages"`, filtered to the final nodes by `metadata.langgraph_node` and a `final` tag;
  - `{type:"step",…}`, `{type:"trace",…}` (RAG/chart verdicts), `{type:"interrupt",kind,payload}`;
  - `{type:"done",items}` or `{type:"error",message,detail}`.
- `POST …/stop` sets a cancel flag. The worker checks it between graph steps and closes the model stream; closing the Ollama HTTP stream stops generation. An in-flight non-streaming grader call finishes first.
- `POST …/regenerate` does **time travel**: it finds in `get_state_history` the checkpoint just before the last AI turn and re-invokes from that `checkpoint_id`. That forks the thread, and the old branch stays in history.
- `POST …/resume` sends `Command(resume=…)` for interrupts.
- The old `/api/ask/*` stays as an alias, so the Streamlit app and tests keep working.

### B5. Ollama models and "the API is not responding" (features 4 and 7)

**Settings** (config.py; `get_llm(role)` and `get_embeddings()`):
- `RECTIFI_MODEL_CHAT` (default `OLLAMA_MODEL`), `RECTIFI_MODEL_CLASSIFY`, `RECTIFI_MODEL_CODE`, `RECTIFI_MODEL_GRADE`, `RECTIFI_EMBED_MODEL`.
- `OLLAMA_NUM_CTX` (8192), `OLLAMA_TIMEOUT` (120 s, passed as `client_kwargs={"timeout":…}`), `OLLAMA_KEEP_ALIVE` (30m).
- `ChatOllama(reasoning=False)` for qwen3, so thinking is off.
- Every other provider keeps working: a role var just names a model for the configured provider. Embeddings are Ollama-only; without them, RAG is off and classification still works through layers 1–4.

**Recommendations.** The doctor reads RAM (Linux `/proc/meminfo`; Windows `ctypes GlobalMemoryStatusEx`; macOS `sysctl -n hw.memsize`) and `nvidia-smi` presence, then prints one tier:

| RAM | chat/agent (tools) | classify | code | grade | embed |
|---|---|---|---|---|---|
| ≤ 8 GB | llama3.2:3b | qwen2.5:3b | qwen2.5-coder:3b | qwen2.5:3b | nomic-embed-text |
| 12–16 GB (likely your laptop) | **qwen2.5:7b** (or qwen3:8b, reasoning off) | qwen2.5:3b (speed) | **qwen2.5-coder:7b (already pulled)** | qwen2.5:3b | nomic-embed-text |
| ≥ 24 GB or GPU | qwen3:14b | qwen2.5:7b | qwen2.5-coder:14b | qwen2.5:7b | nomic-embed-text |
| Oracle A1 (2 cores / 12 GB) | qwen2.5:3b | qwen2.5:3b | qwen2.5-coder:3b | qwen2.5:3b | nomic-embed-text |

- **Keep qwen2.5-coder:7b for chart code, not for the Ask agent.** Ollama tags it `tools`, but coder models are trained for code, and I believe they often write tool calls as JSON text in `content`. That belief is unverified; the doctor's tool round-trip decides.
- **Modelfiles** (Ollama masterclass concept): `deploy/Modelfile.classify` and `Modelfile.chartcoder` with `FROM`, `SYSTEM`, `PARAMETER temperature 0`, `PARAMETER num_ctx 8192`. `dev.py ollama-setup` runs `ollama pull` for the tier and `ollama create rectifi-classify -f …` (list arguments, no shell).

**Likely causes of "the API is not responding"** (most likely first):
1. **The AQ. key.** Google's forum says AI Studio now issues only "Authentication Keys" starting `AQ.`, and people report they fail on generativelanguage REST. `ChatOpenAI` sends `Authorization: Bearer AQ…`, so the call probably got 400 "API key not valid" or 401 "ACCESS_TOKEN_TYPE_UNSUPPORTED". `explain_error` handles only 401, 402, 404 and 429, so a 400 shows the raw exception. UNSURE which exact code you got.
2. **The model id `gemini-3.8-flash`.** I don't know whether that id exists. A 404 would show the misleading "isn't available for tool calling".
3. **`llm_status()` for openai_compat only checks that settings are filled in.** The Ask page said "ready", and the doctor printed "ok … (not tested: that needs the internet)". Nothing ever made a real call, so the first sign of failure was a hang.
4. **Hangs look like "not responding".** A 60 s timeout × (1 + 2 retries) is up to 3 minutes per model call, and one agent turn can make up to about 8 calls (`recursion_limit` 16). `ChatOllama` has **no timeout at all** today.
5. **After switching to Ollama**, any of these:
   - `.env` is read once at import (`load_dotenv` in config.py), so the server **must be restarted**.
   - The model name must match exactly (`qwen2.5-coder:7b`).
   - The first load on a CPU can take 20–90 s.
   - Ollama's default context (version-dependent; I believe 2048 or 4096) can truncate the system prompt plus 11 tool schemas, and the model then ignores the tools.
   - A coder model may answer with JSON text instead of a tool call.
6. Windows `localhost` may try `::1` first while Ollama listens on 127.0.0.1. That adds delay, and fails only if IPv4 fallback is broken (UNSURE). The doctor suggests `OLLAMA_BASE_URL=http://127.0.0.1:11434`.

**Diagnostics so it can't be silent again:**
- `llm.probe(role="chat", timeout=20) -> dict{ok, stage, http_status, provider_message, latency_ms, tools_ok, hints[]}`. Stages:
  1. reachable: Ollama `/api/version`; for others, the request itself;
  2. model present: `/api/tags` and `/api/show`;
  3. capabilities include `tools` (Ollama), and whether `num_ctx` ≥ 8192 is configured;
  4. a tiny chat ("Reply with the word OK", `num_predict` 5);
  5. a tool round-trip (bind a dummy `ping(x:int)`, "call ping with x=1"). It checks that `tool_calls` came back, and flags "the model wrote the tool call as text" when the content parses as `{"name":…}`.
  - Result cached for 60 s.
- `explain_error v2` adds:
  - 400 with "API key not valid" / "ACCESS_TOKEN_TYPE_UNSUPPORTED" → the AQ-key explanation;
  - 403;
  - 404 split into "model not found" vs "no tool support";
  - Ollama `ResponseError` "does not support tools" → suggest a model;
  - `httpx`/`openai` timeouts and connection-refused;
  - otherwise `provider_message` = status + the body's `error.message`, passed through `redact()` (`AQ\.[\w-]+`, `AIza[\w-]{20,}`, `sk-[\w-]+`, `hf_\w+`, `Bearer \S+`) and `mask_sensitive`, cut to 300 characters.
- `dev.py doctor` (stdlib, urllib, 30 s):
  - **real round-trip** for Ollama (`/api/chat` with a tool, `stream:false`) and OpenAI-style providers (`POST {base}/chat/completions`). It prints the HTTP status and provider message, never the key.
  - Plus the RAM tier and model suggestion, embed model present, `/api/ps` loaded models, the LangSmith privacy warning and sandbox limits support.
  - `--quick` skips network calls.
- **UI:** `GET /api/models/status` and `POST /api/models/probe`. A `ModelCheck` component shows stamp rows (REACHABLE, MODEL FOUND, REPLIED 3.2 s, TOOLS OK / TOOLS AS TEXT / NO TOOLS) with a "Details from the provider" disclosure. It appears on the chat page when the status isn't ok, after any error, and under a "Test the model" button.
- **Chat worker:** after 20 s without a token it emits `{type:"slow"}`, shown as "The model is still loading. The first answer on a laptop CPU can take about a minute." OpenAI-style `max_retries` drops to 1. The worker logs the exception type and status (no keys) with `log.warning`.

---

## C. CampusX concept coverage

**What I verified about the playlists:**
- LangChain (YouTube playlist `PLKnIA16_RmvaTbihpo4MtzVm4XOQa0ER0`, 21 videos). The CampusX course page lists 20: GenAI Roadmap, Generative AI using LangChain, Introduction, Components, Models, Prompts, Structured Output, Output Parsers, Chains, Runnables, Runnables Part 2, Document Loaders, Text Splitters, Vector Stores, Retrievers, RAG, YouTube Chatbot (RAG), Tools, Tool Calling, End-to-end AI Agent.
- CampusX's **"Ollama Masterclass 2026"** (published 17 Jan 2026) has these chapters: library, hardware, CLI basic/advanced (inspect/tune), Python library, images and parameters, conversations/history, tool calling deep dive and demo, Modelfiles, REST API, LangChain orchestration, Ollama Cloud, desktop app. **UNSURE** that it is the 21st (final) playlist item; that matches what you said, and I could not confirm it.
- LangGraph course page: 26 lectures, listed in the table below.
- Sub-topics inside each video come from the chapter lists I could fetch (Structured Output: TypedDict/Pydantic/JSON; Agent: ReAct, Agent & AgentExecutor). Where I couldn't confirm a sub-topic, it is marked (U).

Test names below are to be created; `docs/CONCEPTS.md` holds this table, and `tests/test_concepts_doc.py` checks that every referenced test exists.

**LangChain playlist**

| Concept | Where in Rectifi | Test that proves it |
|---|---|---|
| GenAI roadmap, LangChain intro, components (1–4) | docs/CONCEPTS.md overview | test_concepts_doc |
| Chat models (open-source via Ollama) | agent/llm.get_llm(role) → ChatOllama with options | test_llm_roles::test_role_models_and_options (fake Ollama server) |
| Embedding models + similarity | rag/embeddings.py OllamaEmbeddings + cosine in PgVectorStore | test_rag_store::test_ollama_embeddings_via_fake_server, ::test_cosine_topk |
| PromptTemplate / ChatPromptTemplate / MessagesPlaceholder | classifier, chart, RAG, supervisor prompts | test_prompts::test_templates_render |
| Few-shot prompts | classifier FewShotChatMessagePromptTemplate with retrieved neighbours | test_classify_llm::test_neighbours_in_prompt |
| Saving/loading prompts (U) | `load_prompt` for chart system prompt JSON | test_prompts::test_load_prompt_file |
| Structured output: Pydantic | classifier BatchLabels, DocGrade | test_classify_llm::test_enum_constrains_kind |
| Structured output: TypedDict + Annotated | GroundCheck | test_rag_graders::test_groundcheck_typeddict |
| Structured output: JSON schema | UseCheck, RouteDecision | test_rag_graders::test_usecheck_json_schema |
| StrOutputParser | RAG generate, chart write_code | test_rag_graph::test_generate_chain |
| PydanticOutputParser (+ format instructions) | classifier fallback for providers without json_schema | test_classify_llm::test_parser_fallback |
| JsonOutputParser | multi-query rewrite fallback | test_rag_retrieve::test_multiquery_json_parser |
| Sequential chain | prompt \| model \| parser everywhere | test_rag_graph::test_generate_chain |
| Parallel chain / RunnableParallel | multi-corpus, multi-query retrieval | test_rag_retrieve::test_parallel_retrieval_union |
| Conditional chain / RunnableBranch | classifier debit/credit branch | test_classify_llm::test_branch_by_direction |
| RunnableSequence / Passthrough / Lambda | `RunnablePassthrough.assign(context=…)`; `RunnableLambda(mask_sensitive)` before every model | test_privacy_runnables::test_mask_lambda_first |
| LCEL | all of the above | same |
| Document loaders (custom BaseLoader, lazy_load) | GuideDirectoryLoader, NotesLoader, TransactionLoader, (Pdfplumber)UploadLoader | test_rag_loaders::* |
| Text splitters (structure + recursive) | MarkdownHeaderTextSplitter + RecursiveCharacterTextSplitter | test_rag_loaders::test_split_sizes |
| Text splitters, semantic chunker (U) | **not used** (langchain_experimental); stated | — |
| Vector stores | PgVectorStore(VectorStore); InMemoryVectorStore in tests | test_rag_store::* |
| Retrievers: as_retriever, MMR, score threshold | retrieve.py | test_rag_retrieve::test_mmr_diversity |
| Multi-query retriever | LCEL implementation (no langchain-classic) | test_rag_retrieve::test_multiquery |
| Contextual compression | CRAG `refine` strip filter | test_rag_graph::test_refine_drops_irrelevant_strips |
| Custom retriever (BaseRetriever) | HybridRetriever (vector + keyword, RRF) | test_rag_retrieve::test_hybrid_rrf |
| RAG | knowledge subgraph (mode rag) | test_rag_graph::test_plain_rag_mode |
| Tools: @tool, StructuredTool, BaseTool, toolkit | existing @tool; `RectifiReadOnlyToolkit(BaseToolkit)`; `StructuredTool.from_function` for remember_fact | test_agent_toolkit::* |
| InjectedToolArg | `search_entries(query, *, uid: Annotated[str, InjectedToolArg])` | test_agent_toolkit::test_injected_arg_hidden_from_schema |
| Tool calling (bind_tools, ToolMessage) | existing agent + probe | test_agent (existing), test_llm_probe::test_tool_roundtrip |
| Agents (ReAct; AgentExecutor) | existing ReAct LangGraph agent; **AgentExecutor not used** (legacy, in langchain-classic); stated | test_agent (existing) |
| Ollama: library, hardware sizing | doctor RAM tier table | test_devtools_models::test_ram_tiers |
| Ollama: CLI (pull/list/show/ps/create) | dev.py ollama-setup command lists | test_devtools_models::test_commands_are_lists |
| Ollama: REST API (/api/tags, show, chat, embed, ps, version) | doctor + llm.probe | test_llm_probe::* (fake server) |
| Ollama: Python library | `ollama.Client().show()` in llm.capabilities (the `ollama` package is already installed via langchain-ollama, so pin it) | test_llm_probe::test_capabilities_client |
| Ollama: model parameters (temperature, num_ctx, seed, num_predict, keep_alive) | get_llm options | test_llm_roles::test_options |
| Ollama: conversation history | supervisor messages | test_chat_threads::test_resume_history |
| Ollama: tool calling | agent on ChatOllama via fake server | test_llm_probe::test_tool_roundtrip |
| Ollama: Modelfiles | deploy/Modelfile.* + ollama-setup | test_devtools_models::test_modelfiles_valid |
| Ollama: images/vision | **stretch:** receipt photo pre-fills the Add form; the user confirms the amount | test_receipt_prefill (stretch) |
| Ollama Cloud, desktop app | **not used:** cloud sends data off the laptop; README mention only | — |

**LangGraph playlist** (MCP excluded)

| # | Concept | Where | Test |
|---|---|---|---|
| 1–4 | Agentic vs generative, LangChain vs LangGraph | docs/CONCEPTS.md | test_concepts_doc |
| 5 | Core: state, reducers (`add_messages`, `operator.add`), nodes, edges, START/END, compile | every graph | test_rag_graph::test_trace_reducer_accumulates |
| 6 | Sequential workflow | chart prepare→…→check; supervisor | test_chart_graph::test_happy_path_order |
| 7 | Parallel workflow (fan-out/fan-in, Send) | grade_docs Send; load_context branches | test_rag_graph::test_grades_fan_out; test_supervisor::test_parallel_context |
| 8 | Conditional workflow | route, crag_decide, reflect | test_supervisor::test_routes; test_rag_graph::test_crag_branches |
| 9 | Iterative workflow (bounded loops) | chart repair; Self-RAG loops | test_chart_graph::test_repair_bounded; test_rag_graph::test_regen_bounded |
| 10 | Chatbot | supervisor chat | test_chat_api::test_turn |
| 11 | Persistence (checkpointer, thread_id, get_state, get_state_history, time travel, update_state, fault tolerance) | pooled PostgresSaver; regenerate = fork; chart "retry from attempt N"; resume after a node crash | test_persistence::test_time_travel_fork, ::test_resume_after_failure, ::test_update_state |
| 12 | Chatbot with UI | React chat window (plus the existing Streamlit Ask fallback) | test_web_e2e_chat |
| 13 | Streaming (updates, messages tokens, custom) | chat NDJSON tokens; chart progress via get_stream_writer | test_chat_api::test_streams_tokens; test_chart_api::test_progress_events |
| 14 | Resume chat (thread list) | conversations sidebar | test_chat_threads::test_list_search_rename_delete |
| 15 | Chatbot + database | PostgresSaver (Postgres instead of SQLite) | test_chat_threads::test_survives_new_graph |
| 16–17 | LangSmith, observability | optional LangSmith (env) + local trace_spans + Traces page | test_tracing::test_local_spans_written, ::test_langsmith_forced_off_without_optin |
| 18 | Tools in LangGraph (ToolNode, tools_condition) | read-only tools subgraph in RAG | test_rag_graph::test_tools_route_uses_toolnode |
| 19 | MCP client | **excluded by request** | — |
| 20 | RAG using LangGraph | knowledge graph | test_rag_graph::* |
| 21 | Human-in-the-loop (interrupt, Command resume) | chart "review code first"; web-search consent; remember_fact confirm | test_chart_graph::test_interrupt_resume; test_rag_graph::test_web_consent_interrupt |
| 22 | Subgraphs | agent, knowledge, chart and tools subgraphs under the supervisor | test_supervisor::test_subgraph_state_mapping |
| 23–24 | Short-term memory (trim, summarise) | memory.trim_or_summarize | test_memory::test_trim_fits_ctx, ::test_summary_node |
| 25 | Long-term memory (Store, namespaces) | PostgresStore ("users",uid,"memories") + Memory page | test_memory::test_store_crud_namespace, ::test_memory_never_supplies_rupees |
| 26 | Orchestrator-worker project (blog agent) | the pattern (Send fan-out workers + reducer) is used in grading; the blog project itself isn't replicated | test_rag_graph::test_grades_fan_out |
| extra | CRAG, Self-RAG | B3 | test_rag_graph::test_modes_matrix; evals |
| extra | RetryPolicy | model nodes | test_persistence::test_retry_policy |

---

## D. UI changes (Passbook design language)

These follow docs/WEB_PLAN.md part B: no card grid; a border only on slips and overlays; stamps always carry a word; one accent; no pie charts in our own UI; reduced-motion respected; 44 px targets.

**D1. Add > Import result: new "Categories" section under the existing slip** (`components/add/ImportCategories.tsx`; data from `GET /api/imports/{batch}/rows?show=all|misc|model|suggested` and `GET /api/classify/jobs/{id}`):
- **Summary eyebrow:** "312 ENTRIES · 241 BY RULES · 38 LIKE YOUR PAST CHOICES · 20 BY THE LOCAL MODEL · 13 IN MISCELLANEOUS".
- **Progress line** (role=status, live): "Sorting the rest with the local model… 120 of 180". This is a real count, and a native `<progress>` is allowed because it is honest. Other states: "Model is off. 13 entries wait in Miscellaneous. [Sort them when the model is on]", and "done".
- **Filter control** (SegmentedControl): All · Miscellaneous · Decided by the model · Suggestions.
- **Rows:**
  - Desktop columns: Date | Payee (narration small, muted) | ‖ Amount | Category (inline native select, the "correct me") | Source.
  - The Source column is a stamp: YOURS (accent-tint; user/learned), RULE (neutral), PATTERN (neutral; heuristic), SIMILAR (neutral; retrieval), MODEL 82% (neutral, mono percent), UNSURE (near tone; fallback).
  - A "Why" disclosure shows `category_reason`.
  - On a phone, two lines per row; tapping a row opens the EntrySheet with the category select.
- **Suggestion row:** "Maybe Food & Dining (62%) · Accept".
- **After a correction:** a toast "Saved. Rectifi will use Health for 'cult fit' from now on." with the action "Apply to 6 similar entries", which calls `POST /api/transactions/{id}/apply-similar`.
- **Ledger:** a muted Source column on desktop (the same stamp, smaller), plus a filter `?source=`.

**D2. Charts page (`/charts`, RECORDS group; also in the More sheet):**
- **Header:** eyebrow CHARTS, h1 "Draw your spending".
- **Prompt box:** a textarea with a period select (default last 3 months), example buttons ("Monthly food spend, bar", "Last month by category", "Daily spending vs budget pace"), a checkbox "Let me read the code before it runs" (the interrupt), and the primary button **Draw**.
- **Working state:** live attempt lines ("Writing code…", "Checking the code… ✓", "Running… ✓", "Checking the figure… ✗ the title says ₹4,200, which isn't in your data; fixing").
- **Result slip:**
  - The image (`<picture>` light/dark), with alt text built from the server's checks summary.
  - Caption: "Drawn by chart code from 214 entries, 1 Jul–30 Sep. Checked: has data · labelled · figures match Rectifi's totals".
  - Actions: Pin to gallery, Run again with latest data, Delete.
- **Disclosures:**
  - "Show code": mono, line numbers, Copy (a CodeValue variant).
  - "How it got here · 2 attempts": rows with stamps VALIDATION FAILED / RAN / ERROR / CHECK FAILED / PASSED, a one-line reason and a collapsed traceback, plus "Try again from here" (time travel).
- **Review-gate state:** the code in an editable mono textarea, with "Run this code" and "Cancel".
- **Gallery:** a ruled list, not cards. Each row has a 160 px thumbnail, title, prompt (muted), "updated 25 Oct", a PINNED stamp and "Run again". Empty state: "No saved charts yet. Draw one above and pin it."
- **Model off:** a near callout using the ModelCheck text. Saved charts still re-run, because that path doesn't need the model.

**D3. The chat window (the `/ask` route grows into it; nav label "Ask"):**
- **Desktop layout:** a 280 px conversation column inside the main area (the app sidebar stays).
  - Top: "New conversation" (secondary) and a search input.
  - List grouped by TODAY / THIS WEEK / EARLIER eyebrows. Each row has the title (600), preview (muted, one line) and time (mono, muted), plus a "⋯" menu (Rename inline, Delete with a confirm dialog).
- **Phone:** the list is a sheet opened from the top-bar "Conversations" button.
- **Transcript:** keeps the existing YOU/RECTIFI eyebrows (no bubbles).
  - Each message shows its time in mono muted (e.g. "14:02").
  - Assistant answers show a ROUTE stamp: DID IT (agent), LOOKED IT UP (knowledge), CHART, CHAT.
  - Streaming tokens render in place.
  - A **Stop** button (secondary, square icon plus the word) replaces Send while a turn runs.
  - **Regenerate** is a tertiary button under the last reply ("Answer again"). The old answer stays reachable through "Earlier answer (1 of 2)" arrows, because regenerate forks the thread.
- **Knowledge answers:**
  - Citations as superscript `[1]` links, then a "Sources" list. Each source has a corpus stamp (GUIDE / YOUR NOTE / YOUR ENTRY), a title and a snippet.
  - Disclosure "How this answer was checked" (the reflection trace, a ruled list of stamp + sentence):
    - ROUTED: look it up
    - RETRIEVED: 6 passages
    - GRADED: 3 relevant · 1 partly · 2 not
    - CRAG: CORRECT (or AMBIGUOUS / INCORRECT → "Asked again as: …")
    - GENERATED: attempt 1
    - SUPPORTED: FULLY / PARTLY / NO, with the unsupported claims listed
    - USEFUL: YES / NO
    - AMOUNTS: "No ₹ figures" or "Every ₹ figure traced to Rectifi's records"
- **Chart answers:** embed the chart slip (D2) inline, with "Open in Charts".
- **Interrupt cards:** a slip with near tint, e.g. "Search the web for '…'?" with Search / Answer without it, or "Save this to memory?" with Save / Don't.
- **Composer:** the existing composer, plus a compact "Answer style" select (Auto · Plain · CRAG · Self-RAG · Full) and quick chips Do · Look up · Chart (they set the route).
- **Errors:** `error.message` plus a "Details from the provider" disclosure and the ModelCheck button.

**D4. Memory page (`/memory`, SETUP):**
- "What Rectifi remembers about you": rows with a kind stamp (PREFERENCE/FACT), the text, the date and the source conversation link.
- Edit is inline; Delete; "Forget everything" opens a danger confirm.
- Note: "Memories never supply amounts. Figures always come from your records."
- A separate read-only section lists "Category rules you taught Rectifi" (learned merchant_rules), each with a Remove action.

**D5. Library documents** (inside `/memory`? No: a `/library` page in SETUP):
- Built-in guides (GUIDE stamp, chunk count) and your uploads (upload .md/.txt, remove).
- Index status: "Indexed with nomic-embed-text · 214 passages · updated 25 Oct", plus a "Re-index" button.

**D6. Traces page (`/traces`, SETUP):** a ruled list of runs (time, entry: chat/chart/classify, route, total latency, status stamp). Selecting one shows the span tree (indented rows: name, type, model, ms, tokens, and a masked preview disclosure).

**D7. Login page (`/login`; shown only when the session is required):**
- Wordmark, "every rupee, accounted for".
- A password field (`autocomplete="current-password"`) and "Sign in".
- Errors: "That password isn't right." and the locked state "Too many tries. Wait 15 minutes."
- A sign-out item goes in the sidebar footer and the More sheet.

**Nav (nav.ts):**
- DAILY: Overview, Review, Insights, Ask.
- RECORDS: Ledger, Charts, Budgets, Accounts.
- SETUP: Phone, Library, Memory, Traces.
- The More sheet adds Charts, Library, Memory and Traces.
- `PAGE_NAMES` gets the new paths.

**Self-critique:** after integration, take screenshots at 1280 and 390 px, light and dark (`web/scripts/screenshots.py`). Fix at least 5 issues; log them in PROGRESS like phase 7 did.

---

## E. Hosting

**Primary: Oracle Cloud Always Free A1 (2 OCPU Arm, 12 GB RAM, 200 GB block storage) + Docker Compose + Tailscale.**
- Use Tailscale **Serve** for private access (recommended for bank data). Use Tailscale **Funnel** only if you want a public URL; with Funnel the app login is mandatory.
- Why this one: it is the only verified ₹0 option that can run Ollama next to Postgres, always on.
- **Risks:**
  - Card verification at sign-up. I don't know whether Indian debit/RuPay cards are accepted.
  - "Out of host capacity" in the home region.
  - Idle reclamation. All three idle tests (CPU, network, memory) must be true for Oracle to call the VM idle, so keeping one model loaded (`OLLAMA_KEEP_ALIVE=-1`, about 2.5 GB, which is over 20% of 12 GB) should keep memory over the line. That is my reading of the rule; UNSURE.
  - A1 is Arm. `ollama/ollama`, `postgres:16` and `python:3.13-slim` are multi-arch.

**Fallback: your laptop + Tailscale (Serve for private, Funnel for public), same Docker or native `dev.py up`.**
- ₹0, uses your full RAM, and the data never leaves your machine. But it only works while the laptop is on, which you said it can't be all the time.
- I couldn't confirm from the plans page whether Funnel is on the free Personal plan. I believe it is; `tailscale funnel` tells you if it isn't.

**Always-on without Ollama (documented, not recommended): Render free web service + Neon free Postgres.**
- 512 MB with spin-down; Neon sleeps after 5 minutes and has 1 GB.
- LLM features use OpenRouter `:free` or stay off. Classification layers 1–4 and pinned-chart re-runs still work. The sandbox may hit the 512 MB limit.

**Rejected:**
- HF Spaces (Docker needs PRO).
- Koyeb (no free compute).
- Fly (trial only).
- Railway ($1 a month after the trial).
- Supabase as the database: widely reported to pause after about 7 days idle; not checked first-hand here.

**Files (WS-HOST):**
- `deploy/Dockerfile` (app):
  - `FROM python:3.13-slim-bookworm`, with a tag pin; add a digest later.
  - `ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RECTIFI_BIND_HOST=0.0.0.0 RECTIFI_PUBLIC=1`.
  - Non-root `useradd -u 10001 rectifi`.
  - `pip install --no-cache-dir -r requirements.txt`.
  - Copy `rectifi/`, `web/dist/`, `scripts/`.
  - `HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8501/api/health',timeout=3)"`.
  - `CMD ["python","-m","uvicorn","--factory","rectifi.api.app:create_app","--host","0.0.0.0","--port","8501","--workers","1"]`.
  - No `--proxy-headers`: we don't trust X-Forwarded-*.
- `deploy/compose.oracle.yml`:
  - `db`: postgres:16-bookworm, volume `pgdata`, `pg_isready` healthcheck, no ports.
  - `ollama`: ollama/ollama with a pinned tag, volume `ollama`, env `OLLAMA_KEEP_ALIVE=-1 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=2`, `mem_limit: 8g`, no ports.
  - `ollama-pull`: a one-shot that runs `ollama pull qwen2.5:3b qwen2.5-coder:3b nomic-embed-text` against `OLLAMA_HOST=ollama:11434`, then `restart: "no"`.
  - `app`: `build: {context: .., dockerfile: deploy/Dockerfile}`, `env_file: ../.env.deploy`, `DATABASE_URL=postgresql://rectifi:${POSTGRES_PASSWORD}@db:5432/rectifi`, `OLLAMA_BASE_URL=http://ollama:11434`, `RECTIFI_ALLOWED_HOSTS=${RECTIFI_HOSTNAME}`, `ports: ["127.0.0.1:8501:8501"]`, `read_only: true`, `tmpfs: [/tmp]`, `mem_limit: 2g`, `depends_on` db healthy.
  - `backup`: postgres image, a daily `pg_dump -Fc` to `/backups` (block volume), keeping 7.
- Process supervision: compose `restart: unless-stopped` per container, so no supervisord.
- `deploy/cloud-init.yaml`: Docker CE + compose plugin, Tailscale install, a 4 GB swapfile, unattended-upgrades, and **no ingress rules opened** (Serve and Funnel need only outbound).
- `.dockerignore`: `.env*`, `.venv`, `web/node_modules`, `statements/`, `*.csv`, `*.xlsx`, `*.pdf` (except `tests/fixtures`), `.git`.
- `docs/HOSTING.md`: step by step (create the VM, `tailscale up`, `docker compose up -d`, `tailscale serve --bg 8501` or `tailscale funnel --bg 8501`, `dev.py set-password`, backups, restore, updates).
- `.env.deploy.example`.

**Auth** (`api/auth.py`, `SessionLock` in security.py, `routes/auth.py`; stdlib only):
- `dev.py set-password` uses `getpass`. It needs at least 12 characters and computes `hashlib.scrypt(n=2**14, r=8, p=1, dklen=32)` with a random 16-byte salt. It writes `RECTIFI_PASSWORD_HASH=scrypt$16384$8$1$<salt_b64>$<hash_b64>` and `RECTIFI_SESSION_SECRET=<secrets.token_urlsafe(48)>` to `.env` (or prints them for `.env.deploy`). It never prints the password.
- **Session cookie** `rectifi_session` = base64url(`{"iat","exp","epoch"}`) + `.` + HMAC-SHA256. Flags: HttpOnly, SameSite=Strict, Secure when `RECTIFI_PUBLIC=1`, Path=/, 7-day expiry. Checked with `hmac.compare_digest`. `epoch` must equal `app_settings.session_epoch` (that's "sign out everywhere").
- **Middleware position:** after HostGuard, before ApiGuard.
  - When `RECTIFI_PASSWORD_HASH` is set, `/api/*` (except `/api/health` and `/api/auth/*`) needs a valid session, else 401 `auth_required`. The SPA then routes to `/login`.
  - Static assets and index stay public (they hold no data).
- **Login rate limit:** global, in memory and in `app_settings`. After 5 failures in a minute, there is a 2 s delay each time. After 10 failures, a 15-minute lock.
- **Start-up refusal:** if public, the app refuses to start without the hash and secret (A3).
- **Laptop default:** no password, behaviour unchanged.
- **Optional later:** GitHub OAuth (free) with an allow-list of one GitHub login, matching your earlier wish for a free OAuth login. Designed, not built in this pass.

**Latency and size on A1** (UNSURE, my estimates; measure with the evals):
- 3B Q4: roughly 5–8 tokens/s generation and roughly 30–60 tokens/s prompt reading. A classification batch takes about 15–30 s; a chart attempt about 60–90 s; a Full CRAG + Self-RAG answer 1–3 minutes.
- Mitigations: deterministic pre-routers, a "fast" grade (one batched grader call instead of the Send fan-out, `RECTIFI_RAG_FAST=1` on A1), and caching of embeddings.
- 7B models fit in RAM but are about 2–3 tokens/s, so they are not recommended there.
- **Cold start:** the VM is always on. A model reload after an idle restart takes about 5–15 s.

**What breaks or changes when hosted:**
- The phone SMS gateway stays off; it would need its own public port.
- `TailscaleLock` doesn't apply to Funnel; the password does.
- The Streamlit fallback and the MCP server (stdio) are not exposed.
- Uploads still have the 10 MB limit.
- Oracle reclaim and capacity risk.
- A single VM is a single point of failure: rely on the backups and 5 free volume backups.

**Testing here (no Docker daemon):**
- `tests/test_deploy_files.py`:
  - parses the Dockerfile with `dockerfile-parse` (dev-only, not in requirements, skipped if missing). Asserts: a non-root `USER`, a `HEALTHCHECK`, `--workers 1`, no `ENV` holding secrets, no `ADD http`.
  - checks that `.dockerignore` lists `.env`.
  - checks that compose `ports` all start with `127.0.0.1:`, that db and ollama have no ports, and `read_only: true` on app.
  - runs `docker compose -f deploy/compose.oracle.yml config --quiet` (that works without a daemon, as already done for the existing file; skipped if docker is missing).
  - runs `hadolint` if available (`pip install hadolint-py`, dev-only).
  - checks that `cloud-init.yaml` is valid YAML.
- Not possible here: `docker build` and a real run. Say so in PROGRESS.

---

## F. Execution plan: parallel workstreams with strict file ownership

### F0. Rules for every workstream agent

- Work in your own git worktree and branch (`ws/<name>`). Run `pg_ctlcluster 16 main start` first.
- **Use a separate test database per worktree** (`TEST_DATABASE_URL=…/rectifi_test_<ws>`; the name must end in `_test`). The integrator creates them, because tests drop all tables.
- **Never edit a file you don't own.** If you need something in a shared file, put it in your handback as a "request for integrator" with the exact diff.
- **Never run `build-web` or commit `web/dist`.** Only the integrator does. In your branch, `test_web_dist` will fail; that is expected. Run `cd web && npx tsc -b && npx vitest run` instead.
- **Never edit CLAUDE.md, PROGRESS.md or README.md.** Report in your handback: what you built, the tests added, the real test count, and what you couldn't verify.
- **Append points** in shared Python files (created by F1): marked blocks such as `# ═══ WS-CLASSIFY: begin ═══ … end`. Edit only inside your own block.
- **Tests:** no live Ollama. Use `tests/fake_ollama.py`, `tests/support/fakes.py` (ScriptedModel, ScriptedStructured, BowEmbeddings). Live tests go under the `@pytest.mark.ollama` marker (skipped unless `-m ollama`).
- Keep the existing 822 tests green, except the "Other" assertion updates done in F1.
- Before handing back, run `python -m pytest -q -x -m "not ollama"` plus your own new files.

### F1. FOUNDATION (sequential, one agent; everyone waits for it)

**Goal:** the shared schema, migrations, interfaces, fakes, pins, the rename, and append points. Nothing user-visible beyond the rename.

**Owns** (and afterwards these become integrator-owned shared files):
- `rectifi/schema.sql` (all of A2)
- `rectifi/db.py` (categories, rules, `_migrate_categories`)
- `requirements.txt`: add `langchain-text-splitters==1.1.3`, `matplotlib==3.11.2`, `seaborn==0.13.2`, `numpy==2.5.3`, `ollama==0.6.3`. Plotly comes later, only if the stretch is done. Install them and check `langchain-core` doesn't move.
- `rectifi/config.py`: role settings, `bind_host()`, `public_mode()`, `web_search()`, `langsmith_optin()`, `auto_classify()`, chart and classify tunables.
- `rectifi/categorize.py`: **constants and the `merchant_key`/`Decision` interface only**; WS-CLASSIFY fills in the logic.
- `rectifi/agent/contracts.py`: interfaces, see below.
- `rectifi/agent/checkpoint.py`: pooled saver + store, implemented.
- `rectifi/agent/llm.py`: `get_llm(role)` / `get_embeddings()` signatures implemented with options and timeouts; `probe`/`explain_error v2` bodies are left to WS-LLM inside an append block.
- `rectifi/agent/tracing.py`: LocalTraceHandler implemented (small) and `callbacks_for(run_name, **meta)`.
- `rectifi/api/app.py`: the routes list includes new modules `classify, charts, chat, memory, traces, models, auth, knowledge`; F1 creates each `routes/<x>.py` with `routes = []`; each WS fills its own.
- `rectifi/api/security.py`: `UPLOAD_PATHS = {"/api/imports", "/api/knowledge/docs"}`, plus an append block for WS-HOST.
- `tools.py`: the rename via `FALLBACK_*`, and append blocks `WS-CLASSIFY`, `WS-CHARTS`, `WS-CHAT`, `WS-RAG`.
- `wire.py`: append blocks.
- The rename in `insights.py`, `wire.py`, `streamlit_app.py`, `seed_demo.py` and `web/src/pages/Review.tsx`, plus the test updates in A2.
- `web/src/App.tsx` and `nav.ts`: routes for `/charts /library /memory /traces /login` pointing at page files F1 creates as minimal placeholders, which the WSs overwrite.
- `scripts/dev.py`: command registry entries delegating to `scripts/devtools/models.py` (WS-LLM) and `scripts/devtools/deploy.py` (WS-HOST), loaded by path. F1 creates both with stub functions that print "not built yet".
- `tests/conftest.py`: `pytest_collection_modifyitems` skips `ollama` unless `-m ollama` is given; the marker is registered.
- `tests/fake_ollama.py`: ThreadingHTTPServer. Endpoints `/api/version`, `/api/tags`, `/api/show` (configurable capabilities), `/api/chat` (scripted queue; tool_calls; `format` → JSON content; stream true NDJSON or false), `/api/embed` (Bow vectors), `/api/ps`. Error injection: 404 model, 400 "does not support tools", delays. Fixture `fake_ollama(monkeypatch)` sets `OLLAMA_BASE_URL`.
- `tests/support/fakes.py`: move `ScriptedModel` here and re-export it from `test_agent.py`; add `ScriptedStructured` (overrides `with_structured_output` to pop scripted objects validated by the schema) and `BowEmbeddings`.

**contracts.py** (what every workstream codes against):
```python
class RagInput(TypedDict): question: str; mode: str; history: list; uid: str
class RagOutput(TypedDict): answer: str; citations: list[dict]; trace: list[dict]; unverified: list[str]; support: str|None; useful: bool|None
def build_rag_subgraph(*, llms: dict, embeddings, checkpointer=None, store=None) -> "CompiledStateGraph"   # WS-RAG implements
class ChartInput(TypedDict): request: str; period: str; review: bool
class ChartOutput(TypedDict): chart_id: int|None; ok: bool; trace: list[dict]; error: str|None
def build_chart_subgraph(*, llm, checkpointer=None) -> "CompiledStateGraph"                                # WS-CHARTS implements
def classify_pending(batch_id: int|None, *, llm=None, progress=None) -> dict                               # WS-CLASSIFY implements
TraceEvent = TypedDict("TraceEvent", {"step": str, "verdict": str, "detail": str})                         # shared UI trace shape
```

**Acceptance:**
- All existing tests green, with only the "Other" assertion strings updated.
- `test_categories_migration.py`:
  - an old database (Other with rows, a budget and a rule) keeps its id, now named Miscellaneous;
  - Fuel exists and petrol/fuel default rules point to it;
  - learned rules are marked;
  - running init twice is a no-op.
- `test_schema_additive.py`: init on a database built with the pre-change schema keeps every row.
- `test_fake_ollama.py`: ChatOllama, OllamaEmbeddings and `with_structured_output` work against the fake server.
- `test_requirements` passes.
- `test_import_boundaries.py`: categorize, textvec, charts.validate, charts.sandbox and importer don't load langchain; `rectifi.api` imports without langchain.

**Brief:**
- Read: CLAUDE.md, db.py, schema.sql, tools.py lines 1–340, insights.py around "Other", tests/test_agent.py lines 1–35, conftest.py, api/app.py, nav.ts, App.tsx, and this plan's A, F0 and F1.
- Do NOT read web pages other than Review.tsx, or docs/WEB_PLAN.md.

### F2. Parallel wave (all start after F1 merges)

**WS-LLM: model diagnostics and Ollama setup.** Small, so it can merge first to unblock you.
- **Goal:** B5 diagnostics, the RAM tier, Modelfiles, `ModelCheck` UI.
- **Owns:**
  - `agent/llm.py` WS-LLM block: `probe`, `capabilities`, `explain_error v2`, `redact`.
  - `scripts/devtools/models.py`: doctor LLM round-trip, RAM tier, `ollama-setup`, `classify`/`eval` passthroughs.
  - `deploy/Modelfile.classify`, `deploy/Modelfile.chartcoder`.
  - `api/routes/models.py`.
  - `web/src/components/ModelCheck.tsx` (+ `.module.css`), `web/src/api/models.ts`.
  - Tests: `test_llm_probe.py`, `test_llm_roles.py`, `test_devtools_models.py`.
  - `.env.example` additions: send them to the integrator as text.
- **Relies on:** F1 config roles and fake_ollama.
- **Acceptance:**
  - probe reports each stage against the fake server: model missing (404), no tools (400), tool call returned as text, timeout, OK;
  - `explain_error` maps 400 "API key not valid"/ACCESS_TOKEN_TYPE_UNSUPPORTED to the AQ-key advice, and the key never appears;
  - the doctor round-trip works against the fake server with a stdlib client;
  - the RAM tier table is used;
  - Modelfiles parse (FROM/SYSTEM/PARAMETER lines).
- **Brief:** read agent/llm.py, config.py, scripts/dev.py lines 600–760, tests/test_llm.py (fake OpenAI server pattern), and plan B5. Don't read rag/charts/web pages.

**WS-CLASSIFY: classification, background job, import UI.**
- **Goal:** B1 and D1.
- **Owns:**
  - `rectifi/categorize.py` (logic), `rectifi/textvec.py`, `rectifi/importer.py`.
  - tools.py WS-CLASSIFY block, plus `_categorize`/`set_category`/`add_transaction` category bookkeeping (these existing functions are transferred to WS-CLASSIFY for this wave).
  - wire.py WS-CLASSIFY block (txn fields).
  - `agent/classify.py`, `agent/jobs.py`, `api/routes/classify.py`.
  - The `api/routes/imports.py` result now includes `classify_job_id` (transferred to this WS).
  - `web/src/components/add/ImportCategories.tsx` (+ css), `components/SourceStamp.tsx`, `web/src/api/classify.ts`, `components/add/ImportStatement.tsx` (mount point only).
  - Ledger source column: `components/ledger/LedgerTable.tsx` (small).
  - `evals/classify_cases.json`, `rectifi/evals/classify.py`.
  - Tests: `test_categorize.py`, `test_classify_llm.py`, `test_api_classify.py`, `test_classify_live.py` (marked ollama).
- **Relies on:** F1 schema and the `contracts.classify_pending` signature.
- **Acceptance:**
  - Every heuristic has a positive and a negative case ("parent" ≠ Rent).
  - Retrieval picks the trusted neighbour and ignores llm/fallback labels.
  - Import marks sources and remains instant with the model off.
  - The job applies only to pending rows and **never changes amount, direction, date, account, upi_ref or fingerprint** (snapshot compare).
  - A user correction during a job wins (race test).
  - Enum blocks wrong-kind categories.
  - Suggestions band works.
  - `apply-similar` skips user rows.
  - Undo cancels the job and cascades embeddings.
  - Masking: the model never sees a ≥9-digit run.
  - Restart requeue is idempotent.
  - The eval runner scores a scripted run.
  - vitest for SourceStamp words.
- **Brief:** read importer.py, tools.py lines 100–340, parsing.narration_details, privacy.py, routes/imports.py, ImportStatement.tsx, LedgerTable.tsx (columns only), plan B1/D1. Don't read the agent graph, insights or phone code.

**WS-CHARTS: chart harness.**
- **Goal:** B2 and D2 (plotly iframe = stretch).
- **Owns:**
  - `rectifi/charts/*` (including `_winjob.py`, fonts, theme).
  - tools.py WS-CHARTS block: `chart_frame`, `chart_reference_totals`, the charts table CRUD.
  - `api/routes/charts.py`.
  - `web/src/pages/Charts.tsx`, `components/charts/*`, `web/src/api/charts.ts`.
  - `evals/chart_cases.json`, `rectifi/evals/charts.py`.
  - Tests: `test_chart_validate.py`, `test_chart_sandbox.py`, `test_chart_checks.py`, `test_chart_graph.py`, `test_api_charts.py`, `test_charts_live.py` (marked ollama).
  - Plotly stretch: a request to the integrator for security.py's frame CSP and the `plotly==7.1.0` pin.
- **Relies on:** F1 checkpoint, tracing, `get_llm("code")`, and the contracts.
- **Acceptance:**
  - Validator: about 40 table-driven cases (forbidden import, dunder, `open`, `to_csv`, `getattr`, a ₹ string, `5000`, `figsize=(10,6)` allowed, the year 2026 allowed, a number the user typed allowed).
  - Sandbox on Linux: timeout kill; memory-limit kill; a socket attempt is blocked; writing outside job_dir is blocked; reading `.env` is blocked; a subprocess is blocked; a second `exec` is blocked; the environment has no DATABASE_URL; output PNG validated; malicious result.json is rejected.
  - Windows job object test skipped off Windows.
  - Checks: a title with a figure not in the data fails; a computed sum passes; a truncated rupee passes; a missing title fails.
  - Graph: the repair loop is bounded at 3; interrupt and resume (review gate); time travel from attempt 2; RetryPolicy.
  - Re-running a pinned chart makes no LLM call.
  - API streams progress events.
  - Chart PNGs render ₹ without missing-glyph warnings.
- **Brief:** read tools.py lines 378–460 (summary/daily SQL), money.py, api/routes/ask.py and api/ask.py (the streaming pattern), security.py (CSP), docs/WEB_PLAN.md B9, plan B2/D2. Don't read importer, insights or phone code.

**WS-RAG: CRAG + Self-RAG.**
- **Goal:** B3 and D5, plus the knowledge subgraph used by the chat.
- **Owns:**
  - `rectifi/rag/*`, `rectifi/knowledge/*.md`.
  - tools.py WS-RAG block: note/txn document rows for loaders.
  - `api/routes/knowledge.py` (docs, upload, reindex, status).
  - `web/src/pages/Library.tsx`, `web/src/components/rag/{Citations,ReflectionTrace,WebConsent}.tsx`, `web/src/api/knowledge.ts`.
  - `evals/rag_questions.json`, `rectifi/evals/rag.py`.
  - Tests: `test_rag_loaders.py`, `test_rag_store.py`, `test_rag_retrieve.py`, `test_rag_graders.py`, `test_rag_graph.py`, `test_api_knowledge.py`, `test_rag_live.py` (marked ollama).
  - `agent/graph.py` gains `tools=` and `READ_ONLY_TOOLS`, plus the `RectifiReadOnlyToolkit` (transferred to this WS for this wave; WS-CHAT must not edit graph.py).
- **Relies on:** the F1 contract `build_rag_subgraph`, checkpoint, embeddings, `get_llm("grade"|"chat")`, and `agent.graph.unverified_amounts`.
- **Acceptance:**
  - Guides contain no ₹.
  - The store matches InMemoryVectorStore on top-k for the same embeddings.
  - MMR diversity.
  - Hybrid RRF.
  - Incremental reindex skips unchanged hashes.
  - txn chunks cascade on undo.
  - Mode matrix (rag/crag/self_rag/full) visits the expected nodes.
  - Each CRAG branch.
  - Self-RAG: an unsupported answer leads to regeneration, bounded at 2; not useful leads to rewrite and re-retrieve, bounded at 2.
  - The ₹ check fails an invented figure before the LLM grader runs.
  - The tools route uses ToolNode with read-only tools only (a write tool is impossible).
  - Web consent: interrupt → resume(false) → no search call.
  - Statement-looking uploads are refused.
  - The eval runner computes metrics on a scripted run.
- **Brief:** read agent/graph.py, agent/tools.py, privacy.py, money.amounts_in_text, parsing.AMOUNT, plan B3/D3 (knowledge parts)/D5. Don't read importer, charts or web pages other than Ask.tsx.

**WS-CHAT: supervisor, memory, conversations, observability UI.**
- **Goal:** B4 and D3/D4/D6.
- **Owns:**
  - `agent/supervisor.py`, `agent/memory.py`.
  - tools.py WS-CHAT block: conversations CRUD and trace queries.
  - `api/ask.py` (generalised streaming worker), `api/routes/{ask,chat,memory,traces}.py`.
  - `web/src/pages/{Ask.tsx,Memory.tsx,Traces.tsx}`, `ask.module.css`, `web/src/components/chat/*`, `web/src/api/{chat,memory,traces}.ts`, `web/src/api/ndjson.ts`.
  - Tests: `test_supervisor.py`, `test_memory.py`, `test_chat_threads.py`, `test_chat_api.py`, `test_tracing.py`, `test_persistence.py`, `test_web_e2e_chat.py`.
- **Relies on:** the contracts for the RAG and chart subgraphs. Until integration, use **fake subgraphs** built from contract TypedDicts; integration swaps in the real ones. It also uses the existing `build_graph`.
- **Acceptance:**
  - Router: deterministic and LLM routes, plus the explicit chips.
  - The parallel context branches join.
  - Subgraph state mapping.
  - Trimming fits the context; the summary node keeps the full checkpoint.
  - Store CRUD per namespace; memory ₹ figures are redacted in prompts and never accepted by `unverified_amounts`.
  - Threads: list, search, rename, delete (checkpoints gone).
  - Resume shows the full history with timestamps.
  - Stop cancels between steps.
  - Regenerate forks (the old branch is in history).
  - Token streaming events.
  - `/api/ask/*` aliases keep test_api_ask green.
  - Local spans are written and masked; LangSmith is forced off without opt-in.
  - Playwright: new chat → stream → reload → resume → rename → delete.
- **Brief:** read api/ask.py, routes/ask.py, agent/graph.py (read only), web/src/pages/Ask.tsx, ask.module.css, api/ndjson.ts, docs/WEB_PLAN.md B5 (the Ask section), plan B4/D3/D4/D6. Don't read importer, charts or the RAG internals.

**WS-HOST: auth and deployment.**
- **Goal:** E (auth, Docker, docs) and D7.
- **Owns:**
  - `rectifi/api/auth.py`, the security.py WS-HOST block (SessionLock, startup refusal), `api/routes/auth.py`.
  - `web/src/pages/Login.tsx`, `web/src/api/auth.ts`, plus a 401 handler: a request to the integrator for a one-line hook in `web/src/api/client.ts`.
  - `deploy/*` (except the Modelfiles), `.dockerignore`, `.env.deploy.example`, `docs/HOSTING.md`, `scripts/devtools/deploy.py` (`set-password`, `deploy-check`).
  - Tests: `test_auth.py`, `test_deploy_files.py`.
- **Relies on:** F1 config (`bind_host`, `public_mode`).
- **Acceptance:**
  - scrypt round trip; a wrong password gives 401 plus the delay and the lock.
  - The cookie is tampered → 401; the epoch bump logs everyone out.
  - Public mode without a secret refuses to start.
  - With the password set, every `/api` route except health and auth returns 401 without a session; static files are still served.
  - Existing guards are unchanged: `test_api_security` stays green.
  - Funnel-like request (Host `*.ts.net`, no Tailscale header) plus a session → allowed when the TailscaleLock is unset.
  - Deploy file tests as in E.
- **Brief:** read api/security.py, api/app.py, tests/test_api_security.py, config.py, docker-compose.yml, plan E. Don't read agent, rag, charts or importer.

### F3. INTEGRATION (sequential, one integrator agent)

1. Merge in this order: WS-LLM → WS-CLASSIFY → WS-HOST → WS-CHARTS → WS-RAG → WS-CHAT. Resolve append blocks and apply the "requests for integrator" diffs.
2. Swap WS-CHAT's fake subgraphs for the real `build_rag_subgraph` and `build_chart_subgraph`. Run `test_supervisor` against the real ones (scripted models).
3. Write the `.env.example` lines, CLAUDE.md (A3 rules, Map, Commands), README sections (models, charts, library, hosting, honest limits), `docs/CONCEPTS.md`, and `nav.ts` final ordering.
4. Run `python scripts/dev.py build-web` once and commit `web/dist`. Run the full `pytest -q -m "not ollama"` and `cd web && npm test`.
5. Take screenshots (1280/390, light/dark) and do the self-critique pass (at least 5 fixes, listed).
6. Update PROGRESS.md with the true test count and the "Not verified" items: Windows Job Object, real Ollama, a real Oracle VM, Funnel, real bank statements, LangSmith, plotly CSP.

### F4. REVIEW → FIX loop (iterative, conditional)

- A **reviewer** agent (Opus) reads the integrated diff per area against a checklist:
  - CLAUDE rules 1–15;
  - masking at every model boundary;
  - no money maths in the browser;
  - Decimal in wire;
  - import boundaries;
  - sandbox escape attempts (it writes 5 new adversarial validator/sandbox cases);
  - unbounded loops;
  - SQL injection (all parameters bound);
  - XSS (no `dangerouslySetInnerHTML` except the existing markdown renderer);
  - accessibility (labels, focus, 44 px targets, words on stamps);
  - the design system.
- Its output is findings tagged by workstream and severity.
- **Conditional re-run:** only workstreams with HIGH or MED findings get a fixer agent, using the same brief plus the findings. The fixer works on that WS's files only.
- Re-review only the changed areas. At most 3 rounds; then the integrator lists what is left in PROGRESS "Not done".

### F5. Live checks on your laptop (you, after the build)

- `python scripts/dev.py doctor`, then `ollama-setup`.
- `pytest -m ollama`.
- `python -m rectifi.evals classify --live`, then `charts --live`, then `rag --live`.
- Put the numbers into the README tables and pick the thresholds.

**Priority if time runs short:**
1. WS-LLM (it unblocks you)
2. Foundation + WS-CLASSIFY
3. WS-CHARTS core (no plotly)
4. WS-RAG core (no web search, no PDF uploads)
5. WS-CHAT (threads, streaming, memory, traces)
6. WS-HOST

**Stretch:** plotly iframe, web search + consent, PDF knowledge uploads, receipt vision, the LangSmith live check, GitHub OAuth.

---

## G. Risks, and what I couldn't confirm

**Risks:**
1. **Scope.** This is about 6 workstreams of new code. The parallel plan depends on F1's contracts being right. If a contract has to change mid-wave, stop and re-issue it from the integrator.
2. **Small-model quality.** 3B graders and classifiers will be noisy. Self-RAG reflection with prompted graders is an approximation; the evals are the truth. Expect "Full" mode to be slow on CPU.
3. **The sandbox isn't a security boundary.** It is layered defence for a single-user app (B2.4). The Windows Job Object code can't be tested here.
4. **Audit-hook false positives.** matplotlib, seaborn or plotly may call `compile`, or open files outside the allow-list, lazily. Pre-import everything; tests will show it; the `compile` block is switchable.
5. **Re-labelling Fuel and Miscellaneous** changes what Insights and Review say on existing data. That is fine, and tested.
6. **Memory pressure** on 8 GB laptops when the classifier, coder and embedder are all loaded. Set `OLLAMA_MAX_LOADED_MODELS=2` and use one bg worker.
7. **Public hosting of bank data.** Prefer Tailscale Serve (private) even on Oracle. Use Funnel plus the password only if you truly need a public link.

**UNSURE or "I don't know":**
- Whether the CampusX Ollama Masterclass is the 21st (final) video of the LangChain playlist. It is CampusX's and dated Jan 2026; membership not confirmed. Some per-video sub-topics are marked (U).
- Whether `gemini-3.8-flash` is a real model id. I don't know.
- The exact HTTP code your AQ. key produced (400 vs 401).
- qwen2.5-coder writing tool calls as text: an anecdotal belief. The probe measures it.
- Ollama's default `num_ctx` in your installed version. It is set explicitly anyway.
- Whether nomic-embed-text needs `search_query:`/`search_document:` prefixes. The flag and the eval decide.
- A1 token speeds and latencies are my estimates.
- How Oracle's idle-reclaim rule plays out with a model kept in memory.
- Indian card acceptance at Oracle sign-up.
- Tailscale Funnel on the free plan (not confirmed from the plans page).
- LangSmith's free-tier limits.
- Supabase's 7-day pause: widely reported, not checked first-hand here.
- plotly.js CSP fix (PR #7109): unmerged when read, hence the iframe design.
- Windows wheels for matplotlib 3.11.2 and numpy 2.5.3 on Python 3.13: likely, not checked.
- Whether `ddgs` still works. It is optional and unpinned.

**Worth saving to memory** (main session decides):
- Rectifi hosting decision: Oracle A1 + Docker Compose + Tailscale Serve/Funnel is primary; the laptop + Tailscale is the fallback. HF Docker Spaces now need PRO.
- Rectifi uses Postgres-table vectors (no pgvector) and n-gram retrieval for categories.
- "Other" is renamed to "Miscellaneous".
- Recommended Ollama models per role (B5).
- Rule-8 amendment pending your decision.

### Critical files for implementation
- /home/claude/rectifi/rectifi/schema.sql and /home/claude/rectifi/rectifi/db.py (additive schema, category migration ordering)
- /home/claude/rectifi/rectifi/tools.py (`_categorize`, `set_category`, append blocks, `chart_frame` / `chart_reference_totals`)
- /home/claude/rectifi/rectifi/importer.py (per-row category bookkeeping; must stay free of langchain)
- /home/claude/rectifi/rectifi/agent/llm.py (roles, timeouts, probe, `explain_error` v2) and /home/claude/rectifi/rectifi/agent/graph.py (`build_graph(tools=…)`, `unverified_amounts` reuse)
- /home/claude/rectifi/rectifi/api/ask.py and /home/claude/rectifi/rectifi/api/security.py (streaming worker generalisation; SessionLock, upload paths, CSP)

### Sources
- [Generative AI using LangChain playlist](https://www.youtube.com/playlist?list=PLKnIA16_RmvaTbihpo4MtzVm4XOQa0ER0) · [CampusX LangChain course page](https://learnwith.campusx.in/s/preview/package/699c9c709ee1006450f17073/courses/LangChain-YouTube) · [CampusX LangGraph course page](https://learnwith.campusx.in/courses/LangGraph-YouTube-69145df5c26d79058b698748)
- [CampusX Ollama Masterclass 2026](https://www.youtube.com/watch?v=YcAYmIFtA0o) · [Structured Output video](https://www.youtube.com/watch?v=y5EmRr1O1h4) · [AI Agent video](https://www.youtube.com/watch?v=gm_lQG8fYjI)
- [HF Spaces overview](https://huggingface.co/docs/hub/spaces-overview) · [HF pricing](https://huggingface.co/pricing)
- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm) · [InfoQ on the 2026 Oracle cut](https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)
- [Neon free plan limits](https://neon.com/faqs/free-plan-limits-and-quotas) · [Render free](https://render.com/docs/free) · [Free hosting comparison 2026](https://agentdeals.dev/hosting-free-tier-comparison-2026) · [Tailscale plans](https://tailscale.com/plans/)
- [Ollama qwen2.5-coder](https://ollama.com/library/qwen2.5-coder) · [qwen3](https://ollama.com/library/qwen3) · [llama3.2](https://ollama.com/library/llama3.2) · [nomic-embed-text](https://ollama.com/library/nomic-embed-text) · [Ollama API docs](https://github.com/ollama/ollama/blob/main/docs/api.md)
- [Gemini forum: AQ. keys and REST](https://discuss.ai.google.dev/t/new-api-keys-generated-with-aq-prefix-dont-work-with-rest-endpoint/176177)
- [plotly.js CSP PR #7109](https://github.com/plotly/plotly.js/pull/7109) · [Langfuse self-hosting](https://langfuse.com/self-hosting)
