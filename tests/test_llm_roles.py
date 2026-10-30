"""Role models (chat, classify, code, grade) in describe(), model_name(), llm_status() and get_llm()."""

import pytest

from rectifi.agent import llm

KEYS = ["LLM_PROVIDER", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "HF_TOKEN", "HF_MODEL", "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "RECTIFI_MODEL_CHAT",
        "RECTIFI_MODEL_CLASSIFY", "RECTIFI_MODEL_CODE", "RECTIFI_MODEL_GRADE", "OLLAMA_MODEL"]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)


def test_ollama_roles_fall_back_to_chat_then_ollama_model(fake_ollama, monkeypatch):
    assert llm.describe() == "ollama · qwen2.5:7b" and llm.describe("code") == "ollama · qwen2.5:7b"
    monkeypatch.setenv("RECTIFI_MODEL_CHAT", "qwen2.5:3b")
    assert llm.model_name("classify") == "qwen2.5:3b"
    monkeypatch.setenv("RECTIFI_MODEL_CODE", "qwen2.5-coder:7b")
    assert llm.describe("code") == "ollama · qwen2.5-coder:7b" and llm.describe("chat") == "ollama · qwen2.5:3b"


def test_ollama_status_checks_the_model_of_the_role(fake_ollama, monkeypatch):
    assert llm.llm_status("code") == (True, "")
    monkeypatch.setenv("RECTIFI_MODEL_CODE", "qwen2.5-coder:14b")
    assert llm.llm_status() == (True, "")                                  # chat is untouched
    ok, why = llm.llm_status("code")
    assert not ok and "ollama pull qwen2.5-coder:14b" in why
    fake_ollama.add_model("qwen2.5-coder:14b", ["completion", "tools"])
    assert llm.llm_status("code") == (True, "")


def test_installed_match():
    assert llm.installed_match("llama3", ["llama3:latest"]) == "llama3:latest"
    assert llm.installed_match("qwen2.5:7b", ["qwen2.5:14b"]) is None


def test_deepseek_role_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert llm.describe("grade") == "deepseek · deepseek-flash"
    monkeypatch.setenv("RECTIFI_MODEL_GRADE", "deepseek-small")
    assert llm.describe("grade") == "deepseek · deepseek-small" and llm.describe() == "deepseek · deepseek-flash"
    assert llm.get_llm("grade").model_name == "deepseek-small" and llm.get_llm().model_name == "deepseek-flash"


def test_openrouter_role_variable_stands_in_for_a_missing_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    ok, why = llm.llm_status()
    assert not ok and "OPENROUTER_MODEL" in why and llm.describe() == "openrouter · (no model set)"
    monkeypatch.setenv("RECTIFI_MODEL_CHAT", "vendor/model:free")
    assert llm.llm_status() == (True, "") and llm.describe() == "openrouter · vendor/model:free"
    assert llm.get_llm("classify").model_name == "vendor/model:free"       # a role with no model of its own uses the chat one
    monkeypatch.setenv("RECTIFI_MODEL_CLASSIFY", "vendor/small:free")
    assert llm.get_llm("classify").model_name == "vendor/small:free" and llm.get_llm().model_name == "vendor/model:free"


def test_ollama_get_llm_per_role(fake_ollama, monkeypatch):
    monkeypatch.setenv("RECTIFI_MODEL_CLASSIFY", "qwen2.5:3b")
    assert llm.get_llm("classify").model == "qwen2.5:3b" and llm.get_llm("chat").model == "qwen2.5:7b"


def test_unknown_role(fake_ollama):
    with pytest.raises(ValueError):
        llm.describe("poet")
    with pytest.raises(ValueError):
        llm.llm_status("poet")
