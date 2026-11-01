"""llm.probe, llm.redact, llm.capabilities and explain_error v2, against the fake Ollama server and a fake OpenAI-style server."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from rectifi.agent import llm

KEYS = ["LLM_PROVIDER", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "HF_TOKEN", "HF_MODEL", "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL", "DEEPSEEK_API_KEY", "RECTIFI_MODEL_CHAT", "RECTIFI_MODEL_CLASSIFY",
        "RECTIFI_MODEL_CODE", "RECTIFI_MODEL_GRADE", "OLLAMA_MODEL", "OLLAMA_NUM_CTX", "OLLAMA_TIMEOUT"]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)
    llm.clear_probe_cache()
    yield
    llm.clear_probe_cache()


def stage_names(result):
    return [s["name"] for s in result["stages"]]


# ───────────── Ollama ─────────────

def test_ok_reports_every_stage_and_the_tool_round_trip(fake_ollama):
    fake_ollama.reply("OK")
    fake_ollama.reply_tool_call("ping", {"x": 1})
    r = llm.probe()
    assert r["ok"] is True and r["stage"] == "ok" and r["tools_ok"] is True and r["hints"] == []
    assert r["http_status"] is None and r["provider_message"] == "" and isinstance(r["latency_ms"], int)
    assert stage_names(r) == ["reachable", "model", "capabilities", "chat", "tools"] and all(s["ok"] for s in r["stages"])
    assert r["provider"] == "ollama" and r["model"] == "qwen2.5:7b" and r["role"] == "chat" and r["cached"] is False
    assert fake_ollama.paths() == ["/api/version", "/api/tags", "/api/show", "/api/chat", "/api/chat"]


def test_the_requests_carry_the_context_window_and_keep_alive(fake_ollama, monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "4096")
    fake_ollama.reply("OK")
    fake_ollama.reply_tool_call("ping", {"x": 1})
    r = llm.probe()
    first, second = fake_ollama.bodies("/api/chat")
    assert first["options"]["num_ctx"] == 4096 and first["options"]["num_predict"] == 5 and first["stream"] is False
    assert first["keep_alive"] == "30m" and "tools" not in first
    assert second["tools"][0]["function"]["name"] == "ping"
    assert any("OLLAMA_NUM_CTX is 4096" in h for h in r["hints"])         # too small to be safe: a hint, not a failure
    assert r["ok"] is True


def test_qwen3_is_probed_with_thinking_off(fake_ollama, monkeypatch):
    fake_ollama.add_model("qwen3:8b", ["completion", "tools"])
    monkeypatch.setenv("RECTIFI_MODEL_CHAT", "qwen3:8b")
    fake_ollama.reply("OK")
    fake_ollama.reply_tool_call("ping", {"x": 1})
    assert llm.probe()["ok"] is True
    assert all(b["think"] is False for b in fake_ollama.bodies("/api/chat"))


def test_model_missing_is_a_404_at_the_model_stage(fake_ollama, monkeypatch):
    monkeypatch.setenv("RECTIFI_MODEL_CHAT", "nope:1b")
    r = llm.probe()
    assert (r["ok"], r["stage"], r["http_status"]) == (False, "model", 404)
    assert "nope:1b" in r["provider_message"] and any("ollama pull nope:1b" in h for h in r["hints"])
    assert any("qwen2.5:7b" in h for h in r["hints"])                      # lists what is installed
    assert "/api/chat" not in fake_ollama.paths()


def test_a_model_without_tools_fails_at_capabilities(fake_ollama, monkeypatch):
    fake_ollama.add_model("tiny:1b", ["completion"])
    monkeypatch.setenv("RECTIFI_MODEL_CHAT", "tiny:1b")
    r = llm.probe()
    assert (r["ok"], r["stage"], r["tools_ok"]) == (False, "capabilities", False)
    assert any("does not support tool calling" in h and "ollama pull qwen2.5:7b" in h for h in r["hints"])
    assert "/api/chat" not in fake_ollama.paths()


def test_a_job_that_does_not_need_tools_passes_without_them(fake_ollama, monkeypatch):
    fake_ollama.add_model("tiny:1b", ["completion"])
    monkeypatch.setenv("RECTIFI_MODEL_CLASSIFY", "tiny:1b")
    fake_ollama.reply("OK")
    r = llm.probe("classify")
    assert r["ok"] is True and r["tools_ok"] is None and r["role"] == "classify" and r["model"] == "tiny:1b"
    assert stage_names(r) == ["reachable", "model", "capabilities", "chat"]
    assert len(fake_ollama.bodies("/api/chat")) == 1


def test_an_embedding_model_is_not_a_chat_model(fake_ollama, monkeypatch):
    monkeypatch.setenv("RECTIFI_MODEL_CLASSIFY", "nomic-embed-text")
    r = llm.probe("classify")
    assert r["ok"] is False and r["stage"] == "capabilities" and "embedding" in r["provider_message"]


def test_a_tool_call_written_as_text_is_flagged(fake_ollama):
    fake_ollama.reply("OK")
    fake_ollama.reply('```json\n{"name": "ping", "arguments": {"x": 1}}\n```')
    r = llm.probe()
    assert (r["ok"], r["stage"], r["tools_ok"], r["tools_as_text"]) == (False, "tools", False, True)
    assert any("as text" in h and "RECTIFI_MODEL_CODE" in h for h in r["hints"])


def test_a_model_that_ignores_the_tool(fake_ollama):
    fake_ollama.reply("OK")
    fake_ollama.reply("Sure, pong!")
    r = llm.probe()
    assert (r["ok"], r["stage"], r["tools_ok"], r["tools_as_text"]) == (False, "tools", False, False)
    assert any("did not call the tool" in h for h in r["hints"])


def test_a_slow_model_times_out_with_advice(fake_ollama):
    fake_ollama.delay = 1.0
    r = llm.probe(timeout=0.2)
    assert (r["ok"], r["stage"], r["http_status"]) == (False, "reachable", None)
    assert "No answer within 0.2 seconds" in r["provider_message"] and any("loads" in h or "loaded" in h for h in r["hints"])


def test_ollama_not_running(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:9")
    r = llm.probe(timeout=2)
    assert (r["ok"], r["stage"]) == (False, "reachable")
    hints = " ".join(r["hints"])
    assert "ollama serve" in hints and "127.0.0.1:11434" in hints and "restart Rectifi" in hints


def test_a_server_error_is_reported_with_its_status(fake_ollama):
    fake_ollama.fail_next(503, "overloaded")
    r = llm.probe()
    assert (r["ok"], r["stage"], r["http_status"]) == (False, "reachable", 503)
    assert r["provider_message"] == "overloaded" and any("problem on its side" in h for h in r["hints"])


def test_the_result_is_kept_for_a_minute_unless_fresh(fake_ollama):
    fake_ollama.reply("OK")
    fake_ollama.reply_tool_call("ping", {"x": 1})
    first = llm.probe()
    calls = len(fake_ollama.calls)
    second = llm.probe()
    assert len(fake_ollama.calls) == calls and second["cached"] is True and second["ok"] is True
    assert llm.cached_probe()["cached"] is True
    third = llm.probe(fresh=True)
    assert len(fake_ollama.calls) > calls and third["cached"] is False and first["cached"] is False


def test_cached_probe_is_empty_before_any_probe(fake_ollama):
    assert llm.cached_probe() is None


def test_each_role_has_its_own_cache_entry(fake_ollama, monkeypatch):
    monkeypatch.setenv("RECTIFI_MODEL_CLASSIFY", "qwen2.5:3b")
    fake_ollama.reply("OK")
    fake_ollama.reply_tool_call("ping", {"x": 1})
    llm.probe("chat")
    fake_ollama.reply("OK")
    assert llm.probe("classify")["model"] == "qwen2.5:3b"
    assert llm.cached_probe("classify")["model"] == "qwen2.5:3b" and llm.cached_probe("chat")["model"] == "qwen2.5:7b"


def test_an_unknown_role_is_refused():
    with pytest.raises(ValueError):
        llm.probe("poet")


def test_capabilities(fake_ollama):
    assert llm.capabilities() == ["completion", "tools"]
    assert llm.capabilities("nomic-embed-text") == ["embedding"]
    assert llm.capabilities("nope") is None


def test_capabilities_is_none_without_ollama(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    assert llm.capabilities() is None


# ───────────── OpenAI-style providers ─────────────

class FakeCompat:
    """/chat/completions with scripted answers: a string is a text reply, ("tool", name, args) a tool call, (status, body) an error."""

    def __init__(self, replies):
        self.replies, self.requests, outer = list(replies), [], self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body, {k.lower(): v for k, v in self.headers.items()}))
                reply = outer.replies.pop(0)
                status = 200
                if isinstance(reply, tuple) and isinstance(reply[0], int):
                    status, payload = reply
                else:
                    if isinstance(reply, tuple):
                        message = {"role": "assistant", "content": None, "tool_calls": [{
                            "id": "c1", "type": "function", "function": {"name": reply[1], "arguments": json.dumps(reply[2])}}]}
                    else:
                        message = {"role": "assistant", "content": reply}
                    payload = {"id": "x", "object": "chat.completion", "model": body["model"],
                               "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


KEY = "gsk-test-secret-1234567890"


@pytest.fixture
def compat(monkeypatch):
    servers = []

    def start(replies):
        server = FakeCompat(replies)
        servers.append(server)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        monkeypatch.setenv("no_proxy", "127.0.0.1")
        monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
        monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", server.url)
        monkeypatch.setenv("OPENAI_COMPAT_API_KEY", KEY)
        monkeypatch.setenv("OPENAI_COMPAT_MODEL", "tool-model")
        return server

    yield start
    for s in servers:
        s.close()


def test_compat_ok(compat):
    server = compat(["OK", ("tool", "ping", {"x": 1})])
    r = llm.probe()
    assert r["ok"] is True and r["tools_ok"] is True and stage_names(r) == ["reachable", "model", "chat", "tools"]
    path, body, headers = server.requests[0]
    assert path == "/v1/chat/completions" and body["model"] == "tool-model" and body["max_tokens"] == 5
    assert headers["authorization"] == f"Bearer {KEY}"
    assert server.requests[1][1]["tools"][0]["function"]["name"] == "ping"
    assert KEY not in json.dumps(r)


def test_compat_role_model_is_used(compat, monkeypatch):
    server = compat(["OK"])
    monkeypatch.setenv("RECTIFI_MODEL_CLASSIFY", "small-model")
    r = llm.probe("classify")
    assert r["ok"] is True and r["model"] == "small-model" and server.requests[0][1]["model"] == "small-model"


def test_compat_aq_key_gets_the_aq_advice_and_the_key_never_appears(compat, monkeypatch):
    aq = "AQ.Ab8RN6Ixyz-secret_0987654321"
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", aq)
    compat([(400, [{"error": {"code": 400, "message": f"API key not valid. Please pass a valid API key. ({aq})",
                              "status": "INVALID_ARGUMENT"}}])])
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", aq)
    r = llm.probe()
    assert (r["ok"], r["stage"], r["http_status"]) == (False, "chat", 400)
    text = json.dumps(r)
    assert aq not in text and "AQ.Ab8" not in text
    advice = " ".join(r["hints"])
    assert "starts with AQ." in advice and "AIza" in advice and "OpenRouter" in advice


def test_compat_access_token_type_unsupported(compat):
    compat([(401, {"error": {"message": "Expected OAuth 2 access token", "details": [{"reason": "ACCESS_TOKEN_TYPE_UNSUPPORTED"}]}})])
    r = llm.probe()
    assert r["ok"] is False and r["http_status"] == 401
    assert r["hints"] and "AQ." in r["hints"][0] or "401" in r["hints"][0]


def test_compat_401_and_404_and_429(compat, monkeypatch):
    compat([(401, {"error": {"message": "bad key"}})])
    r = llm.probe()
    assert r["stage"] == "chat" and "rejected the API key" in r["hints"][0]
    llm.clear_probe_cache()
    compat([(404, {"error": {"message": "The model `tool-model` does not exist"}})])
    r = llm.probe()
    assert r["stage"] == "model" and "doesn't know this model id" in r["hints"][0]
    llm.clear_probe_cache()
    compat([(429, {"error": {"message": "slow down"}})])
    assert "rate limiting" in llm.probe()["hints"][0]


def test_compat_tools_unsupported_is_a_404_at_the_tools_stage(compat, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    compat(["OK", (404, {"error": {"message": "No endpoints found that support tool use."}})])
    # compat() pointed LLM_PROVIDER at openai_compat; use that provider so the fake URL is honoured
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    r = llm.probe()
    assert (r["ok"], r["stage"], r["http_status"], r["tools_ok"]) == (False, "tools", 404, False)
    assert "tool calling" in r["hints"][0]


def test_compat_tool_call_as_text(compat):
    compat(["OK", '{"name": "ping", "arguments": {"x": 1}}'])
    r = llm.probe()
    assert r["stage"] == "tools" and r["tools_as_text"] is True


def test_compat_without_settings_is_a_config_failure_with_no_network(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    r = llm.probe()
    assert (r["ok"], r["stage"]) == (False, "config") and "OPENAI_COMPAT_BASE_URL" in r["provider_message"]


def test_deepseek_probe_uses_its_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-abcdefgh")
    seen = []

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            seen.append((url, json))
            request = httpx.Request("POST", url)
            if json.get("tools"):
                body = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "1", "type": "function",
                                                                                  "function": {"name": "ping", "arguments": "{}"}}]}}]}
            else:
                body = {"choices": [{"message": {"content": "OK"}}]}
            return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx, "Client", FakeClient)
    r = llm.probe()
    assert r["ok"] is True
    assert seen[0][0] == "https://api.deepseek.com/chat/completions" and seen[0][1]["model"] == "deepseek-flash"
    assert seen[0][1]["thinking"] == {"type": "disabled"}


# ───────────── redact ─────────────

@pytest.mark.parametrize("secret", [
    "AQ.Ab8RN6Ixyz-secret_0987", "AIzaSyA1234567890abcdefghijklmnop", "sk-or-v1-abcdef0123456789", "sk-abc123def456",
    "hf_AbCdEfGhIjKlMnOp", "Bearer abc.def.ghi",
])
def test_redact_hides_every_key_shape(secret):
    out = llm.redact(f"request failed for {secret} at the provider")
    assert secret.split()[-1] not in out and "request failed for" in out


def test_redact_hides_a_key_in_a_url_and_the_configured_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "plainlookingkey99")
    out = llm.redact("GET /v1/models?key=abc123XYZ&alt=json using plainlookingkey99")
    assert "abc123XYZ" not in out and "plainlookingkey99" not in out and "alt=json" in out


def test_redact_masks_account_numbers():
    assert "123456789012" not in llm.redact("account 123456789012 failed")


def test_redact_leaves_ordinary_words_alone():
    text = "The task-force asked about disk-usage and sk8 boards."
    assert llm.redact(text) == text


# ───────────── explain_error v2 ─────────────

class Failure(Exception):
    def __init__(self, status_code, message="", body=None):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code, self.body = status_code, body


def test_aq_key_400(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "AQ.Ab8RN6Ixyz-secret_0987654321")
    out = llm.explain_error(Failure(400, "Error code: 400 - API key not valid. Please pass a valid API key."))
    assert "AQ." in out and "AIza" in out and "Ab8RN6Ixyz" not in out


def test_access_token_type_unsupported_401(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    out = llm.explain_error(Failure(401, "ACCESS_TOKEN_TYPE_UNSUPPORTED"))
    assert "AQ." in out and "AIza" in out


def test_status_codes(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    assert "refused this request" in llm.explain_error(Failure(403)) and "HTTP 403" in llm.explain_error(Failure(403))
    assert "problem on its side" in llm.explain_error(Failure(502))
    assert "rate limit" in llm.explain_error(Failure(429)) or "50 a day" in llm.explain_error(Failure(429))


def test_404_model_not_found_versus_no_tools(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    not_found = llm.explain_error(Failure(404, "Error code: 404 - model 'x/y' not found"))
    no_tools = llm.explain_error(Failure(404, "No endpoints found that support tool use."))
    assert "doesn't know this model id" in not_found and "tool calling" not in not_found
    assert "tool calling" in no_tools and "free-models" in no_tools


def test_404_on_ollama_says_pull(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    out = llm.explain_error(Failure(404, 'model "qwen2.5:7b" not found, try pulling it first'))
    assert "ollama pull qwen2.5:7b" in out


def test_ollama_does_not_support_tools(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    out = llm.explain_error(Failure(400, "registry.ollama.ai/library/tiny:1b does not support tools"))
    assert "does not support tool calling" in out and "ollama pull qwen2.5:7b" in out


def test_a_real_ollama_response_error(fake_ollama, monkeypatch):
    from langchain_ollama import ChatOllama

    fake_ollama.add_model("tiny:1b", ["completion"])
    model = ChatOllama(model="tiny:1b", base_url=fake_ollama.base_url).bind_tools(
        [{"type": "function", "function": llm._PING_TOOL["function"]}])
    with pytest.raises(Exception) as caught:
        model.invoke("hi")
    assert "does not support tool calling" in llm.explain_error(caught.value)
    with pytest.raises(Exception) as caught:
        ChatOllama(model="nope", base_url=fake_ollama.base_url).invoke("hi")
    assert "ollama pull nope" in llm.explain_error(caught.value)


def test_timeouts_and_refused_connections(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_TIMEOUT", "45")
    out = llm.explain_error(httpx.ReadTimeout("timed out"))
    assert "45 seconds" in out and "OLLAMA_TIMEOUT" in out
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    out = llm.explain_error(httpx.ConnectError("refused"))
    assert "ollama serve" in out and "127.0.0.1:11434" in out
    assert "Can't reach Ollama" in llm.explain_error(ConnectionError("Failed to connect to Ollama."))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    assert "didn't answer in time" in llm.explain_error(httpx.ReadTimeout("t"))
    assert "Can't reach the provider" in llm.explain_error(httpx.ConnectError("refused"))


def test_the_openai_client_errors(clean, monkeypatch):
    import openai

    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    request = httpx.Request("POST", "https://x.invalid/v1/chat/completions")
    assert "didn't answer in time" in llm.explain_error(openai.APITimeoutError(request=request))
    assert "Can't reach the provider" in llm.explain_error(openai.APIConnectionError(request=request))
    response = httpx.Response(403, request=request, json={"error": {"message": "nope"}})
    error = openai.PermissionDeniedError("nope", response=response, body={"message": "nope"})
    assert "HTTP 403" in llm.explain_error(error)


def test_the_fallback_is_the_message_with_keys_removed_and_cut_short(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    out = llm.explain_error(ValueError("boom with sk-live-abcdef123456 inside " + "x" * 600))
    assert "sk-live" not in out and out.startswith("boom with [key hidden] inside") and len(out) <= 300
    assert llm.explain_error(ValueError("boom")) == "boom"


# ───────────── a connect timeout means "not running", never "loading" ─────────────

def test_a_connect_timeout_is_ollama_not_running_not_a_loading_model(monkeypatch):
    # Windows takes about 2 s to refuse a closed port, so httpx reports ConnectTimeout. A model that is still
    # loading accepts the TCP connection first, so a connect timeout can only mean nothing is listening.
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    target = {"provider": "ollama", "base": "http://127.0.0.1:11434", "model": "m"}
    run = llm._Run(target, "chat", 5)
    r = llm._fail_http(run, "reachable", httpx.ConnectTimeout("timed out"))
    hints = " ".join(r["hints"])
    assert (r["ok"], r["stage"]) == (False, "reachable")
    assert "ollama serve" in hints and "loads" not in hints and "loaded" not in hints
    assert "Can't connect" in r["provider_message"]


def test_a_read_timeout_is_still_a_loading_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    run = llm._Run({"provider": "ollama", "base": "http://127.0.0.1:11434", "model": "m"}, "chat", 5)
    r = llm._fail_http(run, "reachable", httpx.ReadTimeout("slow"))
    assert "loads" in " ".join(r["hints"]) and "ollama serve" not in " ".join(r["hints"])


def test_explain_error_says_cant_reach_for_a_connect_timeout_and_keeps_the_slow_model_text(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    out = llm.explain_error(httpx.ConnectTimeout("timed out"))
    assert "Can't reach Ollama" in out and "ollama serve" in out and "didn't answer" not in out
    assert "didn't answer" in llm.explain_error(httpx.ReadTimeout("slow"))
