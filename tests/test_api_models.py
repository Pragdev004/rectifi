"""/api/models/status and /api/models/probe, against the fake Ollama server."""

import json
import threading

import pytest
from api_support import make_client

from rectifi.agent import llm


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("RECTIFI_MODEL_CHAT", "RECTIFI_MODEL_CLASSIFY", "RECTIFI_MODEL_CODE", "RECTIFI_MODEL_GRADE", "OLLAMA_MODEL",
                 "OPENAI_COMPAT_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    llm.clear_probe_cache()
    yield
    llm.clear_probe_cache()


@pytest.fixture
def client():
    return make_client()


def test_status_never_calls_the_model(client, fake_ollama):
    body = client.get("/api/models/status").json()
    assert body["ok"] is True and body["why"] == "" and body["provider"] == "ollama" and body["model"] == "ollama · qwen2.5:7b"
    assert [r["role"] for r in body["roles"]] == ["chat", "classify", "code", "grade"]
    assert body["roles"][0] == {"role": "chat", "model": "qwen2.5:7b", "named": False}
    assert body["embed_model"] == "nomic-embed-text" and body["probe"] is None
    assert "/api/chat" not in fake_ollama.paths()


def test_status_says_what_is_wrong(client, fake_ollama, monkeypatch):
    monkeypatch.setenv("RECTIFI_MODEL_CODE", "gone:7b")
    body = client.get("/api/models/status").json()
    assert body["ok"] is True and body["roles"][2] == {"role": "code", "model": "gone:7b", "named": True}
    monkeypatch.setenv("RECTIFI_MODEL_CHAT", "gone:7b")
    body = client.get("/api/models/status").json()
    assert body["ok"] is False and "ollama pull gone:7b" in body["why"]


def test_status_when_ollama_is_off(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    body = client.get("/api/models/status").json()
    assert body["ok"] is False and "isn't running" in body["why"]


def test_status_for_a_cloud_provider_has_no_embed_model(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    body = client.get("/api/models/status").json()
    assert body["ok"] is True and body["embed_model"] is None and body["model"] == "deepseek · deepseek-flash"


def test_probe_runs_the_real_calls_and_status_then_shows_it(client, fake_ollama):
    fake_ollama.reply("OK")
    fake_ollama.reply_tool_call("ping", {"x": 1})
    r = client.post("/api/models/probe", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["stage"] == "ok" and body["tools_ok"] is True and body["role"] == "chat"
    assert [s["name"] for s in body["stages"]] == ["reachable", "model", "capabilities", "chat", "tools"]
    calls = len(fake_ollama.calls)
    status = client.get("/api/models/status").json()
    assert status["probe"]["ok"] is True and status["probe"]["cached"] is True
    assert len(fake_ollama.calls) == calls + 1                              # only llm_status's quick look at /api/tags


def test_probe_is_always_fresh(client, fake_ollama):
    for _ in range(2):
        fake_ollama.reply("OK")
        fake_ollama.reply_tool_call("ping", {"x": 1})
        assert client.post("/api/models/probe", json={}).json()["cached"] is False
    assert fake_ollama.paths().count("/api/version") == 2


def test_probe_a_role(client, fake_ollama, monkeypatch):
    monkeypatch.setenv("RECTIFI_MODEL_CLASSIFY", "qwen2.5:3b")
    fake_ollama.reply("OK")
    body = client.post("/api/models/probe", json={"role": "classify"}).json()
    assert body["ok"] is True and body["model"] == "qwen2.5:3b" and body["tools_ok"] is None


def test_probe_failure_is_a_200_with_the_reason(client, fake_ollama, monkeypatch):
    monkeypatch.setenv("RECTIFI_MODEL_CHAT", "nope:1b")
    r = client.post("/api/models/probe", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["stage"] == "model" and body["http_status"] == 404
    assert any("ollama pull nope:1b" in h for h in body["hints"])


def test_probe_never_returns_a_key(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "AQ.Ab8RN6Ixyz-secret_0987654321")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "m")
    body = client.post("/api/models/probe", json={}).json()
    assert body["ok"] is False and body["stage"] == "reachable"
    assert "AQ.Ab8" not in json.dumps(body) and "Ab8RN6" not in client.get("/api/models/status").text


@pytest.mark.parametrize("role", ["poet", 3, ["chat"]])
def test_probe_rejects_a_bad_role(client, role):
    r = client.post("/api/models/probe", json={"role": role})
    assert r.status_code == 422 and r.json()["error"]["field"] == "role"


def test_two_checks_at_once_is_a_409(client, fake_ollama):
    fake_ollama.delay = 0.5
    answers = []
    t = threading.Thread(target=lambda: answers.append(make_client().post("/api/models/probe", json={}).status_code))
    t.start()
    import time

    time.sleep(0.2)
    r = client.post("/api/models/probe", json={})
    t.join()
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy"


def test_the_guards_still_apply(fake_ollama):
    bare = make_client(headers={})
    assert bare.get("/api/models/status").status_code in (400, 403)
    assert bare.post("/api/models/probe", json={}).status_code in (400, 403)
