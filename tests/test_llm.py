import time

import httpx
import pytest

from rectifi.agent import llm


def test_deepseek_without_key_is_not_ok(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    ok, why = llm.llm_status()
    assert not ok and "DEEPSEEK_API_KEY" in why


def test_deepseek_with_key_is_ok(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert llm.llm_status() == (True, "")


def test_ollama_unreachable_is_quick_and_clear(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    started = time.monotonic()
    ok, why = llm.llm_status()
    assert time.monotonic() - started < 3
    assert not ok and "127.0.0.1:9" in why and "ollama" in why.lower()


def test_unknown_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gpt-banana")
    ok, why = llm.llm_status()
    assert not ok and "gpt-banana" in why


class _Reply:
    def __init__(self, names):
        self._names = names

    def raise_for_status(self):
        pass

    def json(self):
        return {"models": [{"name": n} for n in self._names]}


@pytest.mark.parametrize("wanted, installed, expected_ok", [
    ("qwen2.5:7b", ["qwen2.5:7b"], True),
    ("qwen2.5:7b", ["qwen2.5:14b"], False),   # a different size is a different model
    ("llama3", ["llama3:latest"], True),      # no tag asked for: any tag will do
    ("qwen2.5:7b", [], False),
])
def test_ollama_model_must_be_pulled(monkeypatch, wanted, installed, expected_ok):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", wanted)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Reply(installed))
    ok, why = llm.llm_status()
    assert ok is expected_ok
    if not ok:
        assert f"ollama pull {wanted}" in why


def test_deepseek_thinking_disabled(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    model = llm.get_llm()
    assert model.extra_body == {"thinking": {"type": "disabled"}}
    assert model.model_name == "deepseek-flash"
    assert model.temperature == 0


def test_ollama_uses_configured_base_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example.invalid:1234")
    assert llm.get_llm().base_url == "http://example.invalid:1234"


# ───────────────────────── OpenAI-compatible providers ─────────────────────────

ALL_KEYS = ["LLM_PROVIDER", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "HF_TOKEN", "HF_MODEL",
            "OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL", "DEEPSEEK_API_KEY"]


@pytest.fixture
def clean_env(monkeypatch):
    for name in ALL_KEYS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _secret(model) -> str:
    return model.openai_api_key.get_secret_value()


def test_openrouter_needs_key_then_model(clean_env):
    clean_env.setenv("LLM_PROVIDER", "openrouter")
    ok, why = llm.llm_status()
    assert not ok and "OPENROUTER_API_KEY" in why and "openrouter.ai/keys" in why
    clean_env.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ok, why = llm.llm_status()
    assert not ok and "OPENROUTER_MODEL" in why and "dev.py free-models" in why
    clean_env.setenv("OPENROUTER_MODEL", "vendor/model:free")
    assert llm.llm_status() == (True, "")


def test_openrouter_model_has_no_guessed_default(clean_env):
    from rectifi import config

    clean_env.setenv("LLM_PROVIDER", "openrouter")
    assert config.openai_style_settings("openrouter")["model"] == ""
    assert "no model set" in llm.describe()


def test_openrouter_construction(clean_env):
    clean_env.setenv("LLM_PROVIDER", "openrouter")
    clean_env.setenv("OPENROUTER_API_KEY", "sk-or-test")
    clean_env.setenv("OPENROUTER_MODEL", "vendor/model:free")
    model = llm.get_llm()
    assert model.openai_api_base == "https://openrouter.ai/api/v1"
    assert model.model_name == "vendor/model:free"
    assert model.temperature == 0
    assert model.request_timeout == 60 and model.max_retries == 2
    assert _secret(model) == "sk-or-test"
    assert llm.describe() == "openrouter · vendor/model:free"


def test_huggingface_needs_token_but_not_a_model(clean_env):
    clean_env.setenv("LLM_PROVIDER", "huggingface")
    ok, why = llm.llm_status()
    assert not ok and "HF_TOKEN" in why and "Inference Providers" in why
    clean_env.setenv("HF_TOKEN", "hf_test")
    assert llm.llm_status() == (True, "")      # credits are a warning (llm_notice), not a blocker


def test_huggingface_says_plainly_it_is_not_free(clean_env):
    clean_env.setenv("LLM_PROVIDER", "huggingface")
    notice = llm.llm_notice()
    assert "not free" in notice.lower() and "$0" in notice and "PRO" in notice
    ok, why = llm.llm_status()
    assert "$0" in why                          # the missing-token message says it too


def test_huggingface_construction_default_model(clean_env):
    clean_env.setenv("LLM_PROVIDER", "huggingface")
    clean_env.setenv("HF_TOKEN", "hf_test")
    model = llm.get_llm()
    assert model.openai_api_base == "https://router.huggingface.co/v1"
    assert model.model_name == "deepseek-ai/DeepSeek-V3.1:cheapest"
    assert model.temperature == 0 and model.request_timeout == 60 and model.max_retries == 2
    clean_env.setenv("HF_MODEL", "deepseek-ai/DeepSeek-V3.1:fastest")
    assert llm.get_llm().model_name == "deepseek-ai/DeepSeek-V3.1:fastest"


def test_openai_compat_reports_each_missing_setting(clean_env):
    clean_env.setenv("LLM_PROVIDER", "openai_compat")
    ok, why = llm.llm_status()
    assert not ok and "OPENAI_COMPAT_BASE_URL" in why
    clean_env.setenv("OPENAI_COMPAT_BASE_URL", "https://api.groq.com/openai/v1/")
    ok, why = llm.llm_status()
    assert not ok and "OPENAI_COMPAT_API_KEY" in why
    clean_env.setenv("OPENAI_COMPAT_API_KEY", "gsk-test")
    ok, why = llm.llm_status()
    assert not ok and "OPENAI_COMPAT_MODEL" in why
    clean_env.setenv("OPENAI_COMPAT_MODEL", "llama-3.3-70b-versatile")
    assert llm.llm_status() == (True, "")


def test_openai_compat_construction(clean_env):
    clean_env.setenv("LLM_PROVIDER", "openai_compat")
    clean_env.setenv("OPENAI_COMPAT_BASE_URL", "https://api.groq.com/openai/v1/")
    clean_env.setenv("OPENAI_COMPAT_API_KEY", "gsk-test")
    clean_env.setenv("OPENAI_COMPAT_MODEL", "llama-3.3-70b-versatile")
    model = llm.get_llm()
    assert model.openai_api_base == "https://api.groq.com/openai/v1"       # trailing slash removed
    assert model.model_name == "llama-3.3-70b-versatile"
    assert model.temperature == 0 and model.request_timeout == 60 and model.max_retries == 2


@pytest.mark.parametrize("provider, env", [
    ("openrouter", {"OPENROUTER_API_KEY": "sk-or-SECRET-1", "OPENROUTER_MODEL": "v/m:free"}),
    ("huggingface", {"HF_TOKEN": "hf_SECRET-2"}),
    ("openai_compat", {"OPENAI_COMPAT_BASE_URL": "https://x.invalid/v1", "OPENAI_COMPAT_API_KEY": "gsk-SECRET-3",
                       "OPENAI_COMPAT_MODEL": "m"}),
])
def test_status_and_caption_never_contain_keys(clean_env, provider, env):
    clean_env.setenv("LLM_PROVIDER", provider)
    for name, value in env.items():
        clean_env.setenv(name, value)
    shown = " ".join([llm.describe(), llm.llm_notice(), llm.llm_status()[1], repr(llm.get_llm())])
    assert "SECRET" not in shown
    clean_env.delenv(list(env)[0])
    assert "SECRET" not in llm.llm_status()[1]


def test_unknown_provider_lists_the_choices(clean_env):
    clean_env.setenv("LLM_PROVIDER", "gpt-banana")
    ok, why = llm.llm_status()
    assert not ok and all(p in why for p in ("ollama", "deepseek", "openrouter", "huggingface", "openai_compat"))
    with pytest.raises(ValueError):
        llm.get_llm()


class _Failure(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_explain_error_gives_plain_advice(clean_env):
    clean_env.setenv("LLM_PROVIDER", "huggingface")
    assert "out of credits" in llm.explain_error(_Failure(402)) and "$0" in llm.explain_error(_Failure(402))
    clean_env.setenv("LLM_PROVIDER", "openrouter")
    assert "50 a day" in llm.explain_error(_Failure(429))
    assert "free-models" in llm.explain_error(_Failure(404))
    assert "API key" in llm.explain_error(_Failure(401))
    assert llm.explain_error(ValueError("boom")) == "boom"


def test_the_agent_graph_accepts_the_openai_style_model(clean_env):
    from rectifi.agent.graph import build_graph

    clean_env.setenv("LLM_PROVIDER", "openai_compat")
    clean_env.setenv("OPENAI_COMPAT_BASE_URL", "https://x.invalid/v1")
    clean_env.setenv("OPENAI_COMPAT_API_KEY", "k")
    clean_env.setenv("OPENAI_COMPAT_MODEL", "m")
    assert build_graph() is not None            # bind_tools works on the real ChatOpenAI


# ───── a real HTTP round trip: get_llm() -> ChatOpenAI -> a fake server -> the agent, guard and database ─────

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _FakeOpenAI:
    """Speaks just enough of /v1/chat/completions. Replies come from `replies`, one per request."""

    def __init__(self, replies):
        self.requests, self.headers, outer = [], [], self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                reply = replies.pop(0)
                message = {"role": "assistant", "content": reply} if isinstance(reply, str) else {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "call_1", "type": "function",
                                    "function": {"name": reply[0], "arguments": json.dumps(reply[1])}}]}
                payload = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                                      "choices": [{"index": 0, "message": message,
                                                   "finish_reason": "stop" if isinstance(reply, str) else "tool_calls"}],
                                      "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_openai(clean_env):
    servers = []

    def start(replies, provider="openai_compat"):
        server = _FakeOpenAI(replies)
        servers.append(server)
        clean_env.setenv("NO_PROXY", "127.0.0.1")
        clean_env.setenv("no_proxy", "127.0.0.1")
        clean_env.setenv("LLM_PROVIDER", provider)
        clean_env.setenv("OPENAI_COMPAT_BASE_URL", server.url)
        clean_env.setenv("OPENAI_COMPAT_API_KEY", "gsk-test")
        clean_env.setenv("OPENAI_COMPAT_MODEL", "tool-model")
        return server

    yield start
    for server in servers:
        server.close()


def test_openai_compat_end_to_end_logs_an_expense(fake_openai):
    from rectifi import tools
    from rectifi.agent.graph import build_graph, chat

    server = fake_openai([("log_transaction", {"amount": "250", "direction": "debit", "payee": "Swiggy"}),
                          "Saved ₹250 at Swiggy."])
    new = chat(build_graph(), "spent 250 on swiggy", "e2e-1")
    assert new[-1].content == "Saved ₹250 at Swiggy."
    assert [t["amount"] for t in tools.list_transactions()] == [tools.parse_amount("250")]
    path, body = server.requests[0]
    assert path == "/v1/chat/completions"
    assert body["model"] == "tool-model" and body["temperature"] == 0
    assert any(t["function"]["name"] == "log_transaction" for t in body["tools"])      # tool calling is really used
    assert server.headers[0]["authorization"] == "Bearer gsk-test"


def test_guard_and_masking_still_apply_over_openai_compat(fake_openai):
    from rectifi import tools
    from rectifi.agent.graph import build_graph, chat

    server = fake_openai([("log_transaction", {"amount": "2500", "direction": "debit", "payee": "Swiggy"}),
                          "Sorry, how much exactly?"])
    new = chat(build_graph(), "spent 250 on swiggy, card 4111111111111111", "e2e-2")
    assert any(getattr(m, "content", "").startswith("GUARD") for m in new)             # invented 2500 refused
    assert tools.list_transactions() == []
    sent = json.dumps(server.requests[0][1])
    assert "4111111111111111" not in sent and "1111" in sent                              # masked, last 4 kept
