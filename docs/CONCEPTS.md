# Concepts: where each LangChain and LangGraph idea lives in Rectifi

This is a map for learning, not a claim that every idea is used. For each concept: the file where Rectifi uses it, and a test that proves
it. `tests/test_concepts_doc.py` checks that every test named here exists. A dash means the idea is **not used**, and the row says why.

Sources: the CampusX LangChain playlist (the last video is the Ollama masterclass) and the CampusX LangGraph course. **MCP is left out
on purpose** (you asked for that). The playlists were read from their public chapter lists, not watched; where a sub-topic is a guess it
says so. The read-only MCP server that already existed (`rectifi/mcp_server.py`) is a separate feature and is not part of this map.

Test names are written `file::test`. All of them run with fake models: no Ollama, no network.

## Overview (LangChain 1 to 4: roadmap, intro, components)

LangChain gives you small parts (models, prompts, parsers, tools, retrievers) and one way to join them (`a | b | c`, called LCEL).
LangGraph adds a graph on top: **state** that nodes read and write, **edges** between nodes, loops, pauses and saved progress.
Rectifi uses LangChain for the parts and LangGraph for anything with more than one step, a loop, or a pause.

| Graph | File | Shape |
|---|---|---|
| Supervisor (the chat) | `rectifi/agent/supervisor.py` | load context (two branches in parallel) then route then one worker then check amounts |
| Spending agent (a subgraph of the supervisor) | `rectifi/agent/graph.py` | model node, guarded tool node, loop until no more tool calls |
| Knowledge answers (CRAG + Self-RAG) | `rectifi/rag/graph.py` | route, retrieve, grade in parallel, correct, generate, check support, check use, loops bounded |
| Chart harness | `rectifi/charts/graph.py` | write, validate, (review), run in the sandbox, check, repair loop bounded at 3 |

## LangChain playlist

| Concept | Where in Rectifi | Test that proves it |
|---|---|---|
| Chat models (open-source models through Ollama) | `rectifi/agent/llm.py` `get_llm(role)` makes a `ChatOllama` per job (chat, classify, code, grade) | `test_llm_roles::test_ollama_get_llm_per_role` |
| Embedding models and similarity | `rectifi/rag/embeddings.py` (`OllamaEmbeddings` with the nomic prefixes), cosine in `rectifi/rag/store.py` | `test_rag_store::test_cosine_topk_orders_by_similarity`, `test_rag_loaders::test_ollama_embeddings_via_fake_server` |
| PromptTemplate, ChatPromptTemplate, MessagesPlaceholder | classifier `agent/classify.py`, chart `charts/prompts.py`, graders and answer chain in `rag/` (history goes in a `MessagesPlaceholder`) | `test_langchain_pieces::test_the_classifier_prompt_is_system_then_few_shot_then_rows`, `test_rag_graph::test_generate_chain_masks_history_and_question` |
| Few-shot prompts | `FewShotChatMessagePromptTemplate` in `agent/classify.py` (examples only for categories that exist) | `test_langchain_pieces::test_the_classifier_prompt_is_system_then_few_shot_then_rows` |
| Saving and loading prompts (`load_prompt`) | — not used. Prompts are Python strings next to the code that fills them. | — |
| Structured output with Pydantic | `agent/classify.py` `label_schema` (the category list is an enum), `DocGrade` in `rag/graders.py` | `test_classify_llm::test_the_enum_blocks_a_wrong_kind_category`, `test_rag_graders::test_docgrade_is_a_pydantic_model_and_normalises` |
| Structured output with TypedDict + Annotated | `GroundCheck` in `rag/graders.py` | `test_rag_graders::test_groundcheck_typeddict` |
| Structured output with a JSON schema | `UseCheck` in `rag/graders.py` | `test_rag_graders::test_usecheck_json_schema` |
| StrOutputParser | the answer chain in `rag/graph.py`, code writer in `charts/graph.py` | `test_rag_graph::test_generate_chain_masks_history_and_question`, `test_chart_graph::test_happy_path_runs_in_order_and_saves_the_chart` |
| PydanticOutputParser (with format instructions) | the fallback in `agent/classify.py` for a model with no structured output | `test_langchain_pieces::test_a_model_without_structured_output_is_asked_for_json_and_parsed_with_a_pydantic_parser` |
| JsonOutputParser | the multi-query fallback in `rag/retrieve.py` | `test_rag_retrieve::test_multiquery_json_parser_fallback` |
| Sequential chain (prompt, model, parser) | every `prompt \| model \| parser` above | `test_rag_graph::test_generate_chain_masks_history_and_question` |
| Parallel chain (`RunnableParallel`) | one retrieval per section and per rewritten question, merged | `test_rag_retrieve::test_parallel_retrieval_union_over_sections_and_queries`, `test_langchain_pieces::test_runnable_parallel_runs_branches_on_the_same_input` |
| Conditional chain (`RunnableBranch`) | — not used. Branching is done in LangGraph (conditional edges) and in plain Python (a credit gets an income-only category list). | `test_classify_llm::test_credits_get_an_income_only_enum` (the plain Python branch) |
| RunnableLambda, RunnableSequence, RunnablePassthrough | `RunnableLambda` and `\|` are used (a masking step first, the multi-query chain). `RunnablePassthrough` and an explicit `RunnableSequence` are not. | `test_langchain_pieces::test_lcel_pipes_compose_with_a_masking_lambda_first`, `test_rag_retrieve::test_multiquery_runs_prompt_structured_llm_and_keeps_the_original_first` |
| LCEL | all of the above | same tests |
| Document loaders (a custom `BaseLoader`, `lazy_load`) | `rag/loaders.py`: guides, your notes, your entries, uploaded files | `test_rag_loaders::test_guide_loader_is_lazy_and_labels_documents`, `test_rag_loaders::test_transaction_loader_one_masked_document_per_row` |
| Text splitters | `MarkdownHeaderTextSplitter` then `RecursiveCharacterTextSplitter` in `rag/loaders.py` | `test_rag_loaders::test_split_sizes_and_structure` |
| Semantic chunker | — not used. It lives in `langchain_experimental`, which is not installed. | — |
| Vector stores | `PgVectorStore(VectorStore)` in `rag/store.py`: vectors in Postgres, cosine in Python (no pgvector extension) | `test_rag_store::test_store_matches_in_memory_vector_store_on_top_k` |
| Retrievers: `as_retriever`, MMR | `rag/retrieve.py` | `test_rag_retrieve::test_mmr_diversity_through_as_retriever` |
| Multi-query retriever | an LCEL version in `rag/retrieve.py` (no `langchain-classic`) | `test_rag_retrieve::test_multiquery_runs_prompt_structured_llm_and_keeps_the_original_first` |
| Contextual compression | the strip filter that CRAG's *refine* step uses | `test_rag_retrieve::test_strip_compressor_keeps_close_sentences_and_always_one`, `test_rag_graph::test_refine_drops_irrelevant_strips` |
| A custom retriever (`BaseRetriever`) | the hybrid retriever (vectors + keywords, joined with reciprocal rank fusion) | `test_rag_retrieve::test_hybrid_rrf_keyword_pass_rescues_what_vectors_cannot_tell_apart` |
| RAG | the knowledge graph in plain mode | `test_rag_graph::test_modes_matrix` |
| Tools: `@tool`, `StructuredTool`, toolkit | the `@tool` functions in `agent/tools.py`; `RectifiReadOnlyToolkit` (`BaseToolkit`) in `agent/graph.py`; `StructuredTool` for `remember_fact` in `agent/memory.py` | `test_rag_toolkit::test_toolkit_holds_exactly_the_read_only_tools`, `test_memory::test_the_remember_tool_hides_its_injected_arguments_from_the_model` |
| `InjectedToolArg` | the user id and the store are injected into `remember_fact`; the model never sees them | `test_memory::test_the_remember_tool_hides_its_injected_arguments_from_the_model` |
| Tool calling (`bind_tools`, `ToolMessage`) | the spending agent, and the check in `agent/llm.py` that asks a model to call a tool | `test_agent::test_logs_expense_end_to_end`, `test_llm_probe::test_ok_reports_every_stage_and_the_tool_round_trip` |
| Agents (ReAct) | the spending agent is a ReAct loop in LangGraph. `AgentExecutor` is not used (it is the old way). | `test_agent::test_logs_expense_end_to_end` |
| Ollama: model library and hardware sizing | the RAM tier table in `scripts/devtools/models.py` (`dev.py ollama-setup`, `llm-check`) | `test_devtools_models::test_the_tier_table_is_the_plan` |
| Ollama: the command line (pull, list, create) | `ollama-setup` runs these as argument lists, never through a shell | `test_devtools_models::test_ollama_setup_pulls_then_creates_with_list_arguments` |
| Ollama: the REST API (`/api/tags`, `/api/show`, `/api/chat`) | `agent/llm.py` `probe` and `capabilities`, and the doctor round trip | `test_llm_probe::test_ok_reports_every_stage_and_the_tool_round_trip`, `test_devtools_models::test_the_round_trip_against_the_fake_ollama` |
| Ollama: the Python library | `ollama` is installed (it comes with `langchain-ollama`) and pinned, but Rectifi talks to the REST API with `httpx` for the checks. Not called directly. | — |
| Ollama: model parameters (`num_ctx`, `temperature`, `keep_alive`, `num_predict`) | `get_llm` options, set from `OLLAMA_NUM_CTX`, `OLLAMA_KEEP_ALIVE` | `test_llm_probe::test_the_requests_carry_the_context_window_and_keep_alive` |
| Ollama: conversation history | the chat keeps messages in the checkpoint and resumes them | `test_chat_threads::test_resume_shows_the_full_history_with_times_and_survives_a_new_graph` |
| Ollama: tool calling | the agent on `ChatOllama`; the check flags a model that writes the call as text | `test_llm_probe::test_a_tool_call_written_as_text_is_flagged` |
| Ollama: Modelfiles | `deploy/Modelfile.classify`, `deploy/Modelfile.chartcoder`, created by `dev.py ollama-setup` | `test_devtools_models::test_the_modelfiles_parse` |
| Ollama: images and vision | — not built. | — |
| Ollama Cloud, the desktop app | — not used. Cloud would send your data off the laptop. | — |

## LangGraph course (MCP is excluded)

| # | Concept | Where in Rectifi | Test that proves it |
|---|---|---|---|
| 1 to 4 | Agentic vs generative, LangChain vs LangGraph | the overview above | — |
| 5 | State, reducers, nodes, edges, START and END, compile | every graph; `trace` and `grades` use custom reducers in `rag/graph.py` | `test_rag_graph::test_trace_reducer_accumulates_and_a_second_turn_starts_fresh` |
| 6 | Sequential workflow | chart: prepare, write, validate, run, check | `test_chart_graph::test_happy_path_runs_in_order_and_saves_the_chart` |
| 7 | Parallel workflow (fan-out and fan-in with `Send`) | one grader task per passage; the supervisor loads memory and trims history side by side | `test_rag_graph::test_grades_fan_out`, `test_supervisor::test_parallel_context_branches_both_run_before_the_route` |
| 8 | Conditional workflow | the supervisor's router; CRAG's decision | `test_supervisor::test_fixed_rules_route`, `test_rag_graph::test_crag_branches` |
| 9 | Iterative workflow (loops with a bound) | chart repair (3 tries); Self-RAG rewrites and re-retrieves (bounded by settings) | `test_chart_graph::test_the_repair_loop_is_bounded_at_three_attempts`, `test_rag_graph::test_regen_bounded_and_strict_prompt_lists_the_claims` |
| 10 | Chatbot | the supervisor | `test_chat_api::test_a_turn_streams_nodes_trace_steps_then_the_whole_conversation` |
| 11 | Persistence: checkpointer, `thread_id`, `get_state`, history, time travel, `update_state`, fault tolerance | pooled `PostgresSaver` in `agent/checkpoint.py`; *Answer again* forks a thread; a chart can be redrawn from attempt N | `test_persistence::test_time_travel_forks_and_keeps_the_old_branch`, `test_persistence::test_resume_after_a_node_crash_runs_only_the_failed_step`, `test_persistence::test_update_state_edits_a_thread_as_if_a_node_wrote_it`, `test_chart_graph::test_time_travel_forks_from_just_before_attempt_two` |
| 12 | Chatbot with a UI | the React chat window; the old Streamlit Ask page remains | `test_web_e2e_chat::test_chat_new_stream_reload_resume_search_rename_delete` |
| 13 | Streaming: updates, message tokens, custom events | NDJSON tokens from the `generate` and `agent` nodes; chart progress through `get_stream_writer` | `test_chat_api::test_tokens_stream_before_the_done_line`, `test_chart_graph::test_progress_is_streamed_with_the_custom_writer` |
| 14 | Resume a chat (thread list) | the conversations sidebar | `test_chat_threads::test_new_search_rename_delete_through_the_api` |
| 15 | Chatbot with a database | Postgres instead of SQLite | `test_persistence::test_state_survives_a_brand_new_graph_and_pool` |
| 16, 17 | LangSmith and observability | optional LangSmith (off unless `RECTIFI_LANGSMITH=1`); your own trace table and the Traces page | `test_tracing::test_local_spans_are_written_for_every_kind_of_step`, `test_tracing::test_langsmith_is_forced_off_without_opt_in` |
| 18 | Tools in LangGraph: `ToolNode`, `tools_condition` | the read-only tools route inside the knowledge graph | `test_rag_graph::test_tools_route_uses_toolnode_and_read_only_tools` |
| 19 | MCP client | — **excluded by your request** | — |
| 20 | RAG with LangGraph | the knowledge graph | `test_rag_graph::test_modes_matrix` |
| 21 | Human in the loop: `interrupt`, `Command(resume=...)` | read the chart code before it runs; allow one web search; confirm before remembering | `test_chart_graph::test_the_review_gate_pauses_and_runs_the_approved_code`, `test_rag_graph::test_web_consent_interrupt_resume_false_means_no_search`, `test_supervisor::test_remember_asks_first_then_saves_on_yes`, `test_chat_real_subgraphs::test_the_real_chart_review_pause_uses_the_review_code_kind_and_resumes` |
| 22 | Subgraphs | the spending agent, knowledge and chart graphs run as subgraphs of the supervisor | `test_chat_real_subgraphs::test_the_real_knowledge_subgraph_answers_with_sources_and_checks`, `test_supervisor::test_knowledge_state_mapping_and_answer_style` |
| 23, 24 | Short-term memory: trim, summarise | `agent/memory.py` | `test_memory::test_trim_fits_the_budget_and_starts_on_a_user_message`, `test_memory::test_long_chats_are_summarised_without_amounts_and_the_window_shrinks` |
| 25 | Long-term memory: the Store and namespaces | `PostgresStore` under `("users", uid, "memories")`, and the Memory page | `test_memory::test_store_crud_and_namespaces_are_separate`, `test_memory::test_memory_never_supplies_rupees` |
| 26 | Orchestrator-worker (the blog agent) | the pattern is used for grading (fan out, join). The blog project itself is not copied. | `test_rag_graph::test_grades_fan_out` |
| extra | CRAG and Self-RAG | `rag/graph.py` (read the library section of the README for the honest limits) | `test_rag_graph::test_modes_matrix`, `test_rag_eval::test_scripted_run_computes_every_metric_for_every_mode` |
| extra | `RetryPolicy` | model nodes retry a dropped connection; nodes that write do not | `test_persistence::test_the_model_node_that_is_safe_to_repeat_has_a_retry_policy_and_the_others_do_not` |

## What is deliberately not here

`load_prompt`, `RunnableBranch`, `RunnablePassthrough`, a semantic chunker, `AgentExecutor`, calling the `ollama` Python package
directly, image input, Ollama Cloud, and MCP. Each is listed above with the reason. Adding any of them just to tick a box would make
the code worse.
