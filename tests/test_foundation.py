"""The shared plumbing the workstreams build on: config roles and tunables, get_llm(role), contracts, the pooled chat memory,
local traces, the fakes, the dev.py registry and the upload paths."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ValidationError

from rectifi import config, db
from rectifi.agent import llm
from support.fakes import BowEmbeddings, ScriptedModel, ScriptedStructured, bow_vector

ROOT = Path(__file__).resolve().parent.parent


# ───────────── config ─────────────

@pytest.fixture
def clean_roles(monkeypatch):
    for name in (*config.ROLE_ENV.values(), "OLLAMA_MODEL", "RECTIFI_EMBED_MODEL", "OLLAMA_NUM_CTX", "OLLAMA_TIMEOUT",
                 "OLLAMA_KEEP_ALIVE", "RECTIFI_BIND_HOST", "RECTIFI_PUBLIC", "RECTIFI_WEB_SEARCH", "RECTIFI_LANGSMITH",
                 "RECTIFI_AUTO_CLASSIFY"):
        monkeypatch.delenv(name, raising=False)


def test_every_role_falls_back_to_the_chat_model_then_ollama_model(clean_roles, monkeypatch):
    assert config.ROLES == ("chat", "classify", "code", "grade")
    assert {r: config.role_model(r) for r in config.ROLES} == {r: "qwen2.5:7b" for r in config.ROLES}
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    assert config.role_model("grade") == "llama3.2:3b"
    monkeypatch.setenv("RECTIFI_MODEL_CHAT", "qwen3:8b")
    assert config.role_model("code") == "qwen3:8b" and config.role_model("chat") == "qwen3:8b"
    monkeypatch.setenv("RECTIFI_MODEL_CODE", "qwen2.5-coder:7b")
    monkeypatch.setenv("RECTIFI_MODEL_CLASSIFY", "qwen2.5:3b")
    assert config.role_model("code") == "qwen2.5-coder:7b" and config.role_model("classify") == "qwen2.5:3b"
    with pytest.raises(ValueError):
        config.role_model("poet")


def test_explicit_role_model_is_empty_when_nothing_is_named(clean_roles):
    assert all(config.explicit_role_model(r) == "" for r in config.ROLES)


def test_embed_model_and_ollama_options(clean_roles, monkeypatch):
    assert config.embed_model() == "nomic-embed-text"
    assert (config.ollama_num_ctx(), config.ollama_timeout(), config.ollama_keep_alive()) == (8192, 120, "30m")
    assert config.ollama_keep_alive_seconds() == 1800
    monkeypatch.setenv("OLLAMA_NUM_CTX", "4096")
    monkeypatch.setenv("OLLAMA_TIMEOUT", "not a number")
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "2h")
    assert config.ollama_num_ctx() == 4096 and config.ollama_timeout() == 120 and config.ollama_keep_alive_seconds() == 7200


def test_hosting_switches_default_to_this_laptop_only(clean_roles, monkeypatch):
    assert config.bind_host() == "127.0.0.1" and config.public_mode() is False
    assert config.web_search() is False and config.langsmith_optin() is False and config.auto_classify() is True
    monkeypatch.setenv("RECTIFI_BIND_HOST", "0.0.0.0")
    assert config.bind_host() == "0.0.0.0" and config.public_mode() is True
    monkeypatch.setenv("RECTIFI_BIND_HOST", "not-an-ip")
    assert config.bind_host() == "127.0.0.1" and config.public_mode() is False
    monkeypatch.setenv("RECTIFI_PUBLIC", "1")
    assert config.public_mode() is True
    monkeypatch.setenv("RECTIFI_WEB_SEARCH", "yes")
    monkeypatch.setenv("RECTIFI_LANGSMITH", "1")
    monkeypatch.setenv("RECTIFI_AUTO_CLASSIFY", "0")
    assert config.web_search() and config.langsmith_optin() and not config.auto_classify()


def test_tunables_are_sane_numbers():
    assert 0 <= config.CLASSIFY_SUGGEST_MIN <= config.CLASSIFY_APPLY_MIN <= 100
    assert config.CLASSIFY_BATCH_SIZE >= 1 and config.CHART_MAX_ATTEMPTS == 3 and config.CHART_TIMEOUT >= 5
    assert config.RAG_TOP_K >= 1 and config.TRACE_PREVIEW_CHARS == 2048


def test_langsmith_is_forced_off_unless_opted_in(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("RECTIFI_LANGSMITH", raising=False)
    assert config.enforce_langsmith_privacy() is True
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert config.enforce_langsmith_privacy() is False            # nothing left to switch off
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("RECTIFI_LANGSMITH", "1")
    assert config.enforce_langsmith_privacy() is False and os.environ["LANGSMITH_TRACING"] == "true"


# ───────────── get_llm(role) ─────────────

def test_get_llm_uses_the_role_model_and_ollama_options(clean_roles, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "4096")
    monkeypatch.setenv("RECTIFI_MODEL_CLASSIFY", "qwen2.5:3b")
    chat = llm.get_llm()
    assert chat.model == "qwen2.5:7b" and chat.num_ctx == 4096 and chat.keep_alive == "30m"
    assert chat.client_kwargs == {"timeout": 120}
    assert llm.get_llm("classify").model == "qwen2.5:3b"
    assert llm.get_llm("code").model == "qwen2.5:7b"             # no code model named: the chat model
    with pytest.raises(ValueError):
        llm.get_llm("poet")


def test_qwen3_gets_reasoning_off_and_others_keep_the_default(clean_roles, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("RECTIFI_MODEL_CHAT", "qwen3:8b")
    assert llm.get_llm("chat").reasoning is False
    monkeypatch.setenv("RECTIFI_MODEL_CHAT", "llama3.2:3b")
    assert llm.get_llm("chat").reasoning is None


def test_a_role_names_a_model_for_the_other_providers_too(clean_roles, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/free-model:free")
    assert llm.get_llm("chat").model_name == "vendor/free-model:free"
    monkeypatch.setenv("RECTIFI_MODEL_GRADE", "vendor/small:free")
    assert llm.get_llm("grade").model_name == "vendor/small:free"
    assert llm.get_llm("classify").model_name == "vendor/free-model:free"


# ───────────── contracts ─────────────

def test_contracts_shapes_and_lazy_owners():
    from rectifi.agent import contracts

    assert set(contracts.RagOutput.__annotations__) == {"answer", "citations", "trace", "unverified", "support", "useful"}
    assert set(contracts.RagInput.__annotations__) == {"question", "mode", "history", "uid"}
    assert set(contracts.ChartInput.__annotations__) == {"request", "period", "review"}
    assert set(contracts.ChartOutput.__annotations__) == {"chart_id", "ok", "trace", "error"}
    assert contracts.TraceEvent(step="grade", verdict="relevant", detail="2 of 4 chunks")["verdict"] == "relevant"
    import inspect

    assert list(inspect.signature(contracts.build_rag_subgraph).parameters) == ["llms", "embeddings", "checkpointer", "store"]
    assert list(inspect.signature(contracts.build_chart_subgraph).parameters) == ["llm", "checkpointer"]
    assert list(inspect.signature(contracts.classify_pending).parameters) == ["batch_id", "llm", "progress"]


def test_contract_owners_that_do_not_exist_yet_say_so():
    from rectifi.agent import contracts

    # Once a workstream lands its module, this stops raising and its own tests take over; that is expected.
    for call in (lambda: contracts.build_rag_subgraph(llms={}, embeddings=None),
                 lambda: contracts.build_chart_subgraph(llm=None), lambda: contracts.classify_pending(None)):
        try:
            call()
        except NotImplementedError as exc:
            assert "not built yet" in str(exc)
        except Exception:      # noqa: BLE001 - the owner exists now and rejected the dummy arguments: fine
            pass


def test_decision_and_fallback_names():
    from rectifi import categorize

    assert (categorize.FALLBACK_EXPENSE, categorize.FALLBACK_INCOME) == ("Miscellaneous", "Other income")
    assert categorize.fallback_name("debit") == "Miscellaneous" and categorize.fallback_name("credit") == "Other income"
    d = categorize.Decision("Fuel", "rule", 95, "rule 'petrol'")
    assert d.suggested is None and d.source in categorize.SOURCES
    assert categorize.merchant_key("SWIGGY*ORDER 8812@icici") == categorize.merchant_key("swiggy order")


# ───────────── chat memory (pooled saver + store) ─────────────

def test_memory_pool_serves_saver_and_store_and_survives_threads():
    from rectifi.agent import checkpoint

    mem = checkpoint.get_memory()
    assert checkpoint.get_memory() is mem                          # one per process
    ns = ("users", "owner", "memories")
    mem.store.put(ns, "m1", {"kind": "preference", "text": "I like short answers"})
    assert mem.store.get(ns, "m1").value["text"] == "I like short answers"
    from langgraph.graph import END, START, MessagesState, StateGraph

    builder = StateGraph(MessagesState)
    builder.add_node("echo", lambda state: {"messages": [AIMessage("pong")]})
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    graph = builder.compile(checkpointer=mem.saver)

    import threading

    errors = []

    def run(i):
        try:
            graph.invoke({"messages": [HumanMessage("ping")]}, {"configurable": {"thread_id": f"t{i}"}})
        except Exception as exc:    # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    for i in range(4):
        assert len(graph.get_state({"configurable": {"thread_id": f"t{i}"}}).values["messages"]) == 2


def test_memory_rebuilds_after_close_and_reports_a_missing_database(monkeypatch):
    from rectifi.agent import checkpoint

    first = checkpoint.get_memory()
    checkpoint.close_memory()
    second = checkpoint.get_memory()
    assert second is not first
    checkpoint.close_memory()
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:x@127.0.0.1:9/nothing")
    with pytest.raises(db.DatabaseUnavailable):
        checkpoint.get_memory()


# ───────────── local traces ─────────────

def spans():
    with db.connect() as conn:
        return conn.execute("SELECT * FROM trace_spans ORDER BY started_at, id").fetchall()


def test_local_trace_handler_records_a_run_with_masked_previews():
    from langgraph.graph import END, START, MessagesState, StateGraph

    from rectifi.agent.tracing import callbacks_for

    model = ScriptedModel(script=[AIMessage("Paid from account 123456789012 done")])

    def node(state):
        return {"messages": [model.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("answer", node)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    graph = builder.compile()
    cfg = callbacks_for("test-run", thread_id="thread-7", workstream="chat", role="chat")
    assert cfg["run_name"] == "test-run" and cfg["tags"] == ["chat", "chat"] and cfg["metadata"]["thread_id"] == "thread-7"
    graph.invoke({"messages": [HumanMessage("my card is 4111 1111 1111 1111")]}, cfg)
    rows = spans()
    assert rows and {r["root_run_id"] for r in rows} == {next(r["root_run_id"] for r in rows if r["parent_run_id"] is None)}
    assert {"chain", "llm"} <= {r["run_type"] for r in rows}
    assert all(r["thread_id"] == "thread-7" and r["status"] == "ok" for r in rows)
    text = " ".join((r["input_preview"] or "") + (r["output_preview"] or "") for r in rows)
    assert "4111 1111 1111 1111" not in text and "123456789012" not in text           # masked before storing
    assert all(len(r["input_preview"] or "") <= 2048 and len(r["output_preview"] or "") <= 2048 for r in rows)


def test_a_failing_step_is_recorded_and_a_database_problem_never_breaks_the_run(monkeypatch):
    from langchain_core.runnables import RunnableLambda

    from rectifi.agent import tracing

    def boom(_):
        raise RuntimeError("model fell over")

    with pytest.raises(RuntimeError):
        RunnableLambda(boom).invoke("x", tracing.callbacks_for("failing"))
    bad = [r for r in spans() if r["status"] == "error"]
    assert bad and "model fell over" in bad[0]["error"]

    def broken_connect():
        raise RuntimeError("database gone")

    monkeypatch.setattr(tracing, "connect", broken_connect)
    assert RunnableLambda(lambda x: x + 1).invoke(1, tracing.callbacks_for("still-works")) == 2


def test_prune_removes_old_spans_and_caps_the_table():
    from rectifi.agent.tracing import prune

    run = uuid.uuid4()
    with db.connect() as conn:
        for age, name in ((30, "old"), (1, "new-a"), (0, "new-b")):
            conn.execute(
                """INSERT INTO trace_spans (run_id, root_run_id, name, run_type, started_at)
                   VALUES (%s, %s, %s, 'chain', now() - make_interval(days => %s))""", (uuid.uuid4(), run, name, age))
    assert prune(retention_days=14, max_rows=100) == 1
    assert [r["name"] for r in spans()] == ["new-a", "new-b"]
    assert prune(retention_days=14, max_rows=1) == 1
    assert [r["name"] for r in spans()] == ["new-b"]


# ───────────── fakes ─────────────

class Verdict(BaseModel):
    relevant: bool


def test_scripted_structured_pops_objects_and_checks_the_schema():
    model = ScriptedStructured(structured=[Verdict(relevant=True), {"relevant": False}, {"relevant": "maybe not a bool"}, RuntimeError("down")])
    grader = model.with_structured_output(Verdict)
    assert grader.invoke("is this relevant?") == Verdict(relevant=True)
    assert grader.invoke([HumanMessage("again")]) == Verdict(relevant=False)
    with pytest.raises(ValidationError):
        grader.invoke("third")
    with pytest.raises(RuntimeError):
        grader.invoke("fourth")
    with pytest.raises(AssertionError):
        grader.invoke("fifth")                                      # ran out
    assert model.schemas == [Verdict] * 5 and any("again" in str(m.content) for m in model.seen)


def test_scripted_structured_is_still_a_scripted_model():
    model = ScriptedStructured(script=[AIMessage("hello")])
    assert model.invoke("hi").content == "hello"


def test_scripted_model_is_importable_from_test_agent_as_before():
    import test_agent

    assert test_agent.ScriptedModel is ScriptedModel


def test_bow_embeddings_put_similar_texts_close():
    emb = BowEmbeddings()
    a, b, c = emb.embed_documents(["monthly rent for the flat", "rent flat monthly payment", "swiggy dinner order"])
    dot = lambda x, y: sum(p * q for p, q in zip(x, y))  # noqa: E731
    assert dot(a, b) > 0.5 > dot(a, c)
    assert emb.embed_query("flat rent") != [0.0] * 64 and bow_vector("") == [0.0] * 64
    assert emb.embed_documents(["same"]) == emb.embed_documents(["same"])


# ───────────── dev.py registry and upload paths ─────────────

def run_dev(*args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "dev.py"), *args], capture_output=True, text=True, timeout=60)


def test_evals_dispatch_and_dev_eval_passes_options_through():
    bad = subprocess.run([sys.executable, "-m", "rectifi.evals", "nope"], capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert bad.returncode == 2 and "Usage" in bad.stdout
    rag = run_dev("eval", "rag")                      # no --live: says so and exits 2 (no database, no model needed)
    assert rag.returncode == 2 and "Nothing to run" in rag.stdout


def test_dev_help_lists_the_new_commands():
    out = run_dev("--help").stdout
    for command in ("ollama-setup", "classify", "eval", "set-password", "deploy-check"):
        assert command in out


def test_upload_paths_get_the_big_body_limit_and_json_stays_small():
    from rectifi.api import security

    assert security.UPLOAD_PATHS == {"/api/imports", "/api/knowledge/docs"}
    limit = security.BodyLimit.limit_for
    assert limit("/api/imports") == limit("/api/knowledge/docs") == security.UPLOAD_LIMIT + security.UPLOAD_OVERHEAD
    assert limit("/api/chat/threads") == security.JSON_LIMIT


def test_append_blocks_exist_in_the_shared_files():
    expectations = {
        "rectifi/tools.py": ["CLASSIFY", "CHARTS", "CHAT", "RAG"],
        "rectifi/api/wire.py": ["CLASSIFY", "CHARTS", "CHAT", "RAG"],
        "rectifi/api/security.py": ["HOST"],
        "rectifi/agent/llm.py": ["LLM"],
        "rectifi/categorize.py": ["CLASSIFY"],
    }
    for path, names in expectations.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        for name in names:
            assert f"# ═══ WS-{name}: begin ═══" in text and f"# ═══ WS-{name}: end ═══" in text, (path, name)
            assert text.index(f"WS-{name}: begin") < text.index(f"WS-{name}: end")
