# F1 foundation handoff (read this instead of re-deriving)

Commit 5b617fe on master. 886 passed, 5 skipped (4 boundary checks for modules not yet created), 1 deselected (live ollama).

## Append-block markers
`# ═══ WS-<NAME>: begin ═══` … `# ═══ WS-<NAME>: end ═══` — edit ONLY inside your own block.
- rectifi/tools.py: WS-CLASSIFY, WS-CHARTS, WS-CHAT, WS-RAG (file end)
- rectifi/api/wire.py: WS-CLASSIFY, WS-CHARTS, WS-CHAT, WS-RAG (file end)
- rectifi/api/security.py: WS-HOST
- rectifi/agent/llm.py: WS-LLM (holds `probe(role="chat", timeout=20) -> dict` stub)
- rectifi/categorize.py: WS-CLASSIFY (`decide` stub)

## categorize.py (pure, no rectifi imports)
FALLBACK_EXPENSE="Miscellaneous", FALLBACK_INCOME="Other income", FALLBACK_NAMES, LEGACY_FALLBACK_EXPENSE="Other",
SOURCES=("user","learned","rule","heuristic","retrieval","llm","fallback"), fallback_name(direction),
`@dataclass(frozen=True) Decision(category, source, confidence:int, reason, suggested=None, suggested_confidence=None)`,
merchant_key(payee, narration=None), decide(conn, payee, narration, direction) -> Decision (stub, WS-CLASSIFY).

## agent/contracts.py (callers import from here; owners must define these names)
TraceEvent{step,verdict,detail}; RagInput{question,mode,history,uid}; RagOutput{answer,citations,trace,unverified,support,useful};
ChartInput{request,period,review}; ChartOutput{chart_id,ok,trace,error}.
- `rectifi.rag.graph.build_rag_subgraph(*, llms, embeddings, checkpointer=None, store=None)`
- `rectifi.charts.graph.build_chart_subgraph(*, llm, checkpointer=None)`
- `rectifi.agent.classify.classify_pending(batch_id, *, llm=None, progress=None) -> {"total","done","by_source","status","model","error"}`
Missing owner → NotImplementedError("… not built yet").

## agent/checkpoint.py
get_memory() -> Memory(saver, store, pool) (PostgresSaver + PostgresStore, setup() run; per-process singleton; DatabaseUnavailable if PG down); checkpointer(), store(), close_memory() (conftest fresh_db calls it).

## agent/tracing.py
LocalTraceHandler(thread_id=None) → trace_spans rows (masked, 2048-char previews, swallow DB errors); callbacks_for(run_name, **meta) -> {"callbacks","run_name","tags","metadata"} (merge with {"configurable":{"thread_id":…}}); prune(); preview().

## agent/llm.py
get_llm(role="chat"), get_embeddings(). Ollama: num_ctx, keep_alive, client_kwargs timeout, reasoning=False for qwen3*. explain_error/llm_status/describe untouched (WS-LLM updates them to role vars).

## config
ROLES=("chat","classify","code","grade"); env RECTIFI_MODEL_CHAT/_CLASSIFY/_CODE/_GRADE, RECTIFI_EMBED_MODEL (nomic-embed-text). Lookup: role var → RECTIFI_MODEL_CHAT → OLLAMA_MODEL (default qwen2.5:7b). explicit_role_model, role_model, embed_model. OLLAMA_NUM_CTX 8192, OLLAMA_TIMEOUT 120, OLLAMA_KEEP_ALIVE 30m. bind_host(), public_mode(), web_search(), langsmith_optin(), auto_classify(), enforce_langsmith_privacy().
Tunables (monkeypatch config.X): CLASSIFY_APPLY_MIN=80, CLASSIFY_SUGGEST_MIN=50, CLASSIFY_BATCH_SIZE=12, CHART_MAX_ATTEMPTS=3, CHART_TIMEOUT=30, CHART_MEMORY_MB=512, RAG_TOP_K=4, RAG_CHUNK_CHARS=900, RAG_CHUNK_OVERLAP=120, RAG_MAX_REGENERATE=2, RAG_MAX_RETRIEVE=2, TRACE_RETENTION_DAYS=14, TRACE_MAX_ROWS=20000, TRACE_PREVIEW_CHARS=2048.

## Tests
- `fake_ollama` fixture: real HTTP server; .reply(text), .reply_tool_call(name,args), .reply_json(obj), .fail_next(status,msg), .delay, .add_model(name,caps)/.remove_model, .loaded, .queue, .calls/.paths()/.bodies(path), .base_url. Default models qwen2.5:7b, qwen2.5:3b, qwen2.5-coder:7b (tools), nomic-embed-text:latest (embedding). 404 unknown model; 400 "does not support tools". with_structured_output works (reply_json).
- `from support.fakes import ScriptedModel, ScriptedStructured, BowEmbeddings, bow_vector`. ScriptedStructured(structured=[...]) pops/validates per with_structured_output call.
- tests/support/olddb.py rebuild_old_database() for migration tests.
- Live tests: @pytest.mark.ollama (skipped unless -m ollama).
- test_import_boundaries: pure modules (categorize, textvec, charts.validate/sandbox/_child, importer, insights, pdf_statement, sms_gateway, tools, db) and rectifi.api/app must not load langchain/ollama/rectifi.agent/rectifi.rag/rectifi.charts.graph.

## API / web / dev
- Empty `routes/{classify,charts,chat,memory,traces,models,auth,knowledge}.py` (routes = []) already mounted.
- security.UPLOAD_PATHS = {"/api/imports","/api/knowledge/docs"}.
- Placeholder pages web/src/pages/{Charts,Library,Memory,Traces,Login}.tsx (overwrite whole file); routes in App.tsx; nav.ts already lists them.
- dev.py loads scripts/devtools/{models,deploy}.py: `COMMANDS = {name: (fn(args), help)}`; stubs print "not built yet".

## Gotchas
- confidence/percent fields are ints (wire.to_wire stringifies every Decimal as money).
- tools.add_transaction returns {"status","transaction":{…}}.
- WS-CLASSIFY: when rewriting set_category(remember=True), set source='learned' on the rule upsert (migration steps 3/4 are once-only via app_settings 'categories_v2').
- Full suite ≈5 min. Don't run build-web or commit web/dist on ws branches (test_web_dist fails there — expected).
